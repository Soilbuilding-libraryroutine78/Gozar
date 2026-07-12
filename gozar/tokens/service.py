"""Token_Authority: client-token issuance, verification, and lifecycle.

This module implements the Token_Authority described in the design. A Client_Token
is generated as a high-entropy random ``secret`` paired with a non-secret
``id_prefix`` lookup key and rendered as the string ``gz-<id_prefix>-<secret>``.
Verification uses the ``id_prefix`` plus an HMAC-SHA256 digest of the secret keyed
with the server-side pepper from configuration. New tokens also store the full
presentable key envelope-encrypted at rest so the same key can be revealed after
operator password confirmation without rotation.

Verification parses the presented string, locates the row by ``id_prefix``, and
compares the recomputed HMAC against the stored digest in constant time
(:func:`hmac.compare_digest`). A token authorizes a request only when the comparison
succeeds **and** the token is ``active``; ``disabled`` and ``revoked`` tokens are not
authorized (Requirements 8.4, 9.3, 9.4).

All functions operate on a caller-supplied :class:`~sqlalchemy.ext.asyncio.AsyncSession`
and never log the raw secret. ``list_tokens`` returns id prefix, label, status,
configured limit, and recorded usage -- never the secret (Requirement 8.3).
"""

from __future__ import annotations

import hmac
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gozar.core.config import Settings, get_settings
from gozar.core.crypto import decrypt, encrypt
from gozar.core.errors import ConfigError, NotFound, ValidationError
from gozar.routing.models import RouteFallbackChain
from gozar.tokens.models import ClientToken, TokenStatus, TokenUsageLimit
from gozar.usage.limits import LimitMetric, LimitWindow, UsageLimitSpec

# Token string format: ``gz-<id_prefix>-<secret>``.
_TOKEN_SCHEME = "gz"
# Byte-length of the random material. token_urlsafe yields ~1.3 chars/byte.
_ID_PREFIX_BYTES = 8  # 16 hex chars; non-secret, only a lookup key.
_SECRET_BYTES = 32  # 256 bits of entropy for the secret.


@dataclass(frozen=True)
class IssuedToken:
    """The result of creating, revealing, or rotating a token.

    ``secret`` is the full presentable token string (``gz-<id_prefix>-<secret>``) and
    must not be logged.
    """

    token_id: uuid.UUID
    id_prefix: str
    label: str
    status: str
    assigned_chain_id: uuid.UUID | None
    secret: str


@dataclass(frozen=True)
class TokenAuthResult:
    """The outcome of a successful :func:`verify` (no secret material)."""

    token_id: uuid.UUID
    status: str
    assigned_chain_id: uuid.UUID | None


@dataclass(frozen=True)
class TokenView:
    """A secret-free view of a token for listing (Requirement 8.3)."""

    token_id: uuid.UUID
    id_prefix: str
    label: str
    status: str
    assigned_chain_id: uuid.UUID | None
    assigned_chain_name: str | None
    limit: UsageLimitSpec | None
    usage: float
    can_reveal: bool


# Injectable consumption lookup. Given a token id and its current limit spec, it
# returns the recorded usage the listing should display. Mirrors the Account_Manager's
# ``ConsumptionLookup`` shape so both services share one contract. The real lookup
# (reading the Usage_Recorder counters from Redis) is wired at the router layer; the
# default below reports zero so the service stays unit-testable without Redis.
ConsumptionLookup = Callable[[uuid.UUID, "UsageLimitSpec | None"], Awaitable[float]]


async def _zero_usage(token_id: uuid.UUID, limit: UsageLimitSpec | None) -> float:
    """Default :data:`ConsumptionLookup`: report zero in a no-Redis context."""
    return 0.0


def _pepper(settings: Settings | None = None) -> bytes:
    """Return the server-side HMAC pepper, failing closed if unconfigured.

    The pepper is sourced exclusively from :class:`~gozar.core.config.Settings`
    (``GOZAR_TOKEN_PEPPER``); there is no hardcoded fallback (Requirement 16.4,
    steering 9.1).
    """
    settings = settings or get_settings()
    if not settings.token_pepper:
        raise ConfigError(
            "client-token pepper is not configured (set GOZAR_TOKEN_PEPPER)"
        )
    return settings.token_pepper.encode("utf-8")


def hash_secret(secret: str, *, settings: Settings | None = None) -> bytes:
    """Return the keyed HMAC-SHA256 digest of ``secret``.

    The HMAC key is the server-side pepper, so the stored digest is non-reversible
    and cannot be recomputed without the pepper (Requirement 8.2).
    """
    return hmac.new(_pepper(settings), secret.encode("utf-8"), sha256).digest()


def _format_token(id_prefix: str, secret: str) -> str:
    """Render the presentable token string ``gz-<id_prefix>-<secret>``."""
    return f"{_TOKEN_SCHEME}-{id_prefix}-{secret}"


def _secret_aad(id_prefix: str) -> bytes:
    """AAD binding encrypted token secrets to their lookup prefix."""
    return f"gozar-client-token:{id_prefix}".encode("utf-8")


def _has_reveal_secret(token: ClientToken) -> bool:
    """Return whether this token row carries recoverable encrypted secret material."""
    return (
        token.secret_ciphertext is not None
        and token.secret_nonce is not None
        and token.secret_wrapped_dek is not None
    )


def _encrypt_presentable_secret(
    token: ClientToken, presentable: str, *, settings: Settings
) -> None:
    """Store an encrypted copy of the full presentable API key on ``token``."""
    encrypted = encrypt(
        presentable.encode("utf-8"),
        aad=_secret_aad(token.id_prefix),
        settings=settings,
    )
    token.secret_ciphertext = encrypted.ciphertext
    token.secret_nonce = encrypted.nonce
    token.secret_wrapped_dek = encrypted.wrapped_dek


def _parse_token(presented: str) -> tuple[str, str] | None:
    """Parse ``gz-<id_prefix>-<secret>`` into ``(id_prefix, secret)``.

    Returns ``None`` for any malformed input. The split uses ``maxsplit=2`` so a
    secret containing ``-`` (token_urlsafe may emit it) is preserved intact; the
    ``id_prefix`` is hex and therefore never contains ``-``.
    """
    if not presented:
        return None
    parts = presented.split("-", 2)
    if len(parts) != 3:
        return None
    scheme, id_prefix, secret = parts
    if scheme != _TOKEN_SCHEME or not id_prefix or not secret:
        return None
    return id_prefix, secret


def _spec_from_row(row: TokenUsageLimit) -> UsageLimitSpec:
    """Reconstruct a :class:`UsageLimitSpec` from a persisted limit row."""
    return UsageLimitSpec(
        metric=LimitMetric(row.metric),
        limit_value=float(row.limit_value),
        capacity=float(row.capacity) if row.capacity is not None else None,
        window=LimitWindow(row.window),
    )


async def _get_token(session: AsyncSession, token_id: uuid.UUID) -> ClientToken:
    """Load a token by id or raise :class:`~gozar.core.errors.NotFound`."""
    token = await session.get(ClientToken, token_id)
    if token is None:
        raise NotFound("client token not found")
    return token


async def _require_chain(
    session: AsyncSession, chain_id: uuid.UUID | None
) -> RouteFallbackChain | None:
    """Return the selected chain, or raise ``NotFound`` for a stale/invalid id."""
    if chain_id is None:
        return None
    chain = await session.get(RouteFallbackChain, chain_id)
    if chain is None:
        raise NotFound("fallback chain not found")
    return chain


async def _upsert_limit(
    session: AsyncSession, token_id: uuid.UUID, limit: UsageLimitSpec
) -> None:
    """Insert or update the single usage-limit row for ``token_id``."""
    existing = await session.scalar(
        select(TokenUsageLimit).where(TokenUsageLimit.subject_id == token_id)
    )
    if existing is None:
        session.add(
            TokenUsageLimit(
                subject_kind="token",
                subject_id=token_id,
                metric=limit.metric.value,
                limit_value=limit.limit_value,
                capacity=limit.capacity,
                window=limit.window.value,
            )
        )
    else:
        existing.metric = limit.metric.value
        existing.limit_value = limit.limit_value
        existing.capacity = limit.capacity
        existing.window = limit.window.value
    await session.flush()


async def create_token(
    session: AsyncSession,
    label: str,
    limit: UsageLimitSpec | None = None,
    assigned_chain_id: uuid.UUID | None = None,
    *,
    settings: Settings | None = None,
) -> IssuedToken:
    """Issue a new Client_Token, returning the presentable secret.

    Generates a high-entropy secret and a unique non-secret ``id_prefix``, persists
    the HMAC needed for verification plus an envelope-encrypted copy of the full
    presentable key, optionally attaches a usage limit, and returns the full
    ``gz-<id_prefix>-<secret>`` string in :attr:`IssuedToken.secret`.
    """
    settings = settings or get_settings()
    await _require_chain(session, assigned_chain_id)

    id_prefix = secrets.token_hex(_ID_PREFIX_BYTES)
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    token_hash = hash_secret(secret, settings=settings)
    presentable = _format_token(id_prefix, secret)

    token = ClientToken(
        id_prefix=id_prefix,
        token_hash=token_hash,
        label=label,
        status=TokenStatus.ACTIVE.value,
        assigned_chain_id=assigned_chain_id,
    )
    _encrypt_presentable_secret(token, presentable, settings=settings)
    session.add(token)
    # Flush so the id is assigned and id_prefix uniqueness is enforced before the
    # limit row references it.
    await session.flush()

    if limit is not None:
        await _upsert_limit(session, token.id, limit)

    return IssuedToken(
        token_id=token.id,
        id_prefix=id_prefix,
        label=label,
        status=token.status,
        assigned_chain_id=token.assigned_chain_id,
        secret=presentable,
    )


async def verify(
    session: AsyncSession,
    presented: str,
    *,
    settings: Settings | None = None,
) -> TokenAuthResult | None:
    """Authenticate a presented token string.

    Parses ``presented``, looks up the row by ``id_prefix``, and compares the
    recomputed HMAC against the stored digest in constant time. Returns a
    :class:`TokenAuthResult` only when the comparison succeeds **and** the token is
    ``active``; returns ``None`` for malformed input, unknown prefix, hash mismatch,
    or a disabled/revoked token (Requirements 8.4, 9.3, 9.4).
    """
    parsed = _parse_token(presented)
    if parsed is None:
        return None
    id_prefix, secret = parsed

    token = await session.scalar(
        select(ClientToken).where(ClientToken.id_prefix == id_prefix)
    )
    if token is None:
        # Compare against a dummy digest to keep timing independent of existence.
        hmac.compare_digest(hash_secret(secret, settings=settings), b"\x00" * 32)
        return None

    candidate = hash_secret(secret, settings=settings)
    if not hmac.compare_digest(candidate, token.token_hash):
        return None
    if token.status != TokenStatus.ACTIVE.value:
        return None
    return TokenAuthResult(
        token_id=token.id,
        status=token.status,
        assigned_chain_id=token.assigned_chain_id,
    )


async def set_usage_limit(
    session: AsyncSession, token_id: uuid.UUID, limit: UsageLimitSpec
) -> None:
    """Persist (or replace) the Usage_Limit for a token (Requirement 9.1).

    The updated limit applies to subsequent requests using the token; evaluation
    always reads the most recently persisted spec.
    """
    await _get_token(session, token_id)
    await _upsert_limit(session, token_id, limit)


async def set_assigned_chain(
    session: AsyncSession, token_id: uuid.UUID, chain_id: uuid.UUID | None
) -> None:
    """Assign a token to a fallback chain, or clear the assignment.

    A ``None`` assignment preserves the historical auto-routing behavior: the
    gateway chooses a chain from the request model's selector, falling back to the
    first catch-all chain. A concrete chain id is validated before it is stored so
    the hot path does not carry dangling route references.
    """
    token = await _get_token(session, token_id)
    await _require_chain(session, chain_id)
    token.assigned_chain_id = chain_id
    await session.flush()


async def set_enabled(
    session: AsyncSession, token_id: uuid.UUID, enabled: bool
) -> None:
    """Enable or disable a token (Requirements 9.3, 9.4).

    Disabling moves an ``active`` token to ``disabled``; enabling moves a
    ``disabled`` token back to ``active``. A ``revoked`` token is terminal and cannot
    be re-enabled.
    """
    token = await _get_token(session, token_id)
    if token.status == TokenStatus.REVOKED.value:
        # Revocation is terminal; enabling/disabling does not resurrect it.
        return
    token.status = (
        TokenStatus.ACTIVE.value if enabled else TokenStatus.DISABLED.value
    )
    await session.flush()


async def revoke(session: AsyncSession, token_id: uuid.UUID) -> None:
    """Permanently revoke a token (Requirement 8.4).

    Subsequent requests presenting the token are rejected by :func:`verify`.
    Revocation is terminal.
    """
    token = await _get_token(session, token_id)
    token.status = TokenStatus.REVOKED.value
    await session.flush()


async def rotate_token(
    session: AsyncSession,
    token_id: uuid.UUID,
    *,
    settings: Settings | None = None,
) -> IssuedToken:
    """Issue a replacement token and revoke the old one.

    Rotation creates a new secret with the same label, routing chain, and usage
    limit, then makes the old token terminally revoked in the same transaction.
    """
    token = await _get_token(session, token_id)
    if token.status == TokenStatus.REVOKED.value:
        raise ValidationError("revoked API keys cannot be rotated")

    limit_row = await session.scalar(
        select(TokenUsageLimit).where(TokenUsageLimit.subject_id == token_id)
    )
    limit = _spec_from_row(limit_row) if limit_row is not None else None

    issued = await create_token(
        session,
        token.label,
        limit,
        token.assigned_chain_id,
        settings=settings,
    )
    token.status = TokenStatus.REVOKED.value
    await session.flush()
    return issued


async def reveal_token(
    session: AsyncSession,
    token_id: uuid.UUID,
    *,
    settings: Settings | None = None,
) -> IssuedToken:
    """Reveal the existing API key without rotating or revoking it.

    Only rows created after encrypted reveal support was introduced carry the
    encrypted presentable key. Verification continues to use ``token_hash``; this
    decrypt path exists only for password-confirmed operator reveal.
    """
    settings = settings or get_settings()
    token = await _get_token(session, token_id)
    if token.status == TokenStatus.REVOKED.value:
        raise ValidationError("revoked API keys cannot be revealed")
    if not _has_reveal_secret(token):
        raise ValidationError(
            "this API key was created before encrypted key reveal was enabled; "
            "paste the existing API key once to enable future reveal"
        )

    assert token.secret_ciphertext is not None
    assert token.secret_nonce is not None
    assert token.secret_wrapped_dek is not None
    presentable = decrypt(
        token.secret_ciphertext,
        token.secret_nonce,
        token.secret_wrapped_dek,
        aad=_secret_aad(token.id_prefix),
        settings=settings,
    ).decode("utf-8")

    parsed = _parse_token(presentable)
    if parsed is None:
        raise ValidationError("stored API key secret is malformed")
    id_prefix, secret = parsed
    if id_prefix != token.id_prefix or not hmac.compare_digest(
        hash_secret(secret, settings=settings),
        token.token_hash,
    ):
        raise ValidationError("stored API key secret does not match this token")

    return IssuedToken(
        token_id=token.id,
        id_prefix=token.id_prefix,
        label=token.label,
        status=token.status,
        assigned_chain_id=token.assigned_chain_id,
        secret=presentable,
    )


async def store_existing_token_secret(
    session: AsyncSession,
    token_id: uuid.UUID,
    presentable: str,
    *,
    settings: Settings | None = None,
) -> IssuedToken:
    """Verify and store an encrypted copy of an existing API key.

    Legacy rows created before encrypted reveal support cannot be decrypted because
    only their HMAC was stored. If the operator still has the full API key, this path
    verifies it against the existing row and stores it encrypted without changing the
    key, id, route, status, or usage.
    """
    settings = settings or get_settings()
    token = await _get_token(session, token_id)
    if token.status == TokenStatus.REVOKED.value:
        raise ValidationError("revoked API keys cannot be revealed")

    parsed = _parse_token(presentable)
    if parsed is None:
        raise ValidationError("presented API key is malformed")
    id_prefix, secret = parsed
    if id_prefix != token.id_prefix or not hmac.compare_digest(
        hash_secret(secret, settings=settings),
        token.token_hash,
    ):
        raise ValidationError("presented API key does not match this API key")

    _encrypt_presentable_secret(token, presentable, settings=settings)
    await session.flush()
    return IssuedToken(
        token_id=token.id,
        id_prefix=token.id_prefix,
        label=token.label,
        status=token.status,
        assigned_chain_id=token.assigned_chain_id,
        secret=presentable,
    )


async def list_tokens(
    session: AsyncSession,
    *,
    consumption_lookup: ConsumptionLookup | None = None,
) -> list[TokenView]:
    """Return secret-free views of all tokens (Requirement 8.3).

    Each view carries the token's non-secret lookup prefix, label, status,
    configured Usage_Limit (if any), and recorded usage. The secret value is never
    included. Recorded usage is read via ``consumption_lookup`` (the Usage_Recorder
    counters), which the router wires to a real Redis-backed reader; it defaults to
    zero so the service stays testable without Redis.
    """
    lookup = consumption_lookup or _zero_usage

    tokens = (
        await session.scalars(select(ClientToken).order_by(ClientToken.created_at))
    ).all()
    limit_rows = (await session.scalars(select(TokenUsageLimit))).all()
    limits_by_subject = {row.subject_id: row for row in limit_rows}
    assigned_chain_ids = {
        token.assigned_chain_id
        for token in tokens
        if token.assigned_chain_id is not None
    }
    chain_names_by_id: dict[uuid.UUID, str] = {}
    if assigned_chain_ids:
        chain_rows = (
            await session.scalars(
                select(RouteFallbackChain).where(
                    RouteFallbackChain.id.in_(assigned_chain_ids)
                )
            )
        ).all()
        chain_names_by_id = {row.id: row.name for row in chain_rows}

    views: list[TokenView] = []
    for token in tokens:
        limit_row = limits_by_subject.get(token.id)
        limit = _spec_from_row(limit_row) if limit_row is not None else None
        usage = await lookup(token.id, limit)
        views.append(
            TokenView(
                token_id=token.id,
                id_prefix=token.id_prefix,
                label=token.label,
                status=token.status,
                assigned_chain_id=token.assigned_chain_id,
                assigned_chain_name=(
                    chain_names_by_id.get(token.assigned_chain_id)
                    if token.assigned_chain_id is not None
                    else None
                ),
                limit=limit,
                usage=usage,
                can_reveal=_has_reveal_secret(token)
                and token.status != TokenStatus.REVOKED.value,
            )
        )
    return views


# Public surface of the Token_Authority service.
__all__ = [
    "IssuedToken",
    "TokenAuthResult",
    "TokenView",
    "ConsumptionLookup",
    "create_token",
    "verify",
    "set_assigned_chain",
    "set_usage_limit",
    "set_enabled",
    "revoke",
    "reveal_token",
    "store_existing_token_secret",
    "rotate_token",
    "list_tokens",
    "hash_secret",
]
