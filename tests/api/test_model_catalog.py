"""Admin API tests for the grouped model catalog."""

from __future__ import annotations

import asyncio

import pytest

from gozar.accounts.models import CredentialKind, CredentialStatus, UpstreamCredential
from gozar.accounts.service import connect_api_key
from gozar.routing.chains import RouteKind
from gozar.routing.service import ChainEntryInput, create_chain


async def _accept_key(entry, api_key):  # noqa: ANN001
    """Accept API-key validation without a provider network call."""
    return None


async def _seed_catalog(sessionmaker, settings) -> None:
    async with sessionmaker() as session:
        openai = await connect_api_key(
            session,
            "openai",
            "sk-test-openai",
            label="Primary OpenAI",
            settings=settings,
            validate=_accept_key,
        )
        codex = UpstreamCredential(
            provider="codex",
            kind=CredentialKind.SUBSCRIPTION,
            label="Codex subscription",
            status=CredentialStatus.ACTIVE,
        )
        session.add(codex)
        await session.flush()
        await create_chain(session, "codex-only", [codex.id])
        await create_chain(
            session,
            "mixed-route",
            [
                openai.id,
                codex.id,
                ChainEntryInput(
                    openai.id,
                    "text-embedding-3-small",
                    route_kind=RouteKind.EMBEDDINGS,
                ),
            ],
        )
        await session.commit()


async def _seed_two_openrouter_accounts(sessionmaker, settings) -> None:
    async with sessionmaker() as session:
        await connect_api_key(
            session,
            "openrouter",
            "sk-openrouter-team-a",
            label="OpenRouter team A",
            settings=settings,
            validate=_accept_key,
        )
        await connect_api_key(
            session,
            "openrouter",
            "sk-openrouter-team-b",
            label="OpenRouter team B",
            settings=settings,
            validate=_accept_key,
        )
        await session.commit()


@pytest.fixture(autouse=True)
def _skip_redis_cache(monkeypatch):
    """Keep the catalog endpoint deterministic and offline in API tests."""
    import gozar.gateway.catalog as catalog_module

    monkeypatch.setattr(
        catalog_module,
        "get_redis",
        lambda: (_ for _ in ()).throw(RuntimeError("redis disabled in test")),
    )


def test_model_catalog_groups_models_by_account_and_chain(
    client,
    sessionmaker,
    settings,
    auth_header,
    monkeypatch,
):
    import gozar.gateway.catalog as catalog_module

    async def _fake_live_models(provider, material, **kwargs):  # noqa: ANN001
        assert provider == "openai"
        assert material.api_key == "sk-test-openai"
        if kwargs["route_kind"] is RouteKind.EMBEDDINGS:
            return ["text-embedding-3-small"]
        return ["gpt-live", "gpt-live-mini"]

    monkeypatch.setattr(catalog_module, "fetch_live_models", _fake_live_models)
    settings.provider_models.update(
        {
            "codex": ["gpt-5.5", "gpt-5.4-mini"],
            "openai": ["gpt-configured-fallback"],
        }
    )
    asyncio.run(_seed_catalog(sessionmaker, settings))

    resp = client.get("/api/models", headers=auth_header("admin"))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["refreshed"] is False
    assert body["model_count"] == 4
    assert body["embedding_model_count"] == 1
    assert [model["id"] for model in body["models"]] == [
        "gpt-live",
        "gpt-live-mini",
        "gpt-5.5",
        "gpt-5.4-mini",
    ]
    assert [model["id"] for model in body["embedding_models"]] == [
        "text-embedding-3-small"
    ]

    accounts = {account["label"]: account for account in body["accounts"]}
    assert [model["id"] for model in accounts["Primary OpenAI"]["models"]] == [
        "gpt-live",
        "gpt-live-mini",
    ]
    assert [
        model["id"] for model in accounts["Primary OpenAI"]["embedding_models"]
    ] == ["text-embedding-3-small"]
    assert [model["id"] for model in accounts["Codex subscription"]["models"]] == [
        "gpt-5.5",
        "gpt-5.4-mini",
    ]
    assert accounts["Codex subscription"]["embedding_models"] == []

    chains = {chain["name"]: chain for chain in body["chains"]}
    assert [model["id"] for model in chains["codex-only"]["models"]] == [
        "gpt-5.5",
        "gpt-5.4-mini",
    ]
    assert [model["id"] for model in chains["mixed-route"]["models"]] == [
        "gpt-live",
        "gpt-live-mini",
        "gpt-5.5",
        "gpt-5.4-mini",
    ]
    assert chains["mixed-route"]["chat_entry_count"] == 2
    assert chains["mixed-route"]["embedding_entry_count"] == 1
    assert [
        model["id"] for model in chains["mixed-route"]["embedding_models"]
    ] == ["text-embedding-3-small"]


def test_model_catalog_refresh_query_reaches_live_listing(
    client,
    sessionmaker,
    settings,
    auth_header,
    monkeypatch,
):
    import gozar.gateway.catalog as catalog_module

    calls: list[tuple[str, RouteKind]] = []

    async def _fake_live_models(provider, material, **kwargs):  # noqa: ANN001
        route_kind = kwargs["route_kind"]
        calls.append((provider, route_kind))
        if route_kind is RouteKind.EMBEDDINGS:
            return ["text-embedding-refreshed"]
        return ["gpt-refreshed"]

    monkeypatch.setattr(catalog_module, "fetch_live_models", _fake_live_models)
    settings.provider_models["openai"] = ["gpt-fallback"]
    asyncio.run(_seed_catalog(sessionmaker, settings))

    resp = client.get(
        "/api/models",
        params={"refresh": "true"},
        headers=auth_header("admin"),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["refreshed"] is True
    assert ("openai", RouteKind.CHAT) in calls
    assert ("openai", RouteKind.EMBEDDINGS) in calls


def test_model_catalog_keeps_live_models_scoped_to_each_api_key(
    client,
    sessionmaker,
    settings,
    auth_header,
    monkeypatch,
):
    import gozar.gateway.catalog as catalog_module

    async def _fake_live_models(provider, material, **kwargs):  # noqa: ANN001
        assert provider == "openrouter"
        route_kind = kwargs["route_kind"]
        if material.api_key == "sk-openrouter-team-a":
            if route_kind is RouteKind.EMBEDDINGS:
                return ["openai/text-embedding-3-small"]
            return ["openai/gpt-5.4-mini"]
        if route_kind is RouteKind.EMBEDDINGS:
            return ["google/gemini-embedding-001"]
        return ["google/gemini-2.5-flash"]

    monkeypatch.setattr(catalog_module, "fetch_live_models", _fake_live_models)
    asyncio.run(_seed_two_openrouter_accounts(sessionmaker, settings))

    response = client.get("/api/models", headers=auth_header("admin"))

    assert response.status_code == 200, response.text
    accounts = {account["label"]: account for account in response.json()["accounts"]}
    assert [model["id"] for model in accounts["OpenRouter team A"]["models"]] == [
        "openai/gpt-5.4-mini"
    ]
    assert [model["id"] for model in accounts["OpenRouter team B"]["models"]] == [
        "google/gemini-2.5-flash"
    ]
    assert [
        model["id"] for model in accounts["OpenRouter team A"]["embedding_models"]
    ] == ["openai/text-embedding-3-small"]
    assert [
        model["id"] for model in accounts["OpenRouter team B"]["embedding_models"]
    ] == ["google/gemini-embedding-001"]
    assert [model["id"] for model in response.json()["models"]] == [
        "openai/gpt-5.4-mini",
        "google/gemini-2.5-flash",
    ]


def test_provider_model_catalog_update_changes_admin_catalog_without_restart(
    client,
    sessionmaker,
    settings,
    auth_header,
    monkeypatch,
):
    import gozar.gateway.catalog as catalog_module

    async def _fake_live_models(provider, material, **kwargs):  # noqa: ANN001
        assert provider == "openai"
        if kwargs["route_kind"] is RouteKind.EMBEDDINGS:
            return ["text-embedding-3-small"]
        return ["gpt-live"]

    monkeypatch.setattr(catalog_module, "fetch_live_models", _fake_live_models)
    settings.provider_models.update(
        {
            "codex": ["gpt-old"],
            "openai": ["gpt-configured-fallback"],
        }
    )
    asyncio.run(_seed_catalog(sessionmaker, settings))

    first = client.get("/api/models", headers=auth_header("admin"))
    assert first.status_code == 200, first.text
    codex_provider = next(
        provider
        for provider in first.json()["providers"]
        if provider["provider"] == "codex"
    )
    assert codex_provider["source"] == "environment"
    assert codex_provider["models"] == ["gpt-old"]

    updated = client.put(
        "/api/models/providers/codex",
        json={"models": ["gpt-new", "gpt-new-mini", "gpt-new"]},
        headers=auth_header("admin"),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["source"] == "runtime"
    assert updated.json()["models"] == ["gpt-new", "gpt-new-mini"]

    current = client.get("/api/models", headers=auth_header("admin"))
    assert current.status_code == 200, current.text
    body = current.json()
    codex_account = next(
        account
        for account in body["accounts"]
        if account["label"] == "Codex subscription"
    )
    assert [model["id"] for model in codex_account["models"]] == [
        "gpt-new",
        "gpt-new-mini",
    ]
    codex_provider = next(
        provider for provider in body["providers"] if provider["provider"] == "codex"
    )
    assert codex_provider["source"] == "runtime"
    assert codex_provider["models"] == ["gpt-new", "gpt-new-mini"]

    reset = client.delete("/api/models/providers/codex", headers=auth_header("admin"))
    assert reset.status_code == 200, reset.text
    assert reset.json()["source"] == "environment"
    assert reset.json()["models"] == ["gpt-old"]


def test_model_catalog_viewer_is_denied(client, auth_header):
    resp = client.get("/api/models", headers=auth_header("viewer"))

    assert resp.status_code == 403
