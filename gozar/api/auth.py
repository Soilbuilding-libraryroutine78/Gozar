"""Public auth control-path router for operator login, refresh, and bootstrap.

This router exposes the operator-authentication HTTP surface under ``/api/auth``
that the Web_Console drives (``frontend/src/api/config.ts``). Unlike every other
admin router, these routes are **public by necessity**: login and first-run
bootstrap cannot require an existing session, because no session can exist before
an operator has logged in (or before the very first admin is created).

Routes:

* ``POST /api/auth/login``     - exchange operator credentials for signed session
  tokens (:func:`gozar.auth.service.authenticate`, Requirement 16.1). Bad
  credentials fail with a generic 401 that never reveals which half was wrong.
* ``POST /api/auth/refresh``   - exchange a valid refresh token for a fresh session
  token pair (:func:`gozar.auth.session.refresh_session`).
* ``GET  /api/auth/bootstrap`` - report whether first-run admin bootstrap is still
  required (:func:`gozar.auth.service.bootstrap_required`, Requirement 19.2).
* ``POST /api/auth/bootstrap`` - create the first administrative operator while the
  instance is un-bootstrapped (:func:`gozar.auth.service.create_initial_admin`) and
  log them straight in. Once an admin exists this path is closed (HTTP 409), which
  is what gates every administrative function behind first-run bootstrap: until the
  initial admin is established no operator can authenticate, so the fail-closed
  ``require(...)`` guard on every other admin route rejects all callers.

The success bodies match the ``SessionTokens`` shape the console is typed against
(:class:`gozar.auth.session.SessionTokens`). Errors are rendered through the shared
admin error envelope by the registered :class:`gozar.core.errors.GozarError`
handler, so this router raises typed domain errors and never builds bodies by hand.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from gozar.auth.service import authenticate, bootstrap_required, create_initial_admin
from gozar.auth.session import SessionTokens, issue_session_tokens, refresh_session
from gozar.core.config import Settings, get_settings
from gozar.core.db import get_session

router = APIRouter(prefix="/auth", tags=["auth"])


def get_request_settings(request: Request) -> Settings:
    """Return the settings the app was assembled with (fail closed to the singleton).

    The auth flows must sign and verify session tokens with the exact configuration
    the rest of the app uses, so they read the settings stored on the app state by
    :func:`gozar.app.create_app` rather than re-reading the environment.
    """
    return getattr(request.app.state, "settings", None) or get_settings()


# ---------------------------------------------------------------------------
# Request/response models (mirror gozar.auth.session.SessionTokens)
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    """Operator login credentials."""

    username: str = Field(..., description="Operator username.")
    password: str = Field(..., description="Operator password.")


class RefreshRequest(BaseModel):
    """A refresh-token exchange request."""

    refresh_token: str = Field(..., description="A previously issued refresh token.")


class BootstrapCreateRequest(BaseModel):
    """First-run initial-admin creation credentials."""

    username: str = Field(..., description="Username for the first administrator.")
    password: str = Field(..., description="Password for the first administrator.")


class SessionTokensResponse(BaseModel):
    """The OAuth2-style token bundle returned on login, refresh, and bootstrap."""

    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int

    @classmethod
    def from_tokens(cls, tokens: SessionTokens) -> "SessionTokensResponse":
        return cls(
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            token_type=tokens.token_type,
            expires_in=tokens.expires_in,
        )


class BootstrapStatusResponse(BaseModel):
    """Whether first-run admin bootstrap is still required (Requirement 19.2)."""

    bootstrap_required: bool


# ---------------------------------------------------------------------------
# Routes (public: no require() guard)
# ---------------------------------------------------------------------------


@router.post("/login", summary="Operator login", response_model=SessionTokensResponse)
async def login_route(
    payload: LoginRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_request_settings),
) -> SessionTokensResponse:
    """Authenticate an operator and issue signed session tokens (Requirement 16.1).

    Raises :class:`gozar.core.errors.AuthError` (HTTP 401) on any bad credential; the
    message never reveals whether the username or the password was wrong.
    """
    tokens = await authenticate(
        session, payload.username, payload.password, settings=settings
    )
    return SessionTokensResponse.from_tokens(tokens)


@router.post(
    "/refresh", summary="Refresh a session", response_model=SessionTokensResponse
)
async def refresh_route(
    payload: RefreshRequest,
    settings: Settings = Depends(get_request_settings),
) -> SessionTokensResponse:
    """Exchange a valid refresh token for a fresh session token pair.

    Raises :class:`gozar.core.errors.AuthError` (HTTP 401) on any invalid, expired,
    tampered, or non-refresh token.
    """
    tokens = refresh_session(payload.refresh_token, settings=settings)
    return SessionTokensResponse.from_tokens(tokens)


@router.get(
    "/bootstrap",
    summary="First-run bootstrap status",
    response_model=BootstrapStatusResponse,
)
async def bootstrap_status_route(
    session: AsyncSession = Depends(get_session),
) -> BootstrapStatusResponse:
    """Report whether the instance still needs its first administrator (Req 19.2)."""
    required = await bootstrap_required(session)
    return BootstrapStatusResponse(bootstrap_required=required)


@router.post(
    "/bootstrap",
    summary="Create the first administrator (first-run only)",
    response_model=SessionTokensResponse,
    status_code=status.HTTP_201_CREATED,
)
async def bootstrap_create_route(
    payload: BootstrapCreateRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_request_settings),
) -> SessionTokensResponse:
    """Create the initial admin while un-bootstrapped, then log them in.

    Permitted only while :func:`gozar.auth.service.bootstrap_required` is true;
    enforces the configured credential policy, and once any operator exists this path
    is closed with :class:`gozar.auth.service.BootstrapAlreadyCompleteError`
    (HTTP 409). This is the single seam that establishes administrative access on a
    fresh instance (Requirement 19.2).
    """
    operator = await create_initial_admin(
        session, payload.username, payload.password, settings=settings
    )
    tokens = issue_session_tokens(operator.id, operator.role, settings=settings)
    return SessionTokensResponse.from_tokens(tokens)


__all__ = ["router"]
