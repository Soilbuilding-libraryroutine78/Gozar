"""Session-affinity map and attempt-order assembly.

Session affinity lets related requests prefer the credential previously used for the
same session when it is still available (Requirement 12.2). The binding is stored in
Redis as a short-lived ``session id -> credential id`` map so it survives across
requests without occupying a database row, and expires automatically.

This module provides the stateful glue around the pure
:func:`gozar.routing.chains.evaluate_chain`:

* :func:`record_session_binding` writes the session's chosen credential to Redis with
  a configurable TTL.
* :func:`get_session_binding` reads it back (``None`` when absent/expired/malformed).
* :func:`get_attempt_order` resolves the session preference and delegates the actual
  ordering to :func:`evaluate_chain`.

The chain and the credential-state snapshot are supplied by the caller (the
Proxy_Gateway resolves the chain for the requested model and collects current
credential states); this module only adds the Redis-backed session preference.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from redis.asyncio import Redis

from gozar.core.config import Settings, get_settings
from gozar.core.redis import get_redis
from gozar.routing.chains import RoutingChain, RoutingTarget, evaluate_chain
from gozar.routing.state import CredentialState

# Redis key namespace for the session-affinity map.
_SESSION_KEY_PREFIX = "route:session:"


def _session_key(session_id: str) -> str:
    """Return the namespaced Redis key for a session's affinity binding."""
    return f"{_SESSION_KEY_PREFIX}{session_id}"


async def record_session_binding(
    session_id: str,
    account_id: UUID,
    *,
    redis: Redis | None = None,
    settings: Settings | None = None,
) -> None:
    """Bind ``session_id`` to ``account_id`` in the Redis session map.

    The binding is stored with a TTL of
    ``GOZAR_SESSION_AFFINITY_TTL_SECONDS`` so stale affinities expire on their own.
    Recording the credential actually used for a session lets a subsequent request in
    the same session prefer it while it remains available (Requirement 12.2).

    No-ops on an empty ``session_id`` (a request without a session identifier is an
    independent single-message request, Requirement 12.3).
    """
    if not session_id:
        return
    settings = settings or get_settings()
    client = redis or get_redis()
    await client.set(
        _session_key(session_id),
        str(account_id),
        ex=settings.session_affinity_ttl_seconds,
    )


async def get_session_binding(
    session_id: str | None,
    *,
    redis: Redis | None = None,
) -> UUID | None:
    """Return the credential bound to ``session_id``, or ``None``.

    Returns ``None`` when no session id is supplied, no binding exists (or it has
    expired), or the stored value is not a valid credential id.
    """
    if not session_id:
        return None
    client = redis or get_redis()
    raw = await client.get(_session_key(session_id))
    if not raw:
        return None
    try:
        return UUID(raw)
    except (ValueError, AttributeError):
        # Defensive: a malformed/legacy value must not break routing.
        return None


async def get_attempt_order(
    chain: RoutingChain,
    states: Mapping[UUID, CredentialState],
    session_id: str | None = None,
    *,
    redis: Redis | None = None,
) -> list[RoutingTarget]:
    """Return the ordered list of credential ids to attempt for a request.

    Resolves the session-affinity preference from Redis (when a ``session_id`` is
    supplied) and applies it on top of the pure chain evaluation. The resulting order
    skips every unavailable credential and, when the session-bound credential is
    available, tries it first (Requirements 11.1-11.3, 12.2).

    Args:
        chain: The resolved fallback chain for the requested model.
        states: Snapshot of current credential states keyed by credential id.
        session_id: Optional session identifier supplied by the Client_Application.
        redis: Optional Redis client (defaults to the process-wide client); injected
            in tests.

    Returns:
        The ordered, available credential ids to attempt (possibly empty).
    """
    session_pref = await get_session_binding(session_id, redis=redis)
    return evaluate_chain(chain, states, session_pref)
