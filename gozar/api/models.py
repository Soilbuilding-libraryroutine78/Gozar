"""Admin model catalog router.

The OpenAI-compatible ``GET /v1/models`` endpoint is app-facing and scoped by the
presented Gozar API key. This router exposes the same catalog logic to the
operator console, grouped by connected account and saved fallback chain so the UI
can explain where each model comes from without ever revealing provider secrets.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from gozar.accounts.service import list_accounts
from gozar.api.schemas import (
    ModelCatalogAccountResponse,
    ChainIssueResponse,
    ModelCatalogChainResponse,
    ModelCatalogResponse,
    ProviderModelCatalogResponse,
    UpdateProviderModelsRequest,
)
from gozar.auth.rbac import Identity, Permission, require
from gozar.core.db import get_session
from gozar.gateway.catalog import list_available_models
from gozar.providers.model_catalog import (
    clear_provider_model_catalog,
    list_provider_model_catalogs,
    set_provider_model_catalog,
)
from gozar.routing.service import list_chains
from gozar.routing.health import assess_chain_health
from gozar.translation.types import OpenAIModelCard

router = APIRouter(prefix="/models", tags=["models"])

# The catalog names accounts and their provider/kind/status, so it is guarded by
# the same manage-accounts permission as the account inventory.
_guard = require(Permission.MANAGE_ACCOUNTS)


def _merge_model_cards(
    groups: list[list[OpenAIModelCard]],
) -> list[OpenAIModelCard]:
    """Merge account-scoped catalogs in route order, keeping the first model card."""

    merged: list[OpenAIModelCard] = []
    seen: set[str] = set()
    for group in groups:
        for model in group:
            if model.id in seen:
                continue
            seen.add(model.id)
            merged.append(model)
    return merged


@router.get(
    "",
    summary="List models reachable by connected accounts and fallback chains",
    response_model=ModelCatalogResponse,
)
async def list_model_catalog_route(
    request: Request,
    refresh: bool = False,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> ModelCatalogResponse:
    """Return the current model catalog grouped for the admin console.

    ``refresh=true`` bypasses the Redis live-model cache for providers with a
    real listing endpoint (OpenAI/OpenRouter) while still falling back to
    ``GOZAR_PROVIDER_MODELS`` when a live lookup cannot be produced.
    """
    settings = request.app.state.settings
    generated_at = datetime.now(timezone.utc)

    account_views = await list_accounts(session)
    account_responses: list[ModelCatalogAccountResponse] = []
    account_models: dict[uuid.UUID, frozenset[str]] = {}
    account_cards: dict[uuid.UUID, list[OpenAIModelCard]] = {}
    for account in account_views:
        listing = await list_available_models(
            session,
            settings=settings,
            account_ids=[account.account_id],
            refresh=refresh,
        )
        account_cards[account.account_id] = list(listing.data)
        account_responses.append(
            ModelCatalogAccountResponse(
                account_id=account.account_id,
                label=account.label,
                provider=account.provider,
                kind=account.kind.value,
                status=account.status.value,
                model_count=len(listing.data),
                models=listing.data,
            )
        )
        account_models[account.account_id] = frozenset(model.id for model in listing.data)

    global_models = _merge_model_cards(
        [account_cards[account.account_id] for account in account_views]
    )

    accounts_by_id = {account.account_id: account for account in account_views}

    chain_views = await list_chains(session)
    chain_responses: list[ModelCatalogChainResponse] = []
    for chain in chain_views:
        account_ids = [entry.account_id for entry in chain.entries]
        chain_models = _merge_model_cards(
            [account_cards.get(account_id, []) for account_id in account_ids]
        )
        health = assess_chain_health(chain, accounts_by_id, account_models)
        chain_responses.append(
            ModelCatalogChainResponse(
                chain_id=chain.chain_id,
                name=chain.name,
                model_selector=chain.model_selector,
                entry_count=len(chain.entries),
                model_count=len(chain_models),
                models=chain_models,
                health=health.status,
                issues=[
                    ChainIssueResponse(
                        code=issue.code,
                        message=issue.message,
                        position=issue.position,
                        account_id=issue.account_id,
                        model=issue.model_id,
                    )
                    for issue in health.issues
                ],
            )
        )
    provider_responses = [
        ProviderModelCatalogResponse.from_view(view)
        for view in await list_provider_model_catalogs(session, settings=settings)
    ]

    return ModelCatalogResponse(
        generated_at=generated_at,
        cache_ttl_seconds=settings.provider_models_cache_ttl_seconds,
        refreshed=refresh,
        model_count=len(global_models),
        models=global_models,
        accounts=account_responses,
        chains=chain_responses,
        providers=provider_responses,
        unhealthy_chain_count=sum(
            chain.health != "healthy" for chain in chain_responses
        ),
    )


@router.put(
    "/providers/{provider}",
    summary="Replace a provider fallback model catalog without restarting",
    response_model=ProviderModelCatalogResponse,
)
async def update_provider_models_route(
    provider: str,
    payload: UpdateProviderModelsRequest,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> ProviderModelCatalogResponse:
    """Persist a runtime provider fallback catalog.

    The change takes effect immediately for ``/api/models`` and ``/v1/models``.
    Providers with live model-listing support still prefer live data; this fallback
    is used when live listing is unavailable or not documented for that provider.
    """
    view = await set_provider_model_catalog(session, provider, payload.models)
    return ProviderModelCatalogResponse.from_view(view)


@router.delete(
    "/providers/{provider}",
    summary="Reset a provider fallback model catalog to environment defaults",
    response_model=ProviderModelCatalogResponse,
)
async def reset_provider_models_route(
    request: Request,
    provider: str,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(_guard),
) -> ProviderModelCatalogResponse:
    """Remove the runtime override and reveal ``GOZAR_PROVIDER_MODELS`` again."""
    view = await clear_provider_model_catalog(
        session,
        provider,
        settings=request.app.state.settings,
    )
    return ProviderModelCatalogResponse.from_view(view)


__all__ = ["router"]
