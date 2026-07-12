"""Unit tests for the provider registry.

These verify that registry entries build from a sample configuration, that the
registry fails closed when required configuration is missing, and that the adapter
slot is resolved lazily through the registration hook (so the registry does not
hard-depend on the translation adapters).
"""

from __future__ import annotations

import pytest

from gozar.core.config import Settings
from gozar.core.errors import ConfigError, ValidationError
from gozar.providers import registry
from gozar.providers.registry import (
    AdapterKind,
    AuthStyle,
    OAuthEndpointMetadata,
    ProviderId,
)


def _sample_settings(**overrides) -> Settings:
    """Build a Settings instance from an explicit sample config (no real env)."""
    base = {
        "provider_base_urls": {
            "openai": "https://api.openai.com/v1",
            "openrouter": "https://openrouter.ai/api/v1",
            "codex": "https://chatgpt.com/backend-api/codex",
            "anthropic": "https://api.anthropic.com",
        },
        "provider_oauth": {
            "codex": {
                "authorize_url": "https://auth.openai.com/oauth/authorize",
                "token_url": "https://auth.openai.com/oauth/token",
                "client_id": "sample-client",
                "redirect_uri": "http://127.0.0.1:1455/auth/callback",
                "scopes": ["openid", "profile", "email"],
            },
            "anthropic": {
                "authorize_url": "https://claude.ai/oauth/authorize",
                "token_url": "https://console.anthropic.com/v1/oauth/token",
                "client_id": "sample-anthropic-client",
                "redirect_uri": "http://127.0.0.1:1455/auth/callback",
                "scopes": ["org:create_api_key", "user:profile"],
            },
        },
    }
    base.update(overrides)
    return Settings(**base)


def test_api_key_provider_entry_builds_from_config():
    settings = _sample_settings()
    entry = registry.get_provider(ProviderId.OPENAI, settings=settings)

    assert entry.provider_id is ProviderId.OPENAI
    assert entry.base_url == "https://api.openai.com/v1"
    assert entry.auth_style is AuthStyle.API_KEY
    assert entry.adapter_kind is AdapterKind.OPENAI_COMPAT
    assert entry.oauth is None
    assert entry.is_subscription is False


def test_openrouter_is_openai_compatible_passthrough():
    settings = _sample_settings()
    entry = registry.get_provider("openrouter", settings=settings)

    assert entry.auth_style is AuthStyle.API_KEY
    assert entry.adapter_kind is AdapterKind.OPENAI_COMPAT


def test_subscription_provider_entry_includes_oauth_metadata():
    settings = _sample_settings()
    entry = registry.get_provider(ProviderId.CODEX, settings=settings)

    assert entry.auth_style is AuthStyle.SUBSCRIPTION_OAUTH
    assert entry.adapter_kind is AdapterKind.CODEX
    assert entry.is_subscription is True
    assert isinstance(entry.oauth, OAuthEndpointMetadata)
    assert entry.oauth.authorize_url == "https://auth.openai.com/oauth/authorize"
    assert entry.oauth.token_url == "https://auth.openai.com/oauth/token"
    assert entry.oauth.scopes == ("openid", "profile", "email")


def test_string_provider_id_is_coerced():
    settings = _sample_settings()
    entry = registry.get_provider("anthropic", settings=settings)
    assert entry.provider_id is ProviderId.ANTHROPIC


def test_unknown_provider_raises_validation_error():
    settings = _sample_settings()
    with pytest.raises(ValidationError):
        registry.get_provider("does-not-exist", settings=settings)


def test_missing_base_url_uses_builtin_default():
    settings = _sample_settings(provider_base_urls={"openai": "https://api.openai.com/v1"})
    # codex base url is absent from config -> falls back to the built-in default.
    entry = registry.get_provider(ProviderId.CODEX, settings=settings)
    assert entry.base_url == "https://chatgpt.com/backend-api/codex"


def test_subscription_missing_oauth_uses_builtin_defaults():
    settings = _sample_settings(provider_oauth={})
    entry = registry.get_provider(ProviderId.CODEX, settings=settings)
    assert entry.oauth is not None
    # The published Codex OAuth client id and endpoints are supplied as defaults.
    assert entry.oauth.client_id == "app_EMoamEEZ73f0CkXaXp7hrann"
    assert entry.oauth.authorize_url == "https://auth.openai.com/oauth/authorize"
    assert entry.oauth.token_url == "https://auth.openai.com/oauth/token"
    assert entry.oauth.redirect_uri == "http://localhost:1455/auth/callback"
    assert "offline_access" in entry.oauth.scopes


def test_codex_builtin_oauth_defaults_carry_authorize_params_and_format():
    settings = _sample_settings(provider_oauth={})
    entry = registry.get_provider(ProviderId.CODEX, settings=settings)
    assert entry.oauth is not None
    params = dict(entry.oauth.authorize_params)
    assert params["id_token_add_organizations"] == "true"
    assert params["codex_cli_simplified_flow"] == "true"
    assert params["originator"] == "codex_cli_rs"
    assert entry.oauth.token_request_format == "form"


def test_anthropic_builtin_oauth_defaults():
    settings = _sample_settings(provider_oauth={}, provider_base_urls={})
    entry = registry.get_provider(ProviderId.ANTHROPIC, settings=settings)
    assert entry.base_url == "https://api.anthropic.com"
    assert entry.oauth is not None
    assert entry.oauth.client_id == "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    assert entry.oauth.authorize_url == "https://claude.ai/oauth/authorize"
    assert entry.oauth.token_url == "https://platform.claude.com/v1/oauth/token"
    assert entry.oauth.redirect_uri == "http://localhost:53692/callback"
    assert "user:inference" in entry.oauth.scopes
    # Anthropic exchanges the code via a JSON token request and needs code=true.
    assert entry.oauth.token_request_format == "json"
    assert dict(entry.oauth.authorize_params)["code"] == "true"
    # Anthropic echoes the anti-CSRF state back in the token exchange body.
    assert entry.oauth.include_state_in_token_exchange is True


def test_codex_does_not_include_state_in_token_exchange():
    settings = _sample_settings(provider_oauth={})
    entry = registry.get_provider(ProviderId.CODEX, settings=settings)
    assert entry.oauth is not None
    assert entry.oauth.include_state_in_token_exchange is False


def test_oauth_override_is_merged_per_field_over_defaults():
    # Operator overrides only the client id; the rest comes from the built-in default.
    settings = _sample_settings(provider_oauth={"codex": {"client_id": "my-own-id"}})
    entry = registry.get_provider(ProviderId.CODEX, settings=settings)
    assert entry.oauth is not None
    assert entry.oauth.client_id == "my-own-id"
    assert entry.oauth.token_url == "https://auth.openai.com/oauth/token"


def test_base_url_override_wins_over_default():
    settings = _sample_settings(
        provider_base_urls={"anthropic": "https://anthropic.internal/proxy"}
    )
    entry = registry.get_provider(ProviderId.ANTHROPIC, settings=settings)
    assert entry.base_url == "https://anthropic.internal/proxy"


def test_oauth_metadata_missing_required_field_fails_closed():
    # _build_oauth_metadata fails closed when a required endpoint is absent from the
    # fully resolved (default + override) metadata.
    with pytest.raises(ConfigError):
        registry._build_oauth_metadata(
            ProviderId.CODEX,
            {
                "authorize_url": "https://auth.openai.com/oauth/authorize",
                # token_url intentionally omitted
                "client_id": "sample-client",
                "redirect_uri": "http://127.0.0.1:1455/auth/callback",
            },
        )


def test_oauth_metadata_rejects_unknown_token_request_format():
    with pytest.raises(ConfigError):
        registry._build_oauth_metadata(
            ProviderId.CODEX,
            {
                "authorize_url": "https://auth.openai.com/oauth/authorize",
                "token_url": "https://auth.openai.com/oauth/token",
                "client_id": "sample-client",
                "redirect_uri": "http://127.0.0.1:1455/auth/callback",
                "token_request_format": "xml",
            },
        )


def test_oauth_scopes_accept_space_delimited_string():
    settings = _sample_settings(
        provider_oauth={
            "codex": {
                "authorize_url": "https://auth.openai.com/oauth/authorize",
                "token_url": "https://auth.openai.com/oauth/token",
                "client_id": "sample-client",
                "redirect_uri": "http://127.0.0.1:1455/auth/callback",
                "scopes": "openid profile email",
            }
        }
    )
    entry = registry.get_provider(ProviderId.CODEX, settings=settings)
    assert entry.oauth is not None
    assert entry.oauth.scopes == ("openid", "profile", "email")


def test_list_providers_includes_all_with_builtin_defaults():
    # With no provider config at all, the built-in defaults make every supported
    # provider resolvable, so a fresh deployment can serve and connect them.
    settings = _sample_settings(provider_base_urls={}, provider_oauth={})
    entries = registry.list_providers(settings=settings)
    ids = {entry.provider_id for entry in entries}
    assert ids == {
        ProviderId.OPENAI,
        ProviderId.OPENROUTER,
        ProviderId.CODEX,
        ProviderId.ANTHROPIC,
    }


def test_list_providers_full_sample_lists_all():
    settings = _sample_settings()
    entries = registry.list_providers(settings=settings)
    ids = {entry.provider_id for entry in entries}
    assert ids == {
        ProviderId.OPENAI,
        ProviderId.OPENROUTER,
        ProviderId.CODEX,
        ProviderId.ANTHROPIC,
    }


def test_supported_provider_ids_reports_all_known():
    assert set(registry.supported_provider_ids()) == {
        ProviderId.OPENAI,
        ProviderId.OPENROUTER,
        ProviderId.CODEX,
        ProviderId.ANTHROPIC,
    }


def test_adapter_slot_resolves_lazily_via_registration_hook():
    settings = _sample_settings()
    entry = registry.get_provider(ProviderId.OPENAI, settings=settings)

    # Save and clear any existing registration to test the hook in isolation.
    saved = registry._ADAPTER_FACTORIES.get(AdapterKind.OPENAI_COMPAT)
    registry._ADAPTER_FACTORIES.pop(AdapterKind.OPENAI_COMPAT, None)
    try:
        # No adapter registered yet -> fail closed.
        with pytest.raises(ConfigError):
            _ = entry.adapter

        sentinel = object()
        registry.register_adapter(AdapterKind.OPENAI_COMPAT, lambda: sentinel)
        assert entry.adapter is sentinel
    finally:
        registry._ADAPTER_FACTORIES.pop(AdapterKind.OPENAI_COMPAT, None)
        if saved is not None:
            registry._ADAPTER_FACTORIES[AdapterKind.OPENAI_COMPAT] = saved
