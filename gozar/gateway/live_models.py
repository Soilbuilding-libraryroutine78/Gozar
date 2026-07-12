"""Live model-listing lookups for Providers that expose a real ``GET /models`` API.

This is the network seam :mod:`gozar.gateway.catalog` calls to ask a Provider what
models it currently serves, instead of relying solely on the deployment-configured
``GOZAR_PROVIDER_MODELS`` fallback. Only Providers whose wire protocol actually
publishes such an endpoint are handled here (see
:data:`gozar.gateway.catalog._LIVE_LISTING_PROVIDERS`); this module never guesses at
an endpoint a Provider does not document.

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
from gozar.providers.registry import get_provider

def _extract_model_ids(payload: Any) -> list[str] | None:
    """Parse an OpenAI-shaped model-listing response into a list of model ids.

    Returns ``None`` when the payload is not the expected ``{"data": [{"id": ...}]}``
    shape, so the caller treats it the same as any other lookup failure.
    """
    if not isinstance(payload, dict):
        return None
    entries = payload.get("data")
    if not isinstance(entries, list):
        return None
    ids = [entry["id"] for entry in entries if isinstance(entry, dict) and entry.get("id")]
    return ids or None


async def fetch_live_models(
    provider: str,
    material: ProviderCredentialMaterial,
    *,
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
    try:
        async with UpstreamClient(entry, settings=settings) as client:
            response = await client.request(
                "GET",
                entry.model_listing_path,
                headers=headers,
            )
            payload = response.json()
    except (GozarError, ValueError):
        # GozarError covers UpstreamError from the client; ValueError covers a
        # non-JSON body. Both are ordinary "live lookup unavailable" outcomes.
        return None

    return _extract_model_ids(payload)


__all__ = ["fetch_live_models"]
