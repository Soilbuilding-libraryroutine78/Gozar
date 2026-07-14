"""HTTP-level tests for the ``GET /v1/models`` router.

Drives the endpoint with FastAPI's ``TestClient`` against an in-memory database to
verify Client_Token authentication at the boundary and that the listing advertises
the right models for every Provider that has a connected, *available*
Upstream_Credential -- in the OpenAI model-listing shape. No network is touched: the
API-key validation seam is injected with a no-op, and the live model-listing lookup
(:func:`gozar.gateway.catalog.fetch_live_models`) is monkeypatched to a fake so a
connected openai/openrouter credential never dials out during a test.
"""

from __future__ import annotations

import base64

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import gozar.gateway.catalog as catalog_module
from gozar.accounts.service import connect_api_key, set_enabled
from gozar.app import create_app
from gozar.core.config import Settings
from gozar.core.db import get_session
from gozar.tokens.service import create_token

_TEST_MASTER_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()


@pytest.fixture
def settings() -> Settings:
    """Settings with provider model lists so the catalog has something to advertise."""
    return Settings(
        master_key=_TEST_MASTER_KEY,
        token_pepper="test-pepper",
        jwt_secret="test-jwt-secret",
        redis_url="redis://localhost:6379/0",
        provider_base_urls={
            "openai": "https://api.openai.com/v1",
            "openrouter": "https://openrouter.ai/api/v1",
        },
        provider_models={
            "openai": ["gpt-4o", "gpt-4o-mini"],
            "openrouter": ["meta-llama/llama-3.1-70b"],
            "anthropic": ["claude-3-5-sonnet"],
        },
    )


async def _noop_validate(entry, api_key):
    """Injected API-key validation that accepts the key without a network call."""
    return None


@pytest.fixture(autouse=True)
def _no_live_models(monkeypatch):
    """Stub the live model-listing lookup so no test ever touches the network.

    Every test in this module gets "no live listing available" by default (the
    catalog falls back to the configured list); tests that specifically exercise
    the live-listing path override this per-test.
    """

    async def _fake(provider, material, *, route_kind, settings=None):
        return None

    monkeypatch.setattr(catalog_module, "fetch_live_models", _fake)


@pytest.fixture
def client(sessionmaker, settings):
    """A TestClient with the DB session overridden to the in-memory database."""
    app = create_app(settings=settings)

    async def _override_session():
        async with sessionmaker() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_session] = _override_session
    with TestClient(app) as test_client:
        yield test_client


@pytest_asyncio.fixture
async def token_secret(sessionmaker, settings) -> str:
    """Issue a Client_Token and return its raw secret."""
    async with sessionmaker() as session:
        issued = await create_token(session, "models-token", None, settings=settings)
        await session.commit()
        return issued.secret


def test_missing_token_returns_openai_auth_error(client):
    """The listing is never disclosed without a valid Client_Token (Req 18.1)."""
    resp = client.get("/v1/models")
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["type"] == "authentication_error"


def test_invalid_token_returns_openai_auth_error(client):
    resp = client.get(
        "/v1/models", headers={"Authorization": "Bearer gz-bogus-token"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "authentication_error"


def test_no_connected_accounts_lists_nothing(client, token_secret):
    """With no connected credentials, the catalog is an empty OpenAI list."""
    resp = client.get(
        "/v1/models", headers={"Authorization": f"Bearer {token_secret}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert body["data"] == []


async def test_lists_models_for_available_credential(
    client, token_secret, sessionmaker, settings
):
    """With no live listing available, a connected credential falls back to its
    configured models."""
    async with sessionmaker() as session:
        await connect_api_key(
            session, "openai", "sk-real", settings=settings, validate=_noop_validate
        )
        await session.commit()

    resp = client.get(
        "/v1/models", headers={"Authorization": f"Bearer {token_secret}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"

    ids = {card["id"] for card in body["data"]}
    # openai is reachable -> its configured models are advertised.
    assert ids == {"gpt-4o", "gpt-4o-mini"}
    # No anthropic/openrouter credential is connected -> their models are not listed.
    for card in body["data"]:
        assert card["object"] == "model"
        assert card["owned_by"] == "openai"
        assert isinstance(card["created"], int)


async def test_live_listing_overrides_configured_fallback(
    client, token_secret, sessionmaker, settings, monkeypatch
):
    """When a live listing succeeds, it is advertised instead of the fallback."""

    async def _fake_live(provider, material, *, route_kind, settings=None):
        assert provider == "openai"
        return ["gpt-5.5", "gpt-5.5-mini"]

    monkeypatch.setattr(catalog_module, "fetch_live_models", _fake_live)

    async with sessionmaker() as session:
        await connect_api_key(
            session, "openai", "sk-real", settings=settings, validate=_noop_validate
        )
        await session.commit()

    resp = client.get(
        "/v1/models", headers={"Authorization": f"Bearer {token_secret}"}
    )
    assert resp.status_code == 200
    ids = {card["id"] for card in resp.json()["data"]}
    # The live listing entirely replaces the configured fallback for this provider.
    assert ids == {"gpt-5.5", "gpt-5.5-mini"}


async def test_live_listing_failure_falls_back_to_configured_models(
    client, token_secret, sessionmaker, settings, monkeypatch
):
    """A live-listing failure (e.g. network error) degrades to the configured list.

    ``fetch_live_models`` itself is responsible for turning failures (network
    errors, non-2xx responses, unparsable bodies) into ``None`` rather than
    raising (see :mod:`gozar.gateway.live_models`), so simulating that ``None``
    return here exercises exactly what the catalog sees on a real failure.
    """

    async def _fake_live(provider, material, *, route_kind, settings=None):
        return None

    monkeypatch.setattr(catalog_module, "fetch_live_models", _fake_live)

    async with sessionmaker() as session:
        await connect_api_key(
            session, "openai", "sk-real", settings=settings, validate=_noop_validate
        )
        await session.commit()

    resp = client.get(
        "/v1/models", headers={"Authorization": f"Bearer {token_secret}"}
    )
    assert resp.status_code == 200
    ids = {card["id"] for card in resp.json()["data"]}
    assert ids == {"gpt-4o", "gpt-4o-mini"}


async def test_codex_provider_always_uses_configured_models(
    client, token_secret, sessionmaker, settings, monkeypatch
):
    """Codex has no live listing endpoint, so it always uses the configured list."""

    async def _fail_if_called(provider, material, *, route_kind, settings=None):
        raise AssertionError(
            f"fetch_live_models must never be called for provider {provider!r}"
        )

    monkeypatch.setattr(catalog_module, "fetch_live_models", _fail_if_called)

    settings.provider_models["codex"] = ["gpt-5.5", "gpt-5.4-mini"]
    settings.provider_oauth = {}

    from gozar.accounts.models import CredentialKind, CredentialStatus, UpstreamCredential

    async with sessionmaker() as session:
        session.add(
            UpstreamCredential(
                provider="codex",
                kind=CredentialKind.SUBSCRIPTION,
                label="codex",
                status=CredentialStatus.ACTIVE,
            )
        )
        await session.commit()

    resp = client.get(
        "/v1/models", headers={"Authorization": f"Bearer {token_secret}"}
    )
    assert resp.status_code == 200
    ids = {card["id"] for card in resp.json()["data"]}
    assert ids == {"gpt-5.5", "gpt-5.4-mini"}


async def test_runtime_provider_models_override_configured_fallback(
    client, token_secret, sessionmaker, settings, monkeypatch
):
    """Subscription provider fallbacks can be updated without process restart."""

    async def _fail_if_called(provider, material, *, route_kind, settings=None):
        raise AssertionError(
            f"fetch_live_models must never be called for provider {provider!r}"
        )

    monkeypatch.setattr(catalog_module, "fetch_live_models", _fail_if_called)

    settings.provider_models["codex"] = ["gpt-old"]
    settings.provider_oauth = {}

    from gozar.accounts.models import CredentialKind, CredentialStatus, UpstreamCredential
    from gozar.providers.model_catalog import set_provider_model_catalog

    async with sessionmaker() as session:
        session.add(
            UpstreamCredential(
                provider="codex",
                kind=CredentialKind.SUBSCRIPTION,
                label="codex",
                status=CredentialStatus.ACTIVE,
            )
        )
        await set_provider_model_catalog(
            session, "codex", ["gpt-new", "gpt-new-mini", "gpt-new"]
        )
        await session.commit()

    resp = client.get(
        "/v1/models", headers={"Authorization": f"Bearer {token_secret}"}
    )
    assert resp.status_code == 200
    assert [card["id"] for card in resp.json()["data"]] == [
        "gpt-new",
        "gpt-new-mini",
    ]


async def test_pinned_token_lists_only_models_reachable_by_assigned_chain(
    client, sessionmaker, settings
):
    """The public model list follows the presented key's route assignment."""
    from gozar.accounts.models import CredentialKind, CredentialStatus, UpstreamCredential
    from gozar.routing.chains import RouteKind
    from gozar.routing.service import ChainEntryInput, create_chain

    settings.provider_models["codex"] = ["gpt-5.5", "gpt-5.4-mini"]

    async with sessionmaker() as session:
        codex = UpstreamCredential(
            provider="codex",
            kind=CredentialKind.SUBSCRIPTION,
            label="codex",
            status=CredentialStatus.ACTIVE,
        )
        openai = UpstreamCredential(
            provider="openai",
            kind=CredentialKind.API_KEY,
            label="openai",
            status=CredentialStatus.ACTIVE,
        )
        session.add_all([codex, openai])
        await session.flush()
        chain = await create_chain(
            session,
            "dual-lane",
            [
                ChainEntryInput(codex.id),
                ChainEntryInput(openai.id, route_kind=RouteKind.EMBEDDINGS),
            ],
        )
        issued = await create_token(
            session,
            "models-token",
            assigned_chain_id=chain.chain_id,
            settings=settings,
        )
        await session.commit()

    resp = client.get(
        "/v1/models", headers={"Authorization": f"Bearer {issued.secret}"}
    )
    assert resp.status_code == 200
    assert [card["id"] for card in resp.json()["data"]] == [
        "gpt-5.5",
        "gpt-5.4-mini",
    ]


async def test_disabled_credential_is_excluded(
    client, token_secret, sessionmaker, settings
):
    """A disabled credential is unavailable, so its models are not advertised."""
    async with sessionmaker() as session:
        credential = await connect_api_key(
            session, "openai", "sk-real", settings=settings, validate=_noop_validate
        )
        await set_enabled(session, credential.id, False)
        await session.commit()

    resp = client.get(
        "/v1/models", headers={"Authorization": f"Bearer {token_secret}"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"] == []
