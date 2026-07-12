"""Unit tests for Codex's SSE-only non-streaming call path.

The Codex Responses backend rejects a request whose body sets ``"stream": false``
(400 "Stream must be set to true"); it only ever serves this endpoint as
Server-Sent Events. :func:`gozar.gateway.upstream.call_upstream` must therefore open
the stream itself for a "non-streaming" Codex call and aggregate it into the single
completed-response object, exactly as if it had come back from a buffered request.

Mocks at the ``httpx`` transport level (the project convention); no test touches the
network.
"""

from __future__ import annotations

import json
import uuid

import httpx

import gozar.providers.client as provider_client_module
from gozar.accounts.models import CredentialKind
from gozar.accounts.service import ProviderCredentialMaterial
from gozar.core.config import Settings
from gozar.core.errors import UpstreamError
from gozar.gateway.upstream import call_upstream
from gozar.providers.registry import ProviderId, get_provider
from gozar.translation.codex import CodexAdapter


def _settings() -> Settings:
    return Settings(provider_base_urls={}, provider_oauth={}, upstream_max_attempts=1)


def _material() -> ProviderCredentialMaterial:
    return ProviderCredentialMaterial(
        account_id=uuid.uuid4(),
        provider="codex",
        kind=CredentialKind.SUBSCRIPTION,
        access_token="oauth-access-token",
        api_key=None,
        provider_account_ref="acct-123",
        expires_at=None,
    )


def _sse(*events: dict) -> bytes:
    body = ""
    for event in events:
        body += f"event: {event['type']}\n"
        body += f"data: {json.dumps(event)}\n\n"
    return body.encode("utf-8")


def _mock_transport(handler, monkeypatch) -> None:
    transport = httpx.MockTransport(handler)
    real_async_client = provider_client_module.httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(provider_client_module.httpx, "AsyncClient", _factory)


async def test_forces_stream_true_and_aggregates_completed_response(monkeypatch):
    """Codex's completed-response event always carries an empty "output" array
    (verified against the live backend); the real output items only appear on
    intermediate "response.output_item.done" events, which must be collected and
    spliced into the aggregated response."""
    captured = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured["stream"] = payload["stream"]
        body = _sse(
            {"type": "response.created", "response": {"id": "resp_1"}},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "hi"}],
                },
            },
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_1",
                    "object": "response",
                    "created_at": 1_700_000_000,
                    "model": "gpt-5.5",
                    # Matches the real Codex backend: always empty here.
                    "output": [],
                    "usage": {
                        "input_tokens": 5,
                        "output_tokens": 2,
                        "total_tokens": 7,
                    },
                },
            },
        )
        return httpx.Response(200, content=body)

    _mock_transport(_handler, monkeypatch)

    settings = _settings()
    entry = get_provider(ProviderId.CODEX, settings=settings)
    adapter = CodexAdapter()
    provider_body = {"model": "gpt-5.5", "input": [], "stream": False, "store": False}

    result = await call_upstream(entry, _material(), adapter, provider_body, settings=settings)

    # The body Gozar sent upstream had "stream" forced to true even though the
    # adapter's translated body said false.
    assert captured["stream"] is True
    # The aggregated result is the completed response object, ready for the
    # adapter's normal from_provider_response.
    assert result["id"] == "resp_1"
    response = adapter.from_provider_response(result)
    assert response.choices[0].message.content == "hi"
    assert response.usage.total_tokens == 7


async def test_aggregates_multiple_output_items_in_order(monkeypatch):
    """Multiple "response.output_item.done" events (e.g. a function_call followed
    by a message) are all collected, in order, into the aggregated response."""

    def _handler(request: httpx.Request) -> httpx.Response:
        body = _sse(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "get_weather",
                    "arguments": "{}",
                },
            },
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "checking"}],
                },
            },
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_2",
                    "created_at": 1_700_000_000,
                    "model": "gpt-5.5",
                    "output": [],
                    "usage": {
                        "input_tokens": 5,
                        "output_tokens": 2,
                        "total_tokens": 7,
                    },
                },
            },
        )
        return httpx.Response(200, content=body)

    _mock_transport(_handler, monkeypatch)

    settings = _settings()
    entry = get_provider(ProviderId.CODEX, settings=settings)
    adapter = CodexAdapter()
    provider_body = {"model": "gpt-5.5", "input": [], "stream": False, "store": False}

    result = await call_upstream(entry, _material(), adapter, provider_body, settings=settings)

    assert [item["type"] for item in result["output"]] == ["function_call", "message"]
    response = adapter.from_provider_response(result)
    assert response.choices[0].message.tool_calls[0]["function"]["name"] == "get_weather"
    assert response.choices[0].message.content == "checking"


async def test_raises_upstream_error_when_stream_ends_without_completion(monkeypatch):
    def _handler(request: httpx.Request) -> httpx.Response:
        body = _sse({"type": "response.created", "response": {"id": "resp_1"}})
        return httpx.Response(200, content=body)

    _mock_transport(_handler, monkeypatch)

    settings = _settings()
    entry = get_provider(ProviderId.CODEX, settings=settings)
    adapter = CodexAdapter()
    provider_body = {"model": "gpt-5.5", "input": [], "stream": False, "store": False}

    try:
        await call_upstream(entry, _material(), adapter, provider_body, settings=settings)
        assert False, "expected UpstreamError"
    except UpstreamError as exc:
        assert "without a completed response" in str(exc)


async def test_raises_upstream_error_on_response_failed_event(monkeypatch):
    def _handler(request: httpx.Request) -> httpx.Response:
        body = _sse(
            {
                "type": "response.failed",
                "response": {"error": {"message": "boom"}},
            }
        )
        return httpx.Response(200, content=body)

    _mock_transport(_handler, monkeypatch)

    settings = _settings()
    entry = get_provider(ProviderId.CODEX, settings=settings)
    adapter = CodexAdapter()
    provider_body = {"model": "gpt-5.5", "input": [], "stream": False, "store": False}

    try:
        await call_upstream(entry, _material(), adapter, provider_body, settings=settings)
        assert False, "expected UpstreamError"
    except UpstreamError as exc:
        assert "failed response" in str(exc)
