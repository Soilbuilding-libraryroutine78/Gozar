"""Provider registry.

The registry is the single seam that maps a :class:`ProviderId` to everything
the rest of the system needs to call that upstream Provider:

* its **base URL** (sourced from configuration; never hardcoded),
* its **auth style** (:class:`AuthStyle.subscription_oauth` vs
  :class:`AuthStyle.api_key`),
* a :class:`ProviderAdapter` (the translation adapter defined in the
  ``translation`` module), referenced *lazily* through an adapter-kind key and a
  registration hook so this module has no import-time dependency on
  ``translation`` and no circular import, and
* OAuth endpoint metadata (authorize/token URLs, client id, redirect, scopes)
  for subscription providers (``None`` for API-key providers).

Design notes
------------
The *structural* facts about a provider (which providers exist, whether they use
subscription OAuth or an API key, and which adapter shape they need) are intrinsic
to the provider and are declared here as :data:`_PROVIDER_SPECS`. Everything that
varies by deployment - base URLs and OAuth endpoint metadata - is read from
:class:`gozar.core.config.Settings` at lookup time. The registry **fails closed**:
requesting a provider whose base URL (or, for subscription providers, whose OAuth
metadata) is not configured raises a clear :class:`ConfigError` rather than
silently falling back to a default.

This module is intentionally import-light. It imports only from
:mod:`gozar.core.config` and :mod:`gozar.core.errors`. Translation adapters are
attached later (see :func:`register_adapter`) so that task 6.1 does not hard-depend
on the translation adapters (task 7.x) being implemented yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from gozar.core.config import Settings, get_settings
from gozar.core.errors import ConfigError, ValidationError


class ProviderId(str, Enum):
    """Identifiers for the upstream Providers Gozar can route to.

    Values are the keys used in the ``GOZAR_PROVIDER_BASE_URLS`` and
    ``GOZAR_PROVIDER_OAUTH`` configuration maps.
    """

    #: OpenAI / OpenAI-compatible pass-through (metered API key).
    OPENAI = "openai"
    #: OpenRouter - OpenAI-compatible pass-through (metered API key).
    OPENROUTER = "openrouter"
    #: ChatGPT subscription -> Codex backend (subscription OAuth).
    CODEX = "codex"
    #: Claude subscription -> Anthropic Messages API (subscription OAuth).
    ANTHROPIC = "anthropic"


class AuthStyle(str, Enum):
    """How Gozar authenticates to a Provider."""

    #: Subscription credential obtained via an OAuth authorization-code flow.
    SUBSCRIPTION_OAUTH = "subscription_oauth"
    #: Conventional metered API key.
    API_KEY = "api_key"


class AdapterKind(str, Enum):
    """Which translation adapter shape a Provider needs.

    This is a lightweight key into the adapter registration hook so that the
    provider registry can name an adapter without importing it (avoiding a
    circular dependency with the ``translation`` module).
    """

    #: Identity / pass-through for OpenAI-compatible Providers.
    OPENAI_COMPAT = "openai_compat"
    #: OpenAI Chat Completions <-> Codex Responses API.
    CODEX = "codex"
    #: OpenAI Chat Completions <-> Anthropic Messages API.
    ANTHROPIC = "anthropic"


@dataclass(frozen=True)
class OAuthEndpointMetadata:
    """OAuth endpoint metadata for a subscription Provider.

    Built from the provider's documented built-in defaults (see
    :data:`_OAUTH_DEFAULTS`) overlaid, field by field, with any values supplied in
    the ``GOZAR_PROVIDER_OAUTH`` configuration map so a deployment can override any
    individual endpoint without re-declaring the whole block.

    ``authorize_params`` carries provider-specific *extra* query parameters that the
    authorize redirect requires beyond the standard OAuth set (for example OpenAI's
    ``id_token_add_organizations`` / ``codex_cli_simplified_flow`` / ``originator``
    or Anthropic's ``code``); they are intrinsic to each provider's authorize
    contract. ``token_request_format`` selects how the token endpoint is called:
    ``"form"`` (standard RFC 6749 ``application/x-www-form-urlencoded``) or
    ``"json"`` (``application/json`` body), matching what each provider expects.
    ``include_state_in_token_exchange`` echoes the anti-CSRF ``state`` back in the
    authorization-code token exchange body, which Anthropic requires (Codex does not).
    """

    authorize_url: str
    token_url: str
    client_id: str
    redirect_uri: str
    scopes: tuple[str, ...]
    authorize_params: tuple[tuple[str, str], ...] = ()
    token_request_format: str = "form"
    include_state_in_token_exchange: bool = False


@dataclass(frozen=True)
class _ProviderSpec:
    """Intrinsic, deployment-independent facts about a Provider."""

    auth_style: AuthStyle
    adapter_kind: AdapterKind
    model_listing_path: str | None = None
    embeddings_path: str | None = None


# Structural definition of the supported Providers. Base URLs and OAuth metadata
# are NOT here - those are deployment configuration and are resolved from Settings
# when an entry is built.
_PROVIDER_SPECS: dict[ProviderId, _ProviderSpec] = {
    ProviderId.OPENAI: _ProviderSpec(
        AuthStyle.API_KEY,
        AdapterKind.OPENAI_COMPAT,
        "/models",
        "/embeddings",
    ),
    ProviderId.OPENROUTER: _ProviderSpec(
        AuthStyle.API_KEY,
        AdapterKind.OPENAI_COMPAT,
        "/models",
        "/embeddings",
    ),
    ProviderId.CODEX: _ProviderSpec(AuthStyle.SUBSCRIPTION_OAUTH, AdapterKind.CODEX),
    ProviderId.ANTHROPIC: _ProviderSpec(
        AuthStyle.SUBSCRIPTION_OAUTH, AdapterKind.ANTHROPIC
    ),
}


# --- Built-in, documented provider defaults ----------------------------------
# The well-known public endpoints for each supported provider. These are *not*
# deployment secrets - they are the published OAuth client ids, authorize/token
# URLs, redirect URIs, scopes, and API base URLs that the upstream providers
# require for subscription (ChatGPT/Codex and Claude) access. Declaring them here
# lets a fresh deployment begin a subscription connect without the operator having
# to rediscover provider client ids, while every value remains overridable through
# GOZAR_PROVIDER_BASE_URLS / GOZAR_PROVIDER_OAUTH (see _resolve_* below).
#
# Upstream API base URLs (joined with the per-provider wire path in
# gozar.gateway.upstream).
_BASE_URL_DEFAULTS: dict[ProviderId, str] = {
    ProviderId.OPENAI: "https://api.openai.com/v1",
    ProviderId.OPENROUTER: "https://openrouter.ai/api/v1",
    ProviderId.CODEX: "https://chatgpt.com/backend-api/codex",
    ProviderId.ANTHROPIC: "https://api.anthropic.com",
}

# Subscription-provider OAuth metadata. ``client_id`` values are the public OAuth
# client identifiers the providers' own first-party clients use for the
# authorization-code + PKCE flow; they are not secrets.
#
# IMPORTANT -- ``redirect_uri`` MUST stay the provider's registered loopback and must
# NOT be changed to Gozar's console domain. These public client ids only permit the
# loopback redirect (Codex http://localhost:1455/auth/callback, Anthropic
# http://localhost:53692/callback); OpenAI/Anthropic reject any other redirect_uri.
# Because Gozar is a web app served from an arbitrary origin and cannot receive that
# loopback callback, completion is manual/paste-based: the Operator opens the
# authorize URL, consents in their own browser, and pastes the resulting redirect URL
# back into the console (see gozar.accounts.service.extract_code_and_state /
# complete_subscription_connect). This keeps the connect flow working from ANY origin.
_OAUTH_DEFAULTS: dict[ProviderId, dict[str, Any]] = {
    # ChatGPT subscription -> OpenAI Codex backend.
    ProviderId.CODEX: {
        "authorize_url": "https://auth.openai.com/oauth/authorize",
        "token_url": "https://auth.openai.com/oauth/token",
        "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "redirect_uri": "http://localhost:1455/auth/callback",
        "scopes": ["openid", "profile", "email", "offline_access"],
        # Extra authorize params the Codex flow requires: surface the ChatGPT
        # account/organization in the issued token (so the account id claim is
        # present), use the simplified CLI consent flow, and identify the client.
        "authorize_params": {
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "originator": "codex_cli_rs",
        },
        "token_request_format": "form",
    },
    # Claude Pro/Max subscription -> Anthropic Messages API.
    ProviderId.ANTHROPIC: {
        "authorize_url": "https://claude.ai/oauth/authorize",
        "token_url": "https://platform.claude.com/v1/oauth/token",
        "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        "redirect_uri": "http://localhost:53692/callback",
        "scopes": [
            "org:create_api_key",
            "user:profile",
            "user:inference",
            "user:sessions:claude_code",
            "user:mcp_servers",
            "user:file_upload",
        ],
        # Anthropic's authorize endpoint expects ``code=true`` to drive the
        # authorization-code flow, exchanges the code via a JSON token request, and
        # echoes the anti-CSRF ``state`` back in that exchange body.
        "authorize_params": {"code": "true"},
        "token_request_format": "json",
        "include_state_in_token_exchange": True,
    },
}


# --- Adapter registration hook ----------------------------------------------
# Translation adapters (task 7.x) register a zero-argument factory here, keyed by
# AdapterKind. The registry resolves the adapter lazily via get_adapter() so this
# module never imports the translation package at import time.
_ADAPTER_FACTORIES: dict[AdapterKind, Callable[[], Any]] = {}


def register_adapter(kind: AdapterKind, factory: Callable[[], Any]) -> None:
    """Register a factory that builds the adapter for ``kind``.

    Called by the translation module when its adapters are available. The factory
    is invoked lazily by :func:`get_adapter`, so registration order does not matter
    and importing this module pulls in no translation code.
    """
    _ADAPTER_FACTORIES[kind] = factory


def get_adapter(kind: AdapterKind) -> Any:
    """Return the adapter instance for ``kind``.

    Raises :class:`ConfigError` (fail closed) if no adapter has been registered for
    ``kind`` yet, rather than returning ``None`` and letting a missing adapter fail
    obscurely deeper in the request path.
    """
    factory = _ADAPTER_FACTORIES.get(kind)
    if factory is None:
        raise ConfigError(
            f"no translation adapter registered for adapter kind {kind.value!r}"
        )
    return factory()


@dataclass(frozen=True)
class ProviderEntry:
    """A fully resolved registry entry for one Provider.

    Combines the Provider's intrinsic spec with deployment configuration (base URL
    and, for subscription Providers, OAuth metadata). The translation adapter is
    resolved lazily via :attr:`adapter` so building an entry never requires the
    translation module to be importable.
    """

    provider_id: ProviderId
    base_url: str
    auth_style: AuthStyle
    adapter_kind: AdapterKind
    oauth: OAuthEndpointMetadata | None
    model_listing_path: str | None
    embeddings_path: str | None

    @property
    def is_subscription(self) -> bool:
        """True when this Provider authenticates via subscription OAuth."""
        return self.auth_style is AuthStyle.SUBSCRIPTION_OAUTH

    @property
    def adapter(self) -> Any:
        """The translation adapter for this Provider (resolved lazily).

        Raises :class:`ConfigError` if the adapter has not been registered yet.
        """
        return get_adapter(self.adapter_kind)


def coerce_provider_id(value: str | ProviderId) -> ProviderId:
    """Coerce a string (e.g. from a request or config) into a :class:`ProviderId`.

    Raises :class:`ValidationError` for an unknown Provider so callers return a
    descriptive 400 rather than a raw ``ValueError``.
    """
    if isinstance(value, ProviderId):
        return value
    try:
        return ProviderId(value)
    except ValueError as exc:
        supported = ", ".join(sorted(p.value for p in ProviderId))
        raise ValidationError(
            f"unknown provider {value!r}; supported providers: {supported}"
        ) from exc


def _build_oauth_metadata(
    provider_id: ProviderId, raw: dict[str, Any]
) -> OAuthEndpointMetadata:
    """Build and validate OAuth metadata from a resolved config map (fail closed).

    ``raw`` is the built-in default for the provider overlaid with any configured
    overrides (see :func:`_resolve_oauth_raw`). Validation still fails closed if a
    required endpoint is absent, which protects a future provider that ships without
    a complete default.
    """
    required = ("authorize_url", "token_url", "client_id", "redirect_uri")
    missing = [key for key in required if not raw.get(key)]
    if missing:
        raise ConfigError(
            f"OAuth metadata for provider {provider_id.value!r} is missing required "
            f"field(s): {', '.join(missing)}"
        )
    scopes_raw = raw.get("scopes") or []
    if isinstance(scopes_raw, str):
        scopes = tuple(part for part in scopes_raw.split() if part)
    else:
        scopes = tuple(str(scope) for scope in scopes_raw)

    params_raw = raw.get("authorize_params") or {}
    if isinstance(params_raw, dict):
        authorize_params = tuple(
            (str(key), str(value)) for key, value in params_raw.items()
        )
    else:
        # Already a sequence of (key, value) pairs.
        authorize_params = tuple(
            (str(key), str(value)) for key, value in params_raw
        )

    token_request_format = str(raw.get("token_request_format") or "form").lower()
    if token_request_format not in ("form", "json"):
        raise ConfigError(
            f"OAuth metadata for provider {provider_id.value!r} has an unsupported "
            f"token_request_format {token_request_format!r}; expected 'form' or 'json'"
        )

    return OAuthEndpointMetadata(
        authorize_url=str(raw["authorize_url"]),
        token_url=str(raw["token_url"]),
        client_id=str(raw["client_id"]),
        redirect_uri=str(raw["redirect_uri"]),
        scopes=scopes,
        authorize_params=authorize_params,
        token_request_format=token_request_format,
        include_state_in_token_exchange=bool(
            raw.get("include_state_in_token_exchange", False)
        ),
    )


def _resolve_base_url(provider_id: ProviderId, settings: Settings) -> str | None:
    """Return the configured base URL, falling back to the built-in default.

    A non-empty value in ``GOZAR_PROVIDER_BASE_URLS`` overrides the documented
    default; otherwise the published default for the provider is used. Returns
    ``None`` only when neither is available (no known default and nothing configured).
    """
    configured = settings.provider_base_urls.get(provider_id.value)
    if configured:
        return str(configured)
    return _BASE_URL_DEFAULTS.get(provider_id)


def _resolve_oauth_raw(
    provider_id: ProviderId, settings: Settings
) -> dict[str, Any] | None:
    """Merge the built-in OAuth defaults with any configured override (per field).

    The built-in default (when present) is the base; each non-empty value supplied
    in ``GOZAR_PROVIDER_OAUTH[provider]`` overrides the corresponding field, so an
    operator can override just the ``client_id`` (or any single endpoint) without
    re-declaring the whole block, and a placeholder/empty value never clobbers a
    working default. Returns ``None`` when there is neither a default nor an override.
    """
    default = _OAUTH_DEFAULTS.get(provider_id)
    override = settings.provider_oauth.get(provider_id.value)
    if default is None and not override:
        return None
    merged: dict[str, Any] = dict(default or {})
    if override:
        for key, value in override.items():
            if value in (None, "", []):
                continue
            merged[key] = value
    return merged


def _build_entry(
    provider_id: ProviderId, spec: _ProviderSpec, settings: Settings
) -> ProviderEntry:
    """Resolve a :class:`ProviderEntry` from config + built-in defaults.

    Base URLs and OAuth metadata come from the published built-in defaults overlaid
    with any deployment configuration. Fails closed only when a value is genuinely
    unavailable (no default and nothing configured), preserving the registry's
    fail-closed guarantee for truly unconfigured providers.
    """
    base_url = _resolve_base_url(provider_id, settings)
    if not base_url:
        raise ConfigError(
            f"no base URL configured for provider {provider_id.value!r} and no "
            f"built-in default is available; set it in GOZAR_PROVIDER_BASE_URLS "
            f"before requesting this provider"
        )

    oauth: OAuthEndpointMetadata | None = None
    if spec.auth_style is AuthStyle.SUBSCRIPTION_OAUTH:
        raw_oauth = _resolve_oauth_raw(provider_id, settings)
        if not raw_oauth:
            raise ConfigError(
                f"no OAuth metadata configured for subscription provider "
                f"{provider_id.value!r} and no built-in default is available; set it "
                f"in GOZAR_PROVIDER_OAUTH before requesting this provider"
            )
        oauth = _build_oauth_metadata(provider_id, raw_oauth)

    return ProviderEntry(
        provider_id=provider_id,
        base_url=str(base_url),
        auth_style=spec.auth_style,
        adapter_kind=spec.adapter_kind,
        oauth=oauth,
        model_listing_path=spec.model_listing_path,
        embeddings_path=spec.embeddings_path,
    )


def get_provider(
    provider_id: str | ProviderId, settings: Settings | None = None
) -> ProviderEntry:
    """Return the resolved registry entry for ``provider_id``.

    The entry's base URL (and, for subscription providers, OAuth metadata) is read
    from configuration. Fails closed with a descriptive :class:`ConfigError` if the
    requested provider's required configuration is absent, and with a
    :class:`ValidationError` if the provider id itself is unknown.
    """
    pid = coerce_provider_id(provider_id)
    spec = _PROVIDER_SPECS[pid]
    settings = settings or get_settings()
    return _build_entry(pid, spec, settings)


def list_providers(settings: Settings | None = None) -> list[ProviderEntry]:
    """Return resolved entries for every Provider that is configured.

    A Provider is "configured" when it has a base URL in
    ``GOZAR_PROVIDER_BASE_URLS`` (and, for subscription Providers, valid OAuth
    metadata). Providers that are not yet configured are omitted rather than raising,
    so this can be used to advertise what the deployment can actually serve. Use
    :func:`get_provider` to fail closed when a specific provider is required.
    """
    settings = settings or get_settings()
    entries: list[ProviderEntry] = []
    for pid, spec in _PROVIDER_SPECS.items():
        try:
            entries.append(_build_entry(pid, spec, settings))
        except ConfigError:
            # Not configured for this deployment; skip rather than fail closed here.
            continue
    return entries


def supported_provider_ids() -> tuple[ProviderId, ...]:
    """Return all Provider ids the registry knows how to build, regardless of config."""
    return tuple(_PROVIDER_SPECS.keys())


def provider_supports_embeddings(provider_id: str | ProviderId) -> bool:
    """Return whether the provider exposes the OpenAI-compatible embeddings lane."""

    return _PROVIDER_SPECS[coerce_provider_id(provider_id)].embeddings_path is not None
