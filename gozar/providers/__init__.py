"""Provider registry and per-provider upstream clients."""

from gozar.providers.client import UpstreamClient
from gozar.providers.registry import (
    AdapterKind,
    AuthStyle,
    OAuthEndpointMetadata,
    ProviderEntry,
    ProviderId,
    coerce_provider_id,
    get_adapter,
    get_provider,
    list_providers,
    provider_supports_embeddings,
    register_adapter,
    supported_provider_ids,
)

__all__ = [
    "AdapterKind",
    "AuthStyle",
    "OAuthEndpointMetadata",
    "ProviderEntry",
    "ProviderId",
    "coerce_provider_id",
    "get_adapter",
    "get_provider",
    "list_providers",
    "provider_supports_embeddings",
    "register_adapter",
    "supported_provider_ids",
    "UpstreamClient",
]
