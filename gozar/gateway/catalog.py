"""Model catalog for the OpenAI-compatible ``GET /v1/models`` surface.

Advertises the models a Client_Application can request through Gozar: the union of
the models reachable via **connected, available** Upstream_Credentials. A credential
is *available* when it is enabled, does not require reauthorization, and has not been
deleted (the same availability notion the Flow_Controller uses for routing); the
Account_Manager's :func:`gozar.accounts.service.list_accounts` already excludes
deleted credentials, and an :class:`~gozar.accounts.models.CredentialStatus.ACTIVE`
status captures "enabled and not awaiting reauthorization".

Two sources feed a Provider's advertised models, tried in order:

1. **Live listing** (:data:`LiveModelsFetcher`) -- for a Provider whose wire
   protocol exposes a real model-listing endpoint (OpenAI and OpenAI-compatible
   Providers such as OpenRouter serve ``GET /models``), Gozar calls it with one of
   the Provider's connected credentials and caches the result in Redis for
   ``GOZAR_PROVIDER_MODELS_CACHE_TTL_SECONDS`` so the catalog stays current without
   an upstream call on every request.
2. **Configured fallback** (``GOZAR_PROVIDER_MODELS``) -- used whenever the live
   listing is unavailable: the Provider has no live endpoint (for example Codex,
   whose ChatGPT-subscription backend publishes no model-listing API), the call
   failed, or no credential was available to make it. This keeps the catalog
   working even when a live lookup cannot be made, and is the *only* source for
   Providers without a live endpoint.

Model identifiers are **never hardcoded** in this module: the live listing comes
from the Provider itself, and the fallback list is deployment configuration.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Sequence
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from gozar.accounts.models import CredentialKind, CredentialStatus
from gozar.accounts.service import (
    ProviderCredentialMaterial,
    get_usable_token,
    list_accounts,
)
from gozar.core.config import Settings, get_settings
from gozar.core.errors import NotFound
from gozar.core.logging import get_logger
from gozar.core.redis import get_redis
from gozar.gateway.live_models import fetch_live_models, filter_model_ids_for_route
from gozar.providers.model_catalog import get_provider_model_ids
from gozar.providers.registry import get_provider, provider_supports_embeddings
from gozar.routing.chains import RouteKind
from gozar.routing.service import get_chain
from gozar.tokens.models import ClientToken
from gozar.translation.types import OpenAIModelCard, OpenAIModelList

_logger = get_logger(__name__)

# Injectable live model-listing fetch. Given a provider id and its decrypted
# credential material, returns the provider's currently served model ids, or
# ``None`` when a live listing cannot be produced (mirrors the ``ValidateFn`` /
# ``RefreshFn`` injection seams in :mod:`gozar.accounts.service`). Defaults to
# :func:`gozar.gateway.live_models.fetch_live_models`; tests inject a fake so the
# catalog never makes a real network call.
class LiveModelsFetcher(Protocol):
    """Injectable operation-aware provider model listing."""

    def __call__(
        self,
        provider: str,
        material: ProviderCredentialMaterial,
        *,
        route_kind: RouteKind,
    ) -> Awaitable[list[str] | None]: ...

# Redis key namespace for the cached live model listing. The key includes both
# provider and credential id because different API keys for the same provider can
# have different model access (OpenRouter orgs, OpenAI project scoping, etc.).
_CACHE_KEY_PREFIX = "gw:models_cache:v2:"


def _cache_key(
    provider: str, account_id: uuid.UUID, route_kind: RouteKind
) -> str:
    return f"{_CACHE_KEY_PREFIX}{route_kind.value}:{provider}:{account_id}"


async def _cached_live_models(
    provider: str,
    account_id: uuid.UUID,
    *,
    session: AsyncSession,
    redis: Redis | None,
    settings: Settings,
    fetch_live: LiveModelsFetcher,
    route_kind: RouteKind,
    refresh: bool = False,
) -> list[str] | None:
    """Return a Provider's live model ids, using the Redis cache when fresh.

    Returns ``None`` (rather than raising) whenever a live listing cannot be
    produced -- no cached entry and the fetch fails, or Redis itself is
    unreachable -- so the caller falls back to the configured list. The Redis
    cache is a performance nicety here (like the consumption-counter reads in the
    accounts view), not a correctness requirement, so its unavailability degrades
    the catalog rather than failing it.
    """
    ttl = settings.provider_models_cache_ttl_seconds
    key = _cache_key(provider, account_id, route_kind)

    if redis is not None and ttl > 0 and not refresh:
        try:
            cached = await redis.get(key)
        except (RedisError, OSError) as exc:
            _logger.warning(
                "models cache unavailable; fetching live",
                extra={"provider": provider, "error": str(exc)},
            )
            cached = None
        if cached is not None:
            try:
                return json.loads(cached)
            except (TypeError, ValueError):
                pass  # Corrupt cache entry; fall through and re-fetch live.

    material = await get_usable_token(session, account_id, settings=settings)
    model_ids = await fetch_live(provider, material, route_kind=route_kind)
    if model_ids is None:
        return None

    if redis is not None and ttl > 0:
        try:
            await redis.set(key, json.dumps(model_ids), ex=ttl)
        except (RedisError, OSError) as exc:
            _logger.warning(
                "failed to cache live models",
                extra={"provider": provider, "error": str(exc)},
            )
    return model_ids


async def list_available_models(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
    redis: Redis | None = None,
    fetch_live: LiveModelsFetcher | None = None,
    account_ids: Sequence[uuid.UUID] | None = None,
    route_kind: RouteKind = RouteKind.CHAT,
    refresh: bool = False,
) -> OpenAIModelList:
    """Return the OpenAI-shaped models listing for currently reachable Providers.

    Builds the set of Providers that have at least one connected, available
    Upstream_Credential. For each, tries a live model listing (cached in Redis) and
    falls back to the deployment-configured list (``GOZAR_PROVIDER_MODELS``) when no
    live listing is available. Each model's ``created`` timestamp is the earliest
    connection time among that Provider's available credentials, and ``owned_by`` is
    the Provider id. Models are de-duplicated across Providers, first occurrence wins.
    """
    settings = settings or get_settings()
    if redis is None:
        try:
            redis = get_redis()
        except RuntimeError:
            # No GOZAR_REDIS_URL configured; the cache is skipped and every live
            # lookup below is simply re-fetched each call.
            redis = None
    fetch = fetch_live or (
        lambda provider, material, *, route_kind: fetch_live_models(
            provider,
            material,
            route_kind=route_kind,
            settings=settings,
        )
    )
    accounts = await list_accounts(session)
    if account_ids is not None:
        account_order = {account_id: index for index, account_id in enumerate(account_ids)}
        accounts = [
            account for account in accounts if account.account_id in account_order
        ]
        accounts.sort(
            key=lambda account: (account_order[account.account_id], account.connected_at)
        )

    cards: list[OpenAIModelCard] = []
    seen: set[str] = set()
    for account in accounts:
        if account.status is not CredentialStatus.ACTIVE:
            continue
        if (
            route_kind is RouteKind.EMBEDDINGS
            and not provider_supports_embeddings(account.provider)
        ):
            continue

        model_ids: list[str] | None = None
        provider_entry = get_provider(account.provider, settings=settings)
        if (
            provider_entry.model_listing_path is not None
            and account.kind is CredentialKind.API_KEY
        ):
            model_ids = await _cached_live_models(
                account.provider,
                account.account_id,
                session=session,
                redis=redis,
                settings=settings,
                fetch_live=fetch,
                route_kind=route_kind,
                refresh=refresh,
            )
        if model_ids is None:
            model_ids = await get_provider_model_ids(
                session, account.provider, settings=settings
            )
            model_ids = filter_model_ids_for_route(model_ids, route_kind)

        for model_id in model_ids:
            if model_id in seen:
                continue
            seen.add(model_id)
            cards.append(
                OpenAIModelCard(
                    id=model_id,
                    created=int(account.connected_at.timestamp()),
                    owned_by=account.provider,
                )
            )

    return OpenAIModelList(data=cards)


async def list_available_models_for_token(
    session: AsyncSession,
    token_id: uuid.UUID,
    *,
    settings: Settings | None = None,
    redis: Redis | None = None,
    fetch_live: LiveModelsFetcher | None = None,
    refresh: bool = False,
) -> OpenAIModelList:
    """Return the models reachable through the route assigned to one API key.

    A pinned key sees only the providers in its Fallback_Chain. An unpinned key keeps
    the legacy auto-routing catalog, matching ``GET /v1/models``. This lets the
    console generate copy-paste examples with a model that the selected key can
    actually serve.
    """
    token = await session.get(ClientToken, token_id)
    if token is None:
        raise NotFound("client token not found")

    if token.assigned_chain_id is None:
        return await list_available_models(
            session,
            settings=settings,
            redis=redis,
            fetch_live=fetch_live,
            refresh=refresh,
        )

    chain = await get_chain(session, token.assigned_chain_id)
    return await list_available_models(
        session,
        settings=settings,
        redis=redis,
        fetch_live=fetch_live,
        account_ids=[
            entry.account_id
            for entry in chain.entries
            if entry.route_kind is RouteKind.CHAT
        ],
        refresh=refresh,
    )


__all__ = ["list_available_models", "list_available_models_for_token"]
