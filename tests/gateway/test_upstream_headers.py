"""Unit tests for the upstream auth-header assembly (gozar.gateway.upstream).

These verify that each provider's subscription (OAuth) and API-key calls carry the
exact headers the provider's wire contract requires:

* Codex: bearer token + chatgpt-account-id + the experimental Responses beta opt-in
  and the originator header.
* Anthropic: bearer token + anthropic-version and the Claude Code + OAuth beta opt-ins.
* OpenAI-compatible pass-through: just the bearer token.

No network or DB access: the provider entries are built from the registry defaults
and the credential material is constructed in-memory.
"""

from __future__ import annotations

import uuid

from gozar.accounts.models import CredentialKind
from gozar.accounts.service import ProviderCredentialMaterial
from gozar.core.config import Settings
from gozar.gateway import upstream
from gozar.providers import registry
from gozar.providers.registry import ProviderId
from gozar.translation.codex import CodexAdapter


def _settings() -> Settings:
    # Empty provider config so the built-in defaults are exercised end to end.
    return Settings(provider_base_urls={}, provider_oauth={})


def _subscription_material(provider: str, account_ref: str | None) -> ProviderCredentialMaterial:
    return ProviderCredentialMaterial(
        account_id=uuid.uuid4(),
        provider=provider,
        kind=CredentialKind.SUBSCRIPTION,
        access_token="oauth-access-token",
        api_key=None,
        provider_account_ref=account_ref,
        expires_at=None,
    )


def test_codex_headers_include_account_id_beta_and_originator():
    settings = _settings()
    entry = registry.get_provider(ProviderId.CODEX, settings=settings)
    material = _subscription_material("codex", "acct-123")

    headers = upstream.build_auth_headers(entry, material, CodexAdapter())

    assert headers["Authorization"] == "Bearer oauth-access-token"
    assert headers["chatgpt-account-id"] == "acct-123"
    assert headers["OpenAI-Beta"] == "responses=experimental"
    assert headers["originator"] == "codex_cli_rs"
    # Codex must NOT carry the Anthropic headers.
    assert "anthropic-version" not in headers


def test_codex_upstream_path_is_responses():
    settings = _settings()
    entry = registry.get_provider(ProviderId.CODEX, settings=settings)
    assert upstream.upstream_path(entry) == "/responses"


def test_anthropic_headers_include_version_and_both_betas():
    settings = _settings()
    entry = registry.get_provider(ProviderId.ANTHROPIC, settings=settings)
    material = _subscription_material("anthropic", None)

    headers = upstream.build_auth_headers(entry, material, object())

    assert headers["Authorization"] == "Bearer oauth-access-token"
    assert headers["anthropic-version"] == "2023-06-01"
    beta = headers["anthropic-beta"]
    assert "claude-code-20250219" in beta
    assert "oauth-2025-04-20" in beta
    # Anthropic must NOT carry the Codex headers.
    assert "OpenAI-Beta" not in headers
    assert "originator" not in headers


def test_anthropic_upstream_path_is_messages():
    settings = _settings()
    entry = registry.get_provider(ProviderId.ANTHROPIC, settings=settings)
    assert upstream.upstream_path(entry) == "/v1/messages"


def test_passthrough_api_key_headers_are_bearer_only():
    settings = _settings()
    entry = registry.get_provider(ProviderId.OPENAI, settings=settings)
    material = ProviderCredentialMaterial(
        account_id=uuid.uuid4(),
        provider="openai",
        kind=CredentialKind.API_KEY,
        access_token=None,
        api_key="sk-test",
        provider_account_ref=None,
        expires_at=None,
    )

    headers = upstream.build_auth_headers(entry, material, object())

    assert headers == {"Authorization": "Bearer sk-test"}
