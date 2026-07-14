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

import httpx
import pytest

from gozar.accounts.models import CredentialKind
from gozar.accounts.service import ProviderCredentialMaterial
from gozar.core.config import Settings
from gozar.core.errors import ConfigError
from gozar.gateway import upstream
from gozar.providers import registry
from gozar.providers.registry import ProviderId
from gozar.translation.codex import CodexAdapter
from gozar.translation.types import OpenAIEmbeddingRequest


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


def test_only_api_key_providers_advertise_embeddings_paths():
    settings = _settings()

    assert upstream.embeddings_path(
        registry.get_provider(ProviderId.OPENAI, settings=settings)
    ) == "/embeddings"
    assert upstream.embeddings_path(
        registry.get_provider(ProviderId.OPENROUTER, settings=settings)
    ) == "/embeddings"
    with pytest.raises(ConfigError, match="does not support embeddings"):
        upstream.embeddings_path(
            registry.get_provider(ProviderId.CODEX, settings=settings)
        )


@pytest.mark.asyncio
async def test_embeddings_call_uses_provider_path_auth_and_clean_body(monkeypatch):
    settings = _settings()
    entry = registry.get_provider(ProviderId.OPENAI, settings=settings)
    material = ProviderCredentialMaterial(
        account_id=uuid.uuid4(),
        provider="openai",
        kind=CredentialKind.API_KEY,
        access_token=None,
        api_key="sk-embedding-test",
        provider_account_ref=None,
        expires_at=None,
    )
    request = OpenAIEmbeddingRequest.model_validate(
        {
            "model": "text-embedding-3-small",
            "input": "hello",
            "gozar": {"include_metadata": True},
        }
    )

    def handler(raw_request: httpx.Request) -> httpx.Response:
        assert raw_request.url == httpx.URL("https://api.openai.com/v1/embeddings")
        assert raw_request.headers["Authorization"] == "Bearer sk-embedding-test"
        assert b'"gozar"' not in raw_request.content
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"object": "embedding", "embedding": [0.1], "index": 0}
                ],
                "model": "text-embedding-3-small",
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    class InjectedClient(upstream.UpstreamClient):
        def __init__(self, provider_entry, *, settings=None):
            super().__init__(provider_entry, settings=settings, client=client)

    monkeypatch.setattr(upstream, "UpstreamClient", InjectedClient)
    try:
        response = await upstream.call_upstream_embeddings(
            entry,
            material,
            request,
            settings=settings,
        )
    finally:
        await client.aclose()

    assert response["object"] == "list"
