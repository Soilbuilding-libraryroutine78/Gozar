"""End-to-end proxy integration test against a mocked upstream Provider.

This complements ``test_router.py``'s ``test_end_to_end_round_trip``, which mocks the
upstream at the pipeline seam (patching ``call_upstream``). Here the upstream is
mocked one layer deeper -- at the **httpx transport** -- so the *real*
:class:`gozar.providers.client.UpstreamClient` and
:func:`gozar.gateway.upstream.call_upstream` execute alongside the real Client_Token
auth, fallback routing, the pass-through Translation_Layer, the credential
decrypt/auth-header assembly, and usage/trace recording. Only the network socket is
replaced (``httpx.MockTransport``); everything from the HTTP boundary down to the
wire is the production code path.

This exercises Requirement 6.1: a Client_Application sends an OpenAI-compatible chat
completion with a valid Client_Token, the gateway forwards it to a selected
Upstream_Credential, and returns the Provider's response.

No network, real DB, or real Redis is touched: an in-memory SQLite DB, the in-memory
``FakeRedis`` from ``conftest``, and an ``httpx.MockTransport`` stand in.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select

from gozar.accounts.service import connect_api_key
from gozar.app import create_app
from gozar.core.db import get_session
from gozar.gateway import pipeline as pipeline_module
from gozar.providers import client as provider_client_module
from gozar.routing.service import create_chain
from gozar.tokens.service import create_token
from gozar.usage.models import TraceLog, UsageRecord

from conftest import openai_response

# The plaintext API key seeded for the upstream credential. The real decrypt path
# must surface exactly this value in the upstream ``Authorization`` header, proving
# the credential acquisition + auth-header assembly ran for real end-to-end.
_REAL_API_KEY = "sk-real-upstream-key"


async def _noop_validate(entry, api_key):
    """Injected API-key validation that accepts the key without a network call."""
    return None


@pytest_asyncio.fixture
async def seeded(sessionmaker, settings):
    """Seed a Client_Token, a real encrypted API-key account, and a chain."""
    async with sessionmaker() as session:
        issued = await create_token(session, "e2e-token", None, settings=settings)
        credential = await connect_api_key(
            session,
            "openai",
            _REAL_API_KEY,
            settings=settings,
            validate=_noop_validate,
        )
        await create_chain(session, "default", [credential.id])
        await session.commit()
        return {"secret": issued.secret, "account_id": credential.id}


@pytest.fixture
def captured() -> dict:
    """Mutable holder the MockTransport handler records the upstream request into."""
    return {}


@pytest.fixture
def client(sessionmaker, settings, redis, captured, monkeypatch):
    """A TestClient whose upstream is mocked at the httpx transport layer.

    The pipeline is pointed at the test settings and the in-memory Redis fake (as the
    other gateway tests do), but the upstream call is **not** stubbed at the pipeline
    seam. Instead ``httpx.AsyncClient`` (used internally by ``UpstreamClient``) is
    built with an ``httpx.MockTransport`` so the real client/translation/auth path
    runs and we can capture the exact request that hit the wire.
    """
    app = create_app(settings=settings)

    async def _override_session():
        async with sessionmaker() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_session] = _override_session

    monkeypatch.setattr(pipeline_module, "get_settings", lambda: settings)
    monkeypatch.setattr(pipeline_module, "get_redis", lambda: redis)

    def _handler(request: httpx.Request) -> httpx.Response:
        # Record what reached the wire so the test can assert the gateway forwarded a
        # well-formed Provider request authenticated with the decrypted credential.
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(200, json=openai_response(content="from-upstream"))

    transport = httpx.MockTransport(_handler)
    real_async_client = provider_client_module.httpx.AsyncClient

    def _async_client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(
        provider_client_module.httpx, "AsyncClient", _async_client_factory
    )

    with TestClient(app) as test_client:
        yield test_client


def _body() -> dict:
    return {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "ping"}],
        "stream": False,
    }


def test_e2e_non_streaming_round_trip_through_mocked_upstream(client, seeded, captured):
    """A valid Client_Token request is forwarded upstream and the response returned.

    Validates Requirement 6.1 through the full data-path with only the socket mocked.
    """
    resp = client.post(
        "/v1/chat/completions",
        json=_body(),
        headers={"Authorization": f"Bearer {seeded['secret']}"},
    )

    # The client receives a clean OpenAI Chat Completions response built from the
    # mocked Provider's body.
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "from-upstream"
    assert body["usage"]["total_tokens"] == 18

    # The gateway actually forwarded a request to the selected Upstream_Credential's
    # Provider endpoint, authenticated with the *decrypted* API key (real decrypt +
    # auth-header assembly, not a stub).
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/chat/completions")
    assert captured["authorization"] == f"Bearer {_REAL_API_KEY}"

    # The OpenAI request was passed through (pass-through adapter) to the wire.
    import json as _json

    forwarded = _json.loads(captured["body"])
    assert forwarded["model"] == "gpt-4o"
    assert forwarded["messages"] == [{"role": "user", "content": "ping"}]

    # The Client_Token secret is never leaked to the upstream Provider.
    assert seeded["secret"] not in captured["authorization"]


async def test_e2e_records_usage_and_trace(client, seeded, sessionmaker):
    """The transport-level round-trip meters usage and finalizes the trace."""
    resp = client.post(
        "/v1/chat/completions",
        json=_body(),
        headers={"Authorization": f"Bearer {seeded['secret']}"},
    )
    assert resp.status_code == 200

    async with sessionmaker() as session:
        record = (await session.scalars(select(UsageRecord))).one()
        assert record.account_id == seeded["account_id"]
        assert record.provider == "openai"
        assert record.total_tokens == 18

        trace = (await session.scalars(select(TraceLog))).one()
        assert trace.outcome == "success"
        assert trace.status_code == 200
        assert trace.account_id == seeded["account_id"]
