"""Unit tests for :func:`gozar.gateway.live_models.fetch_live_models`.

Mocks at the ``httpx`` transport level (the project convention; see
``tests/providers/test_client.py``) so no test touches the network. Covers the
contract :mod:`gozar.gateway.catalog` depends on: a successful listing returns the
model ids, and every failure mode (no api key, upstream error, unparsable body)
returns ``None`` rather than raising.
"""

from __future__ import annotations

import uuid

import httpx

import gozar.providers.client as provider_client_module
from gozar.accounts.models import CredentialKind
from gozar.accounts.service import ProviderCredentialMaterial
from gozar.core.config import Settings
from gozar.gateway.live_models import fetch_live_models
from gozar.routing.chains import RouteKind


def _settings() -> Settings:
    return Settings(
        provider_base_urls={
            "openai": "https://api.openai.com/v1",
            "openrouter": "https://openrouter.ai/api/v1",
        },
        upstream_max_attempts=1,
    )


def _material(
    api_key: str | None = "sk-real", *, provider: str = "openai"
) -> ProviderCredentialMaterial:
    return ProviderCredentialMaterial(
        account_id=uuid.uuid4(),
        provider=provider,
        kind=CredentialKind.API_KEY,
        access_token=None,
        api_key=api_key,
        provider_account_ref=None,
        expires_at=None,
    )


def _mock_transport(handler, monkeypatch) -> None:
    """Point every ``UpstreamClient``-built ``httpx.AsyncClient`` at a mock transport."""
    transport = httpx.MockTransport(handler)
    real_async_client = provider_client_module.httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(provider_client_module.httpx, "AsyncClient", _factory)


async def test_returns_model_ids_on_success(monkeypatch):
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sk-real"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "gpt-5.5", "object": "model"},
                    {"id": "gpt-5.5-mini"},
                    {"id": "text-embedding-3-small"},
                ],
            },
        )

    _mock_transport(_handler, monkeypatch)

    result = await fetch_live_models("openai", _material(), settings=_settings())
    assert result == ["gpt-5.5", "gpt-5.5-mini"]


async def test_openai_embedding_models_are_discovered_from_live_listing(monkeypatch):
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        assert request.url.query == b""
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "gpt-5.5"},
                    {"id": "text-embedding-3-small"},
                    {"id": "text-embedding-3-large"},
                ],
            },
        )

    _mock_transport(_handler, monkeypatch)

    result = await fetch_live_models(
        "openai",
        _material(),
        route_kind=RouteKind.EMBEDDINGS,
        settings=_settings(),
    )

    assert result == ["text-embedding-3-small", "text-embedding-3-large"]


async def test_openrouter_requests_embedding_output_models(monkeypatch):
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/models"
        assert request.url.params["output_modalities"] == "embeddings"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "openai/text-embedding-3-small",
                        "architecture": {"output_modalities": ["embeddings"]},
                    },
                    {
                        "id": "openai/gpt-5.4-mini",
                        "architecture": {"output_modalities": ["text"]},
                    },
                ]
            },
        )

    _mock_transport(_handler, monkeypatch)

    result = await fetch_live_models(
        "openrouter",
        _material(provider="openrouter"),
        route_kind=RouteKind.EMBEDDINGS,
        settings=_settings(),
    )

    assert result == ["openai/text-embedding-3-small"]


async def test_returns_none_without_an_api_key():
    material = _material(api_key=None)
    result = await fetch_live_models("openai", material, settings=_settings())
    assert result is None


async def test_returns_none_on_upstream_error(monkeypatch):
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream is down")

    _mock_transport(_handler, monkeypatch)

    result = await fetch_live_models("openai", _material(), settings=_settings())
    assert result is None


async def test_returns_none_on_malformed_json_body(monkeypatch):
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    _mock_transport(_handler, monkeypatch)

    result = await fetch_live_models("openai", _material(), settings=_settings())
    assert result is None


async def test_returns_none_on_unexpected_response_shape(monkeypatch):
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    _mock_transport(_handler, monkeypatch)

    result = await fetch_live_models("openai", _material(), settings=_settings())
    assert result is None


async def test_returns_none_for_unconfigured_provider(monkeypatch):
    settings = Settings(provider_base_urls={}, upstream_max_attempts=1)
    result = await fetch_live_models("openai", _material(), settings=settings)
    assert result is None
