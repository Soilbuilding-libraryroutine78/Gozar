"""HTTP-level tests for the ``/v1/chat/completions`` router.

These drive the endpoint with FastAPI's ``TestClient`` to verify the OpenAI-
compatible error envelopes, Client_Token authentication at the HTTP boundary, and a
full end-to-end round-trip. The end-to-end test runs the real ``get_usable_token``
decrypt path against an in-memory database and only mocks the upstream provider call
(patched at the pipeline seam), so the auth, routing, translation, and usage/trace
recording all execute for real.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select

from gozar.core.errors import UpstreamError
from gozar.accounts.service import connect_api_key
from gozar.app import create_app
from gozar.core.db import get_session
from gozar.gateway import pipeline as pipeline_module
from gozar.routing.service import create_chain
from gozar.tokens.service import create_token
from gozar.usage.models import UsageRecord

from conftest import openai_response


def _openai_sse_bytes(content: str = "routed", model: str = "gpt-4o") -> list[bytes]:
    """Build a small OpenAI-compatible SSE byte stream (two deltas, then [DONE]).

    The final delta carries ``usage`` (as a Provider that opted into usage on the
    stream would) so the end-to-end test can assert metering from a stream.
    """
    import json as _json

    def event(payload: dict) -> bytes:
        return f"data: {_json.dumps(payload)}\n\n".encode()

    base = {"id": "chatcmpl-stream", "object": "chat.completion.chunk",
            "created": 1_700_000_000, "model": model}
    return [
        event({**base, "choices": [{"index": 0, "delta": {"role": "assistant",
               "content": ""}, "finish_reason": None}]}),
        event({**base, "choices": [{"index": 0, "delta": {"content": content},
               "finish_reason": None}]}),
        event({**base, "choices": [{"index": 0, "delta": {},
               "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 5, "completion_tokens": 3,
                         "total_tokens": 8}}),
        b"data: [DONE]\n\n",
    ]


async def _noop_validate(entry, api_key):
    """Injected API-key validation that accepts the key without a network call."""
    return None


@pytest_asyncio.fixture
async def seeded(sessionmaker, settings):
    """Seed a token, a real (encrypted) API-key account, and a chain; return ids."""
    async with sessionmaker() as session:
        issued = await create_token(session, "router-token", None, settings=settings)
        credential = await connect_api_key(
            session,
            "openai",
            "sk-real-key",
            settings=settings,
            validate=_noop_validate,
        )
        chain = await create_chain(session, "default", [credential.id])
        await session.commit()
        return {
            "secret": issued.secret,
            "account_id": credential.id,
            "chain_id": chain.chain_id,
        }


@pytest.fixture
def client(sessionmaker, settings, redis, monkeypatch):
    """A TestClient with the DB session overridden and the pipeline seams patched."""
    app = create_app(settings=settings)

    async def _override_session():
        async with sessionmaker() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_session] = _override_session

    # Point the pipeline at the test settings and the in-memory Redis fake, and mock
    # the upstream provider call so no network is touched.
    monkeypatch.setattr(pipeline_module, "get_settings", lambda: settings)
    monkeypatch.setattr(pipeline_module, "get_redis", lambda: redis)

    async def _fake_call_upstream(entry, material, adapter, body, *, settings=None):
        return openai_response(content="routed")

    monkeypatch.setattr(pipeline_module, "call_upstream", _fake_call_upstream)

    async def _fake_call_upstream_stream(entry, material, adapter, body, *, settings=None):
        for raw in _openai_sse_bytes(content="routed"):
            yield raw

    monkeypatch.setattr(
        pipeline_module, "call_upstream_stream", _fake_call_upstream_stream
    )

    with TestClient(app) as test_client:
        yield test_client


def _body(stream: bool = False) -> dict:
    return {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream,
    }


def test_missing_token_returns_openai_auth_error(client):
    resp = client.post("/v1/chat/completions", json=_body())
    assert resp.status_code == 401
    body = resp.json()
    # OpenAI-compatible error envelope (Requirement 6.2 / drop-in compatibility).
    assert body["error"]["type"] == "authentication_error"
    assert body["error"]["code"] == "AUTHENTICATION_ERROR"
    assert resp.headers["x-request-id"] == resp.headers["x-gozar-trace-id"]


def test_invalid_json_body_returns_400(client):
    resp = client.post(
        "/v1/chat/completions",
        content=b"not json",
        headers={"Authorization": "Bearer gz-x-y", "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_request_error"


def test_conflicting_header_and_body_chain_overrides_return_400(client, seeded):
    header_chain = "11111111-1111-4111-8111-111111111111"
    body_chain = "22222222-2222-4222-8222-222222222222"
    resp = client.post(
        "/v1/chat/completions",
        json={**_body(), "gozar": {"chain_id": body_chain}},
        headers={
            "Authorization": f"Bearer {seeded['secret']}",
            "X-Gozar-Chain-ID": header_chain,
        },
    )

    assert resp.status_code == 400
    assert "must match" in resp.json()["error"]["message"]


def test_streaming_request_returns_sse(client, seeded):
    resp = client.post(
        "/v1/chat/completions",
        json=_body(stream=True),
        headers={"Authorization": f"Bearer {seeded['secret']}"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["x-request-id"] == resp.headers["x-gozar-trace-id"]

    body = resp.text
    # OpenAI SSE framing: data: lines terminated by data: [DONE].
    assert body.startswith("data: ")
    assert body.rstrip().endswith("data: [DONE]")
    # The translated content delta was forwarded to the client.
    assert "routed" in body
    # Exactly one terminator is emitted (upstream's own [DONE] is not double-framed).
    assert body.count("data: [DONE]") == 1


def test_streaming_missing_token_returns_openai_auth_error(client):
    """A streaming request without a token still fails closed with a JSON 401."""
    resp = client.post("/v1/chat/completions", json=_body(stream=True))
    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "authentication_error"


async def test_streaming_records_usage_and_trace(client, seeded, sessionmaker):
    """A streamed round-trip meters usage and finalizes the trace as success."""
    from gozar.usage.models import TraceLog

    resp = client.post(
        "/v1/chat/completions",
        json=_body(stream=True),
        headers={"Authorization": f"Bearer {seeded['secret']}"},
    )
    assert resp.status_code == 200
    # Drain the stream so the generator's end-of-stream recording runs.
    _ = resp.text

    async with sessionmaker() as session:
        record = (await session.scalars(select(UsageRecord))).one()
        assert record.account_id == seeded["account_id"]
        assert record.total_tokens == 8

        trace = (await session.scalars(select(TraceLog))).one()
        assert trace.outcome == "success"
        assert trace.status_code == 200


def test_end_to_end_round_trip(client, seeded, sessionmaker):
    resp = client.post(
        "/v1/chat/completions",
        json=_body(),
        headers={"Authorization": f"Bearer {seeded['secret']}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "routed"
    assert body["usage"]["total_tokens"] == 18
    assert "gozar" not in body
    assert resp.headers["x-request-id"] == resp.headers["x-gozar-trace-id"]
    assert resp.headers["x-gozar-chain-id"] == str(seeded["chain_id"])
    assert resp.headers["x-gozar-node-position"] == "0"
    assert resp.headers["x-gozar-attempt-count"] == "1"
    assert resp.headers["x-gozar-provider"] == "openai"
    assert resp.headers["x-gozar-model"] == "gpt-4o"


def test_opt_in_namespaced_metadata_keeps_standard_response_fields(client, seeded):
    resp = client.post(
        "/v1/chat/completions",
        json={**_body(), "gozar": {"include_metadata": True}},
        headers={"Authorization": f"Bearer {seeded['secret']}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "routed"
    assert body["gozar"]["trace_id"] == resp.headers["x-gozar-trace-id"]
    routing = body["gozar"]["routing"]
    assert routing["chain_id"] == str(seeded["chain_id"])
    assert routing["selected_position"] == 0
    assert routing["attempt_count"] == 1
    assert routing["attempts"][0]["outcome"] == "success"
    assert routing["attempts"][0]["usage"]["total_tokens"] == 18


async def test_end_to_end_records_usage(client, seeded, sessionmaker):
    """The HTTP round-trip persists a usage record for the served account."""
    resp = client.post(
        "/v1/chat/completions",
        json=_body(),
        headers={"Authorization": f"Bearer {seeded['secret']}"},
    )
    assert resp.status_code == 200

    async with sessionmaker() as session:
        record = (await session.scalars(select(UsageRecord))).one()
        assert record.account_id == seeded["account_id"]
        assert record.total_tokens == 18


async def test_upstream_error_persists_trace_and_reports_provider_reason(
    client, seeded, sessionmaker, monkeypatch
):
    """Terminal gateway errors remain visible in traces and include the last cause."""
    from gozar.usage.models import TraceLog

    async def _fail_upstream(entry, material, adapter, body, *, settings=None):
        raise UpstreamError(
            "upstream provider 'openai' returned status 400",
            details=[
                {"upstream_status": 400},
                {"upstream_body": '{"error":"model not found"}'},
            ],
        )

    monkeypatch.setattr(pipeline_module, "call_upstream", _fail_upstream)

    resp = client.post(
        "/v1/chat/completions",
        json=_body(),
        headers={"Authorization": f"Bearer {seeded['secret']}"},
    )
    assert resp.status_code == 502
    assert resp.headers["x-request-id"] == resp.headers["x-gozar-trace-id"]
    assert "last error: upstream provider 'openai' returned status 400" in (
        resp.json()["error"]["message"]
    )
    assert "model not found" in resp.json()["error"]["message"]

    async with sessionmaker() as session:
        trace = (await session.scalars(select(TraceLog))).one()
        assert trace.outcome == "all_fallbacks_failed"
        assert trace.status_code == 502
        routing = trace.outbound_meta["routing"]
        assert routing["attempt_count"] == 1
        assert routing["attempts"][0]["outcome"] == "error"
        assert routing["attempts"][0]["error"]["upstream_status"] == 400
        assert "upstream_body" not in routing["attempts"][0]["error"]
