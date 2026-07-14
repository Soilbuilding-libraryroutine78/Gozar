"""Live model-listing lookups for Providers that expose a real ``GET /models`` API.

This is the network seam :mod:`gozar.gateway.catalog` calls to ask a Provider what
models it currently serves, instead of relying solely on the deployment-configured
``GOZAR_PROVIDER_MODELS`` fallback. Only Providers whose wire protocol actually
publishes such an endpoint are handled here (see the Provider registry); this
module never guesses at an endpoint a Provider does not document. Discovery is
request-lane aware: OpenRouter is queried with its documented embedding-output
filter, while providers that return basic OpenAI model cards are classified by
their model identifiers.

Every failure mode -- no usable credential, a network error, a non-2xx response, or
a response shaped unexpectedly -- returns ``None`` rather than raising, so the
catalog can fall back to the configured list without the models listing ever
breaking the ``GET /v1/models`` endpoint for a Client_Application.
"""

from __future__ import annotations

from typing import Any

from gozar.accounts.service import ProviderCredentialMaterial
from gozar.core.config import Settings, get_settings
from gozar.core.errors import GozarError
from gozar.providers.client import UpstreamClient
from gozar.providers.registry import ProviderId, get_provider
from gozar.routing.chains import RouteKind


def _is_embedding_model_id(model_id: str) -> bool:
    """Identify embedding families when a provider exposes only basic model cards.

    OpenAI's Models API publishes availability but no endpoint-capability field.
    Its embedding model families include ``embed`` in the model id. OpenRouter's
    richer response is classified from ``architecture.output_modalities`` first,
    with this id check retained as a fallback for configured/offline catalogs.
    """

    return "embed" in model_id.casefold()


def _entry_is_embedding_model(entry: dict[str, Any]) -> bool:
    architecture = entry.get("architecture")
    if isinstance(architecture, dict):
        modalities = architecture.get("output_modalities")
        if isinstance(modalities, list) and modalities:
            normalized = {str(value).casefold() for value in modalities}
            return bool({"embedding", "embeddings"} & normalized)
    model_id = entry.get("id")
    return isinstance(model_id, str) and _is_embedding_model_id(model_id)


def filter_model_ids_for_route(
    model_ids: list[str], route_kind: RouteKind
) -> list[str]:
    """Filter basic/fallback model ids for one Gozar request lane."""

    if route_kind is RouteKind.EMBEDDINGS:
        return [model_id for model_id in model_ids if _is_embedding_model_id(model_id)]
    return [model_id for model_id in model_ids if not _is_embedding_model_id(model_id)]


def _extract_model_ids(
    payload: Any, route_kind: RouteKind = RouteKind.CHAT
) -> list[str] | None:
    """Parse an OpenAI-shaped model-listing response into a list of model ids.

    Returns ``None`` when the payload is not the expected ``{"data": [{"id": ...}]}``
    shape, so the caller treats it the same as any other lookup failure.
    """
    if not isinstance(payload, dict):
        return None
    entries = payload.get("data")
    if not isinstance(entries, list):
        return None
    ids: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        is_embedding = _entry_is_embedding_model(entry)
        if route_kind is RouteKind.EMBEDDINGS and not is_embedding:
            continue
        if route_kind is RouteKind.CHAT and is_embedding:
            continue
        ids.append(entry["id"])
    return ids or None


async def fetch_live_models(
    provider: str,
    material: ProviderCredentialMaterial,
    *,
    route_kind: RouteKind = RouteKind.CHAT,
    settings: Settings | None = None,
) -> list[str] | None:
    """Fetch a Provider's currently served model ids via its live listing endpoint.

    Authenticates with ``material``'s API key (a live listing is only attempted for
    API_Key_Account credentials; see the catalog). Returns ``None`` on any failure --
    a missing key, a network/upstream error, or an unparsable response -- so the
    caller falls back to the configured model list without surfacing an error to the
    Client_Application.
    """
    if not material.api_key:
        return None

    settings = settings or get_settings()
    try:
        entry = get_provider(provider, settings=settings)
    except GozarError:
        return None
    if entry.model_listing_path is None:
        return None

    headers = {"Authorization": f"Bearer {material.api_key}"}
    params = (
        {"output_modalities": "embeddings"}
        if provider == ProviderId.OPENROUTER.value
        and route_kind is RouteKind.EMBEDDINGS
        else None
    )
    try:
        async with UpstreamClient(entry, settings=settings) as client:
            response = await client.request(
                "GET",
                entry.model_listing_path,
                headers=headers,
                params=params,
            )
            payload = response.json()
    except (GozarError, ValueError):
        # GozarError covers UpstreamError from the client; ValueError covers a
        # non-JSON body. Both are ordinary "live lookup unavailable" outcomes.
        return None

    return _extract_model_ids(payload, route_kind)


__all__ = ["fetch_live_models", "filter_model_ids_for_route"]
