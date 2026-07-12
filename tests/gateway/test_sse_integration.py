"""End-to-end SSE wire-framing integration test for the streaming data-path.

Task 12.7 / Requirement 6.3: streamed responses are streamed chunk-by-chunk to the
client. Where :mod:`tests.gateway.test_streaming` unit-tests the parser/framing
helpers and the pipeline generator, and :mod:`tests.gateway.test_router` asserts a
streaming request returns SSE at a high level, this module verifies the *wire format
the client actually receives* when a real streaming request is driven through the
full HTTP stack with FastAPI's ``TestClient``:

* the response ``Content-Type`` is ``text/event-stream`` (with the unbuffered-proxy
  hint headers the gateway sets);
* every event on the wire is a ``data: <payload>`` line and events are separated by a
  blank line (``\\n\\n``);
* every non-terminator event's payload is valid JSON in the strict OpenAI
  ``chat.completion.chunk`` shape (so the OpenAI SDKs parse it unchanged);
* the stream is terminated by exactly one ``data: [DONE]`` event, emitted last, even
  when the upstream Provider sends its *own* ``[DONE]`` (it must not be double-framed);
* provider keep-alive comments, non-JSON ``data`` pings, and events split across
  arbitrary network byte boundaries are reassembled/dropped so the client only ever
  sees clean, well-formed events.

The upstream is mocked at the :data:`gozar.gateway.pipeline.call_upstream_stream`
seam (no network), but the Client_Token auth, routing, the real SSE parser
(:func:`gozar.gateway.streaming.iter_sse_data`), adapter translation, OpenAI framing,
and ``StreamingResponse`` delivery all execute for real through the HTTP layer.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from gozar.accounts.service import connect_api_key
from gozar.app import create_app
from gozar.core.db import get_session
from gozar.gateway import pipeline as pipeline_module
from gozar.routing.service import create_chain
from gozar.tokens.service import create_token
from gozar.translation.types import OpenAIStreamChunk

# ``settings``, ``sessionmaker``, and ``redis`` are provided by tests/gateway/conftest.py.

_MODEL = "gpt-4o"


# --------------------------------------------------------------------------- #
# A realistic, "messy" upstream OpenAI-compatible SSE byte stream.
# --------------------------------------------------------------------------- #
def _chunk_event(payload: dict) -> str:
    base = {
        "id": "chatcmpl-wire",
        "object": "chat.completion.chunk",
        "created": 1_700_000_000,
        "model": _MODEL,
    }
    return f"data: {json.dumps({**base, **payload})}\n\n"


def _upstream_wire_bytes() -> list[bytes]:
    """Build provider SSE *bytes* that stress the wire framing and parser.

    Includes an SSE comment keep-alive, a non-JSON ``data`` ping, a content delta
    deliberately split across three network byte slices, a final usage-bearing event,
    and the Provider's *own* ``data: [DONE]`` terminator (which must be consumed and
    re-emitted as exactly one terminator, never double-framed).
    """
    role = _chunk_event(
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                      "finish_reason": None}]}
    )
    content = _chunk_event(
        {"choices": [{"index": 0, "delta": {"content": "hello world"},
                      "finish_reason": None}]}
    )
    final = _chunk_event(
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11}}
    )

    # The content event is sliced mid-payload to prove cross-chunk reassembly.
    third = len(content) // 3
    return [
        b": keep-alive\n\n",          # SSE comment: parser drops it entirely.
        b"data: ping\n\n",            # non-JSON data: pipeline skips, never framed.
        role.encode(),
        content[:third].encode(),     # |
        content[third : 2 * third].encode(),  # | one logical event, three byte slices
        content[2 * third :].encode(),         # |
        final.encode(),
        b"data: [DONE]\n\n",          # Provider's own terminator (consumed by gateway).
    ]


# --------------------------------------------------------------------------- #
# Fixtures (named distinctly; do not collide with other gateway test modules).
# --------------------------------------------------------------------------- #
async def _noop_validate(entry, api_key):
    """API-key validation that accepts the key without a network call."""
    return None


@pytest_asyncio.fixture
async def sse_seeded(sessionmaker, settings):
    """Seed a Client_Token, a real encrypted API-key account, and a default chain."""
    async with sessionmaker() as session:
        issued = await create_token(session, "sse-wire-token", None, settings=settings)
        credential = await connect_api_key(
            session, "openai", "sk-real-key", settings=settings, validate=_noop_validate
        )
        await create_chain(session, "default", [credential.id])
        await session.commit()
        return {"secret": issued.secret, "account_id": credential.id}


@pytest.fixture
def sse_client(sessionmaker, settings, redis, monkeypatch):
    """A TestClient with the DB session overridden and the streaming upstream mocked."""
    app = create_app(settings=settings)

    async def _override_session():
        async with sessionmaker() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_session] = _override_session
    monkeypatch.setattr(pipeline_module, "get_settings", lambda: settings)
    monkeypatch.setattr(pipeline_module, "get_redis", lambda: redis)

    async def _fake_call_upstream_stream(entry, material, adapter, body, *, settings=None):
        for raw in _upstream_wire_bytes():
            yield raw

    monkeypatch.setattr(
        pipeline_module, "call_upstream_stream", _fake_call_upstream_stream
    )

    with TestClient(app) as test_client:
        yield test_client


def _stream_body(model: str = _MODEL) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }


def _split_events(body: str) -> list[str]:
    """Split a raw SSE body into its events (blank-line separated)."""
    return [block for block in body.split("\n\n") if block != ""]


# --------------------------------------------------------------------------- #
# Wire-framing assertions
# --------------------------------------------------------------------------- #
def test_streaming_response_is_event_stream_with_unbuffered_headers(sse_client, sse_seeded):
    """The HTTP response advertises SSE and the proxy-unbuffering hint headers."""
    resp = sse_client.post(
        "/v1/chat/completions",
        json=_stream_body(),
        headers={"Authorization": f"Bearer {sse_seeded['secret']}"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    # Headers that keep the stream flowing unbuffered through reverse proxies.
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["x-accel-buffering"] == "no"


def test_every_event_is_a_data_line_separated_by_blank_lines(sse_client, sse_seeded):
    """Each event on the wire is a single ``data: <payload>`` line, blank-line framed."""
    resp = sse_client.post(
        "/v1/chat/completions",
        json=_stream_body(),
        headers={"Authorization": f"Bearer {sse_seeded['secret']}"},
    )
    body = resp.text

    # Blank-line framing: the body is a sequence of `...\n\n` blocks.
    assert body.endswith("\n\n")
    events = _split_events(body)
    assert len(events) >= 2  # at least one content chunk plus the terminator.
    for event in events:
        # Exactly one line per event, and it is a `data:` field.
        assert "\n" not in event, f"event is not a single line: {event!r}"
        assert event.startswith("data: "), f"event is not a data line: {event!r}"


def test_non_terminator_events_are_openai_chunk_json(sse_client, sse_seeded):
    """Every non-``[DONE]`` event payload is valid strict OpenAI chunk JSON."""
    resp = sse_client.post(
        "/v1/chat/completions",
        json=_stream_body(),
        headers={"Authorization": f"Bearer {sse_seeded['secret']}"},
    )
    events = _split_events(resp.text)

    forwarded_content = ""
    for event in events:
        payload = event[len("data: ") :]
        if payload == "[DONE]":
            continue
        # Parseable JSON in the OpenAI chat.completion.chunk shape.
        data = json.loads(payload)
        assert data["object"] == "chat.completion.chunk"
        for key in ("id", "created", "model", "choices"):
            assert key in data
        # Round-trips through the strict model the OpenAI SDKs expect.
        chunk = OpenAIStreamChunk.model_validate(data)
        for choice in chunk.choices:
            forwarded_content += choice.delta.get("content") or ""

    # The provider content delta was forwarded intact to the client.
    assert "hello world" in forwarded_content


def test_stream_terminated_by_exactly_one_done(sse_client, sse_seeded):
    """The stream ends with exactly one ``data: [DONE]`` (upstream's is not re-framed).

    The mocked Provider emits its own ``data: [DONE]``; the gateway must consume it and
    emit a single canonical terminator as the final event.
    """
    resp = sse_client.post(
        "/v1/chat/completions",
        json=_stream_body(),
        headers={"Authorization": f"Bearer {sse_seeded['secret']}"},
    )
    body = resp.text
    events = _split_events(body)

    assert body.count("data: [DONE]") == 1
    assert events[-1] == "data: [DONE]"


def test_keepalive_and_non_json_pings_never_reach_the_client(sse_client, sse_seeded):
    """Provider SSE comments and non-JSON ``data`` pings are dropped, not forwarded."""
    resp = sse_client.post(
        "/v1/chat/completions",
        json=_stream_body(),
        headers={"Authorization": f"Bearer {sse_seeded['secret']}"},
    )
    body = resp.text

    # The upstream comment line and the bare "ping" payload must not appear on the wire.
    assert "keep-alive" not in body
    assert "data: ping" not in body
    # Every emitted event is either a JSON chunk or the terminator (asserted above);
    # confirm no stray empty data events leaked through.
    assert "data: \n" not in body
