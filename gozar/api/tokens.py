"""Admin control-path router for the Token_Authority (Requirements 8.3, 8.4, 9.x).

Exposes Gozar API key issuance and lifecycle: create, password-confirmed reveal,
list (secret-free), set the usage limit, rotate, enable/disable, and revoke.
Every route is guarded by the fail-closed :func:`gozar.auth.rbac.require` dependency with
:data:`~gozar.auth.rbac.Permission.MANAGE_TOKENS` (Requirements 16.1, 16.3).

List responses never include the token secret. Create, password-confirmed reveal,
and explicit rotation are the only secret-returning endpoints.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response, status
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from gozar.api.schemas import (
    CreateTokenRequest,
    IssuedTokenResponse,
    RevealTokenRequest,
    RotateTokenRequest,
    SetEnabledRequest,
    SetTokenChainRequest,
    TestTokenRouteRequest,
    TokenResponse,
)
from gozar.auth.models import Operator
from gozar.auth.rbac import Identity, Permission, require
from gozar.auth.service import verify_password
from gozar.core.db import get_session
from gozar.core.errors import AuthError
from gozar.core.logging import get_logger
from gozar.core.redis import get_redis
from gozar.gateway.catalog import list_available_models_for_token
from gozar.gateway.pipeline import complete_chat_completion
from gozar.tokens.service import (
    create_token,
    list_tokens,
    reveal_token,
    revoke,
    rotate_token,
    set_assigned_chain,
    set_enabled,
    set_usage_limit,
    store_existing_token_secret,
)
from gozar.translation.types import OpenAIChatRequest, OpenAIChatResponse, OpenAIModelList
from gozar.usage.limits import UsageLimitSpec
from gozar.usage.service import SUBJECT_TOKEN, read_subject_consumption

router = APIRouter(prefix="/tokens", tags=["tokens"])

_logger = get_logger(__name__)

# Single fail-closed guard shared by every route in this router.
_guard = require(Permission.MANAGE_TOKENS)


async def _require_operator_password(
    session: AsyncSession, identity: Identity, password: str
) -> None:
    """Validate the current operator password for sensitive token-secret actions."""
    try:
        operator_id = uuid.UUID(identity.operator_id)
    except ValueError as exc:
        raise AuthError("invalid authentication credentials") from exc

    operator = await session.get(Operator, operator_id)
    if operator is None or not verify_password(operator.password_hash, password):
        raise AuthError("invalid username or password")


async def _token_usage(token_id: uuid.UUID, limit: UsageLimitSpec | None) -> float:
    """Read a token's recorded usage from the Usage_Recorder counters.

    Reads the same Redis counters the Proxy_Gateway enforces limits against (via
    :func:`gozar.usage.service.read_subject_consumption`). The counter store is a
    metering side-channel, not the source of truth, so if it is unavailable
    (unconfigured or unreachable) the listing degrades to ``0.0`` rather than failing
    the operator's tokens view; the condition is logged for observability.
    """
    try:
        return await read_subject_consumption(
            get_redis(), SUBJECT_TOKEN, token_id, limit
        )
    except (RedisError, RuntimeError, OSError) as exc:
        _logger.warning(
            "usage counter unavailable; reporting token usage as 0.0",
            extra={"token_id": str(token_id), "error": str(exc)},
        )
        return 0.0


@router.get("", summary="List Gozar API keys", response_model=list[TokenResponse])
async def list_tokens_route(
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> list[TokenResponse]:
    """Return secret-free views of every Gozar API key (Requirement 8.3)."""
    views = await list_tokens(session, consumption_lookup=_token_usage)
    return [TokenResponse.from_view(view) for view in views]


@router.post(
    "",
    summary="Create a Gozar API key",
    response_model=IssuedTokenResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_token_route(
    payload: CreateTokenRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> IssuedTokenResponse:
    """Issue a new Gozar API key; the secret is returned to the operator (Req 8.1)."""
    issued = await create_token(
        session,
        payload.label,
        payload.limit,
        payload.assigned_chain_id,
        settings=request.app.state.settings,
    )
    return IssuedTokenResponse.from_issued(issued)


@router.get(
    "/{token_id}/models",
    summary="List models reachable by a Gozar API key",
    response_model=OpenAIModelList,
)
async def list_token_models_route(
    token_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> OpenAIModelList:
    """Return the OpenAI-shaped model list for this key's selected route."""
    settings = request.app.state.settings
    return await list_available_models_for_token(
        session,
        token_id,
        settings=settings,
    )


@router.post(
    "/{token_id}/test",
    summary="Test an existing Gozar API key route without revealing its secret",
    response_model=OpenAIChatResponse,
)
async def test_token_route(
    token_id: uuid.UUID,
    payload: TestTokenRouteRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> OpenAIChatResponse:
    """Execute a small prompt using the selected key's status, limit, and routing."""

    return await complete_chat_completion(
        session,
        presented_token=None,
        trusted_token_id=token_id,
        chain_override_id=payload.chain_id,
        request=OpenAIChatRequest(
            model=payload.model,
            messages=[{"role": "user", "content": payload.prompt}],
        ),
        settings=request.app.state.settings,
    )


@router.put(
    "/{token_id}/limit",
    summary="Set an API key usage limit",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def set_token_limit_route(
    token_id: uuid.UUID,
    limit: UsageLimitSpec,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> Response:
    """Persist (or replace) the API key's Usage_Limit (Requirement 9.1)."""
    await set_usage_limit(session, token_id, limit)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/{token_id}/chain",
    summary="Assign or clear an API key fallback chain",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def set_token_chain_route(
    token_id: uuid.UUID,
    payload: SetTokenChainRequest,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> Response:
    """Persist which Fallback_Chain an API key routes through, or clear it."""
    await set_assigned_chain(session, token_id, payload.assigned_chain_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{token_id}/reveal",
    summary="Reveal an existing Gozar API key without rotating it",
    response_model=IssuedTokenResponse,
)
async def reveal_token_route(
    token_id: uuid.UUID,
    payload: RevealTokenRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> IssuedTokenResponse:
    """Return the same API key after confirming the operator password."""
    await _require_operator_password(session, identity, payload.password)
    if payload.existing_api_key:
        issued = await store_existing_token_secret(
            session,
            token_id,
            payload.existing_api_key,
            settings=request.app.state.settings,
        )
    else:
        issued = await reveal_token(
            session,
            token_id,
            settings=request.app.state.settings,
        )
    return IssuedTokenResponse.from_issued(issued)


@router.post(
    "/{token_id}/rotate",
    summary="Rotate a Gozar API key (returns the replacement secret once)",
    response_model=IssuedTokenResponse,
    status_code=status.HTTP_201_CREATED,
)
async def rotate_token_route(
    token_id: uuid.UUID,
    payload: RotateTokenRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> IssuedTokenResponse:
    """Issue a replacement API key after confirming the operator password."""
    await _require_operator_password(session, identity, payload.password)

    issued = await rotate_token(session, token_id, settings=request.app.state.settings)
    return IssuedTokenResponse.from_issued(issued)


@router.patch(
    "/{token_id}/enabled",
    summary="Enable or disable an API key",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def set_token_enabled_route(
    token_id: uuid.UUID,
    payload: SetEnabledRequest,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> Response:
    """Enable or disable an API key (Requirements 9.3, 9.4)."""
    await set_enabled(session, token_id, payload.enabled)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{token_id}/revoke",
    summary="Revoke an API key (terminal)",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_token_route(
    token_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> Response:
    """Permanently revoke an API key (Requirement 8.4)."""
    await revoke(session, token_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
