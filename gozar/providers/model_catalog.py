"""Runtime provider fallback model catalogs.

Provider live listings remain authoritative when they exist. This module manages
the editable fallback catalog used when live listing cannot be produced, especially
for subscription providers whose public/authenticated surface has no documented
``GET /models`` equivalent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gozar.core.config import Settings, get_settings
from gozar.core.errors import ValidationError
from gozar.core.models import ProviderModelCatalog
from gozar.providers.registry import coerce_provider_id


@dataclass(frozen=True)
class ProviderModelCatalogView:
    """Secret-free view of one provider's fallback model list."""

    provider: str
    models: list[str]
    source: str
    updated_at: datetime | None

    @property
    def model_count(self) -> int:
        return len(self.models)


def normalize_model_ids(model_ids: list[str]) -> list[str]:
    """Trim, de-duplicate, and validate operator-supplied model ids."""
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in model_ids:
        model_id = raw.strip()
        if not model_id:
            continue
        if any(char.isspace() for char in model_id):
            raise ValidationError(
                "model ids must not contain whitespace",
                details=[{"field": "models", "message": f"invalid model id {model_id!r}"}],
            )
        if model_id in seen:
            continue
        seen.add(model_id)
        normalized.append(model_id)
    return normalized


async def _runtime_row(
    session: AsyncSession, provider: str
) -> ProviderModelCatalog | None:
    return await session.get(ProviderModelCatalog, provider)


async def get_provider_model_ids(
    session: AsyncSession,
    provider: str,
    *,
    settings: Settings | None = None,
) -> list[str]:
    """Return the fallback models for ``provider``, preferring runtime DB overrides."""
    settings = settings or get_settings()
    row = await _runtime_row(session, provider)
    if row is not None:
        return row.safe_model_ids
    return list(settings.provider_models.get(provider, []))


async def get_provider_model_catalog(
    session: AsyncSession,
    provider: str,
    *,
    settings: Settings | None = None,
) -> ProviderModelCatalogView:
    """Return one provider fallback catalog, with source metadata."""
    settings = settings or get_settings()
    provider_id = coerce_provider_id(provider).value
    row = await _runtime_row(session, provider_id)
    if row is not None:
        return ProviderModelCatalogView(
            provider=provider_id,
            models=row.safe_model_ids,
            source="runtime",
            updated_at=row.updated_at,
        )
    return ProviderModelCatalogView(
        provider=provider_id,
        models=list(settings.provider_models.get(provider_id, [])),
        source="environment",
        updated_at=None,
    )


async def list_provider_model_catalogs(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
) -> list[ProviderModelCatalogView]:
    """Return fallback catalogs for providers known by config or runtime state."""
    settings = settings or get_settings()
    rows = (await session.scalars(select(ProviderModelCatalog))).all()
    providers = set(settings.provider_models) | {row.provider for row in rows}
    return [
        await get_provider_model_catalog(session, provider, settings=settings)
        for provider in sorted(providers)
    ]


async def set_provider_model_catalog(
    session: AsyncSession,
    provider: str,
    model_ids: list[str],
) -> ProviderModelCatalogView:
    """Replace a provider's runtime fallback model list."""
    provider_id = coerce_provider_id(provider).value
    normalized = normalize_model_ids(model_ids)
    row = await _runtime_row(session, provider_id)
    if row is None:
        row = ProviderModelCatalog(provider=provider_id, model_ids=normalized)
        session.add(row)
    else:
        row.model_ids = normalized
    await session.flush()
    return ProviderModelCatalogView(
        provider=provider_id,
        models=row.safe_model_ids,
        source="runtime",
        updated_at=row.updated_at,
    )


async def clear_provider_model_catalog(
    session: AsyncSession,
    provider: str,
    *,
    settings: Settings | None = None,
) -> ProviderModelCatalogView:
    """Remove a runtime fallback override and reveal the environment fallback."""
    settings = settings or get_settings()
    provider_id = coerce_provider_id(provider).value
    row = await _runtime_row(session, provider_id)
    if row is not None:
        await session.delete(row)
        await session.flush()
    return ProviderModelCatalogView(
        provider=provider_id,
        models=list(settings.provider_models.get(provider_id, [])),
        source="environment",
        updated_at=None,
    )


__all__ = [
    "ProviderModelCatalogView",
    "clear_provider_model_catalog",
    "get_provider_model_catalog",
    "get_provider_model_ids",
    "list_provider_model_catalogs",
    "set_provider_model_catalog",
]
