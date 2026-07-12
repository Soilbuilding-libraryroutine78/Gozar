"""Signed operator session tokens (JWT) with refresh.

This module implements the session-token slice of the Auth_Service (design
"Auth_Service", Requirement 16.1): short-lived signed **access** tokens paired with a
longer-lived **refresh** token. Tokens are signed with HMAC-SHA256 (``HS256``) using
the configured ``GOZAR_JWT_SECRET``; every lifetime is read from
:class:`~gozar.core.config.Settings` (``jwt_access_ttl_seconds`` /
``jwt_refresh_ttl_seconds``). There are no hardcoded secrets or TTLs.

Claims carried by each token:

* ``sub``  - the operator id (UUID string).
* ``role`` - the operator's role string (drives fail-closed RBAC in task 3.3).
* ``iat``  - issued-at (UTC).
* ``exp``  - expiry (UTC); short for access, long for refresh.
* ``type`` - ``access`` or ``refresh``; checked on decode so an access token can
  never be used where a refresh token is required and vice-versa.
* ``jti``  - a unique token id, so individual tokens are addressable (e.g. for future
  revocation/denylisting).

:func:`decode_session_token` is the single verification seam used by the
``require(permission)`` dependency (task 3.3): it verifies the signature, expiry, and
token ``type`` and returns the validated claims, raising :class:`AuthError` on any
invalid, expired, tampered, or wrong-type token.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt

from gozar.core.config import Settings, get_settings
from gozar.core.errors import AuthError, ConfigError

# JWT signing algorithm. HMAC-SHA256 with the configured shared secret; symmetric is
# appropriate because Gozar both signs and verifies its own operator sessions.
ALGORITHM = "HS256"

# Token ``type`` claim values. Access tokens authorize admin requests; refresh tokens
# may only be exchanged for a new session via :func:`refresh_session`.
TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"

# OAuth2-style scheme name returned to clients; presented as ``Authorization: Bearer``.
BEARER = "bearer"


@dataclass(frozen=True)
class SessionTokens:
    """The credential bundle returned on successful login or refresh.

    ``expires_in`` is the access token's lifetime in seconds (mirrors the OAuth2
    token-response field) so clients know when to refresh.
    """

    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int


def _now() -> datetime:
    """Current UTC time. Wrapped so tests can reason about token timing."""
    return datetime.now(timezone.utc)


def _require_secret(settings: Settings) -> str:
    """Return the configured JWT secret or fail closed.

    Signing or verifying a session token without a configured secret is a
    misconfiguration, not a client error, so this raises :class:`ConfigError`
    (HTTP 500) rather than silently using a weak/empty key.
    """
    if not settings.jwt_secret:
        raise ConfigError(
            "GOZAR_JWT_SECRET is not configured; cannot sign or verify session tokens."
        )
    return settings.jwt_secret


def _encode(
    *,
    operator_id: str,
    role: str,
    token_type: str,
    ttl_seconds: int,
    secret: str,
    issued_at: datetime,
) -> str:
    """Encode a single signed token with the standard claim set."""
    payload = {
        "sub": operator_id,
        "role": role,
        "type": token_type,
        "iat": issued_at,
        "exp": issued_at + timedelta(seconds=ttl_seconds),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def issue_session_tokens(
    operator_id: uuid.UUID | str,
    role: str,
    *,
    settings: Settings | None = None,
) -> SessionTokens:
    """Issue a fresh access + refresh token pair for an operator.

    The access token is short-lived (``jwt_access_ttl_seconds``); the refresh token
    is long-lived (``jwt_refresh_ttl_seconds``). Both lifetimes come from
    configuration. The two tokens are issued at the same instant so their expiries are
    deterministic relative to one another.
    """
    settings = settings or get_settings()
    secret = _require_secret(settings)
    issued_at = _now()
    subject = str(operator_id)

    access_token = _encode(
        operator_id=subject,
        role=role,
        token_type=TOKEN_TYPE_ACCESS,
        ttl_seconds=settings.jwt_access_ttl_seconds,
        secret=secret,
        issued_at=issued_at,
    )
    refresh_token = _encode(
        operator_id=subject,
        role=role,
        token_type=TOKEN_TYPE_REFRESH,
        ttl_seconds=settings.jwt_refresh_ttl_seconds,
        secret=secret,
        issued_at=issued_at,
    )
    return SessionTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type=BEARER,
        expires_in=settings.jwt_access_ttl_seconds,
    )


def decode_session_token(
    token: str,
    expected_type: str,
    *,
    settings: Settings | None = None,
) -> dict:
    """Verify a session token's signature, expiry, and type; return its claims.

    Raises :class:`~gozar.core.errors.AuthError` (HTTP 401) when the token is missing,
    malformed, has an invalid signature, has expired, or carries a ``type`` claim that
    does not match ``expected_type``. The error message is intentionally generic and
    never echoes the token value (Requirement 16.4).
    """
    settings = settings or get_settings()
    secret = _require_secret(settings)

    if not token:
        raise AuthError("invalid or expired session token")

    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            options={"require": ["exp", "iat", "sub", "type"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("session token has expired") from exc
    except jwt.InvalidTokenError as exc:
        # Covers bad signature, malformed token, missing required claims, etc.
        raise AuthError("invalid or expired session token") from exc

    if claims.get("type") != expected_type:
        raise AuthError("session token has the wrong type")

    return claims


def refresh_session(
    refresh_token: str,
    *,
    settings: Settings | None = None,
) -> SessionTokens:
    """Exchange a valid refresh token for a new session token pair.

    Validates the presented token as a refresh token (signature + expiry + type) and
    issues a brand-new access + refresh pair for the same operator. Issuing a new
    refresh token rotates the credential on every use, which is simple and correct.
    Any invalid, expired, tampered, or non-refresh token raises
    :class:`~gozar.core.errors.AuthError`.
    """
    settings = settings or get_settings()
    claims = decode_session_token(
        refresh_token, TOKEN_TYPE_REFRESH, settings=settings
    )
    return issue_session_tokens(
        claims["sub"], claims.get("role", ""), settings=settings
    )
