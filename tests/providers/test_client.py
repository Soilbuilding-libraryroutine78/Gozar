"""Unit tests for the resilient upstream provider client.

All tests mock at the transport level with :class:`httpx.MockTransport`; no test
touches the network. They cover the behaviours required by task 6.2:

* a successful non-streaming request returns the response,
* a transient 429 then 503 are retried and eventually succeed,
* exhausting retries on a transient status raises :class:`UpstreamError`,
* a non-retryable 4xx is surfaced immediately without retry,
* connection/transport errors are retried and then give up with ``UpstreamError``,
* streaming passes chunks through without buffering the whole body,
* errors never leak the caller-supplied auth header (no secrets in the error).

Backoff sleeps are replaced with a fake recorder so tests are instant and can also
assert that retries actually waited.
"""

from __future__ import annotations

import random

import httpx
import pytest

from gozar.core.config import Settings
from gozar.core.errors import UpstreamError
from gozar.providers.client import UpstreamClient
from gozar.providers.registry import ProviderId, get_provider

SECRET_TOKEN = "super-secret-bearer-token-value"
AUTH_HEADERS = {"Authorization": f"Bearer {SECRET_TOKEN}"}


def _settings(**overrides) -> Settings:
    base = {
        "provider_base_urls": {"openai": "https://api.openai.com/v1"},
        "upstream_request_timeout_seconds": 5.0,
        "upstream_max_attempts": 3,
        "upstream_backoff_base_seconds": 0.5,
        "upstream_backoff_max_seconds": 10.0,
    }
    base.update(overrides)
    return Settings(**base)


def _entry(settings: Settings):
    return get_provider(ProviderId.OPENAI, settings=settings)


class _FakeSleep:
    """Records requested delays instead of actually sleeping."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _make_client(settings: Settings, handler, sleep=None) -> UpstreamClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    return UpstreamClient(
        _entry(settings),
        settings=settings,
        client=http_client,
        sleep=sleep or _FakeSleep(),
        rng=random.Random(1234),  # deterministic jitter
    )


async def test_request_success_returns_response():
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://api.openai.com/v1/chat/completions")
        assert request.headers["Authorization"] == f"Bearer {SECRET_TOKEN}"
        return httpx.Response(200, json={"ok": True})

    client = _make_client(settings, handler)
    try:
        resp = await client.request(
            "POST", "/chat/completions", headers=AUTH_HEADERS, json={"model": "gpt"}
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
    finally:
        await client._client.aclose()


async def test_request_retries_transient_then_succeeds():
    settings = _settings()
    statuses = [429, 503, 200]
    sleep = _FakeSleep()

    def handler(request: httpx.Request) -> httpx.Response:
        status = statuses.pop(0)
        if status == 200:
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(status, json={"error": "transient"})

    client = _make_client(settings, handler, sleep=sleep)
    try:
        resp = await client.request("POST", "/v1/x", headers=AUTH_HEADERS, json={})
        assert resp.status_code == 200
        assert statuses == []  # all three responses consumed
        assert len(sleep.delays) == 2  # two backoff waits before the success
        assert all(d > 0 for d in sleep.delays)
    finally:
        await client._client.aclose()


async def test_request_gives_up_after_max_attempts():
    settings = _settings(upstream_max_attempts=3)
    calls = {"n": 0}
    sleep = _FakeSleep()

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="still down")

    client = _make_client(settings, handler, sleep=sleep)
    try:
        with pytest.raises(UpstreamError) as exc_info:
            await client.request("POST", "/v1/x", headers=AUTH_HEADERS, json={})
        # Exactly max_attempts upstream calls, with max_attempts-1 backoff waits.
        assert calls["n"] == 3
        assert len(sleep.delays) == 2
        assert exc_info.value.status_code == 502
        assert {"upstream_status": 503} in exc_info.value.details
    finally:
        await client._client.aclose()


async def test_request_non_retryable_4xx_raises_immediately():
    settings = _settings()
    calls = {"n": 0}
    sleep = _FakeSleep()

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": "bad request"})

    client = _make_client(settings, handler, sleep=sleep)
    try:
        with pytest.raises(UpstreamError) as exc_info:
            await client.request("POST", "/v1/x", headers=AUTH_HEADERS, json={})
        assert calls["n"] == 1  # no retry on a non-429 4xx
        assert sleep.delays == []
        assert {"upstream_status": 400} in exc_info.value.details
    finally:
        await client._client.aclose()


async def test_request_retries_connection_error_then_gives_up():
    settings = _settings(upstream_max_attempts=3)
    calls = {"n": 0}
    sleep = _FakeSleep()

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("connection refused", request=request)

    client = _make_client(settings, handler, sleep=sleep)
    try:
        with pytest.raises(UpstreamError) as exc_info:
            await client.request("POST", "/v1/x", headers=AUTH_HEADERS, json={})
        assert calls["n"] == 3  # retried up to the limit
        assert len(sleep.delays) == 2
        assert exc_info.value.details[0]["attempts"] == 3
    finally:
        await client._client.aclose()


async def test_connection_error_then_success():
    settings = _settings()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("reset", request=request)
        return httpx.Response(200, json={"ok": True})

    client = _make_client(settings, handler)
    try:
        resp = await client.request("GET", "/models", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        assert calls["n"] == 2
    finally:
        await client._client.aclose()


async def test_stream_passes_chunks_through():
    settings = _settings()
    payload = [b"data: one\n\n", b"data: two\n\n", b"data: [DONE]\n\n"]

    # Build a streaming response from an async byte iterator.
    async def byte_iter():
        for part in payload:
            yield part

    def stream_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {SECRET_TOKEN}"
        return httpx.Response(200, content=byte_iter())

    client = _make_client(settings, stream_handler)
    try:
        received: list[bytes] = []
        async for chunk in client.stream(
            "POST", "/chat/completions", headers=AUTH_HEADERS, json={"stream": True}
        ):
            received.append(chunk)
        joined = b"".join(received)
        assert b"data: [DONE]" in joined
        assert joined == b"".join(payload)
    finally:
        await client._client.aclose()


async def test_stream_retries_transient_status_before_any_bytes():
    settings = _settings()
    calls = {"n": 0}
    sleep = _FakeSleep()

    async def good_iter():
        yield b"data: hello\n\n"
        yield b"data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="warming up")
        return httpx.Response(200, content=good_iter())

    client = _make_client(settings, handler, sleep=sleep)
    try:
        received = [chunk async for chunk in client.stream(
            "POST", "/v1/x", headers=AUTH_HEADERS, json={}
        )]
        assert calls["n"] == 2
        assert len(sleep.delays) == 1
        assert b"".join(received) == b"data: hello\n\ndata: [DONE]\n\n"
    finally:
        await client._client.aclose()


async def test_error_contains_no_secret_header_value():
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal upstream failure")

    client = _make_client(settings, handler)
    try:
        with pytest.raises(UpstreamError) as exc_info:
            await client.request("POST", "/v1/x", headers=AUTH_HEADERS, json={})
        err = exc_info.value
        rendered = str(err.message) + str(err.details)
        assert SECRET_TOKEN not in rendered
        # The (non-secret) provider body may appear; the credential must not.
        assert "Bearer" not in rendered
    finally:
        await client._client.aclose()
