"""Tests for the SSE streaming path of the Proxy_Gateway (Requirement 6.3).

Covers three layers without touching the network:

* the upstream SSE byte-stream parser (:func:`gozar.gateway.streaming.iter_sse_data`)
  and the OpenAI framing helpers, with unit examples for the wire-format edge cases;
* the streaming pipeline (:func:`gozar.gateway.pipeline.stream_chat_completion`) with
  an injected streaming upstream, exercising success, fallback-on-establishment, and
  terminal errors, plus end-of-stream usage/trace recording.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select

from gozar.accounts.models import CredentialKind, CredentialStatus, UpstreamCredential
from gozar.accounts.service import ProviderCredentialMaterial
from gozar.core.errors import AuthError, NoAvailableAccount, UpstreamError
from gozar.gateway.pipeline import stream_chat_completion
from gozar.gateway.streaming import (
    SSE_DONE,
    format_sse_chunk,
    is_done,
    iter_sse_data,
)
from gozar.routing.service import create_chain
from gozar.tokens.service import create_token
from gozar.translation.types import OpenAIChatRequest, OpenAIStreamChunk
from gozar.usage.models import TraceLog, UsageRecord

from conftest import material_for


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def _byte_iter(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def _collect(aiter: AsyncIterator[str]) -> list[str]:
    return [item async for item in aiter]


def _sse_event(payload: dict, model: str = "gpt-4o") -> bytes:
    base = {
        "id": "chatcmpl-stream",
        "object": "chat.completion.chunk",
        "created": 1_700_000_000,
        "model": model,
    }
    return f"data: {json.dumps({**base, **payload})}\n\n".encode()


def _openai_stream_bytes(content: str = "hi", with_usage: bool = True) -> list[bytes]:
    """A minimal OpenAI-compatible SSE stream: role, content, stop, [DONE]."""
    events = [
        _sse_event(
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                          "finish_reason": None}]}
        ),
        _sse_event(
            {"choices": [{"index": 0, "delta": {"content": content},
                          "finish_reason": None}]}
        ),
    ]
    final: dict = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    if with_usage:
        final["usage"] = {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}
    events.append(_sse_event(final))
    events.append(b"data: [DONE]\n\n")
    return events


# --------------------------------------------------------------------------- #
# SSE parser + framing unit tests
# --------------------------------------------------------------------------- #
async def test_iter_sse_data_yields_each_event_payload():
    chunks = [b'data: {"a": 1}\n\n', b'data: {"b": 2}\n\n']
    assert await _collect(iter_sse_data(_byte_iter(chunks))) == ['{"a": 1}', '{"b": 2}']


async def test_iter_sse_data_reassembles_events_split_across_byte_chunks():
    # One logical event delivered in three arbitrary byte slices.
    chunks = [b'data: {"hel', b'lo": "wor', b'ld"}\n\n']
    assert await _collect(iter_sse_data(_byte_iter(chunks))) == ['{"hello": "world"}']


async def test_iter_sse_data_joins_multiple_data_lines_and_skips_comments():
    chunks = [b": keep-alive\ndata: line1\ndata: line2\n\n"]
    assert await _collect(iter_sse_data(_byte_iter(chunks))) == ["line1\nline2"]


async def test_iter_sse_data_ignores_non_data_fields():
    # Anthropic-style framing: an event: line precedes the data: line.
    chunks = [b'event: message_start\ndata: {"type": "message_start"}\n\n']
    assert await _collect(iter_sse_data(_byte_iter(chunks))) == [
        '{"type": "message_start"}'
    ]


async def test_iter_sse_data_flushes_trailing_event_without_blank_line():
    chunks = [b"data: [DONE]"]
    assert await _collect(iter_sse_data(_byte_iter(chunks))) == ["[DONE]"]


def test_is_done_detects_terminator():
    assert is_done("[DONE]") is True
    assert is_done("  [DONE]  ") is True
    assert is_done('{"id": "x"}') is False


def test_format_sse_chunk_frames_compact_json_with_blank_line():
    chunk = OpenAIStreamChunk.model_validate(
        {
            "id": "x",
            "created": 1,
            "model": "gpt-4o",
            "choices": [{"index": 0, "delta": {"content": "hi"},
                         "finish_reason": None}],
        }
    )
    framed = format_sse_chunk(chunk)
    assert framed.startswith("data: ")
    assert framed.endswith("\n\n")
    # finish_reason is None -> dropped by exclude_none; compact separators.
    assert "finish_reason" not in framed
    assert ", " not in framed.split("data: ", 1)[1]


def test_sse_done_constant():
    assert SSE_DONE == "data: [DONE]\n\n"


# --------------------------------------------------------------------------- #
# Streaming pipeline fakes
# --------------------------------------------------------------------------- #
class FakeStreamUpstream:
    """Injected streaming caller: records calls and can fail establishment per account."""

    def __init__(self, *, fail_for=frozenset(), chunks: list[bytes] | None = None):
        self.calls: list[uuid.UUID] = []
        self._fail_for = set(fail_for)
        self._chunks = chunks if chunks is not None else _openai_stream_bytes()

    def __call__(self, entry, material, adapter, body) -> AsyncIterator[bytes]:
        self.calls.append(material.account_id)
        return self._gen(material.account_id in self._fail_for)

    async def _gen(self, fail: bool) -> AsyncIterator[bytes]:
        if fail:
            # Surfaces on the first __anext__, exactly like a non-retryable upstream
            # status raised by the real client before any byte is forwarded.
            raise UpstreamError("simulated stream establishment failure")
        for raw in self._chunks:
            yield raw


class FailsFirstStreamWithStatus:
    """Injected stream caller that fails first establishment with an upstream status."""

    def __init__(self, status_code: int, *, chunks: list[bytes] | None = None):
        self.calls: list[tuple[uuid.UUID, str | None]] = []
        self._status_code = status_code
        self._chunks = chunks if chunks is not None else _openai_stream_bytes()

    def __call__(self, entry, material, adapter, body) -> AsyncIterator[bytes]:
        self.calls.append((material.account_id, material.access_token))
        return self._gen(entry.provider_id.value, len(self.calls) == 1)

    async def _gen(self, provider: str, fail: bool) -> AsyncIterator[bytes]:
        if fail:
            raise UpstreamError(
                f"upstream provider {provider!r} returned status {self._status_code}",
                details=[{"upstream_status": self._status_code}],
            )
        for raw in self._chunks:
            yield raw


def _acquire_fake(provider: str = "openai"):
    async def acquire(session, account_id):
        return material_for(account_id, provider=provider)

    return acquire


def _refreshable_subscription_acquire(refreshed: dict[str, bool]):
    async def acquire(session, account_id):
        token = "access-new" if refreshed["done"] else "access-old"
        return ProviderCredentialMaterial(
            account_id=account_id,
            provider="openai",
            kind=CredentialKind.SUBSCRIPTION,
            access_token=token,
            api_key=None,
            provider_account_ref=None,
            expires_at=None,
        )

    return acquire


async def _add_account(
    session, *, status: CredentialStatus = CredentialStatus.ACTIVE
) -> uuid.UUID:
    account_id = uuid.uuid4()
    session.add(
        UpstreamCredential(
            id=account_id,
            provider="openai",
            kind=CredentialKind.API_KEY,
            label=f"acct-{account_id.hex[:6]}",
            status=status,
        )
    )
    await session.flush()
    return account_id


async def _issue_token(session, settings) -> str:
    issued = await create_token(session, "stream-token", None, settings=settings)
    return issued.secret


def _request() -> OpenAIChatRequest:
    return OpenAIChatRequest(
        model="gpt-4o",
        messages=[{"role": "user", "content": "ping"}],
        stream=True,
    )


# --------------------------------------------------------------------------- #
# Streaming pipeline behavior
# --------------------------------------------------------------------------- #
async def test_stream_success_emits_chunks_and_done(session, redis, settings):
    token = await _issue_token(session, settings)
    account_id = await _add_account(session)
    await create_chain(session, "default", [account_id])

    upstream = FakeStreamUpstream()
    sse = await stream_chat_completion(
        session,
        presented_token=token,
        request=_request(),
        redis=redis,
        settings=settings,
        stream_upstream=upstream,
        acquire_material=_acquire_fake(),
    )
    events = await _collect(sse)

    assert upstream.calls == [account_id]
    # Last event is the OpenAI terminator; content delta forwarded.
    assert events[-1] == SSE_DONE
    body = "".join(events)
    assert "hi" in body
    assert body.count("data: [DONE]") == 1


async def test_stream_records_usage_and_finalizes_trace(session, redis, settings):
    token = await _issue_token(session, settings)
    account_id = await _add_account(session)
    await create_chain(session, "default", [account_id])

    sse = await stream_chat_completion(
        session,
        presented_token=token,
        request=_request(),
        redis=redis,
        settings=settings,
        stream_upstream=FakeStreamUpstream(),
        acquire_material=_acquire_fake(),
    )
    await _collect(sse)  # draining the stream triggers end-of-stream recording

    record = (await session.scalars(select(UsageRecord))).one()
    assert record.account_id == account_id
    assert record.total_tokens == 6
    assert record.provider_metering_missing is False

    trace = (await session.scalars(select(TraceLog))).one()
    assert trace.outcome == "success"
    assert trace.status_code == 200
    assert trace.account_id == account_id


async def test_stream_missing_usage_flagged(session, redis, settings):
    token = await _issue_token(session, settings)
    account_id = await _add_account(session)
    await create_chain(session, "default", [account_id])

    sse = await stream_chat_completion(
        session,
        presented_token=token,
        request=_request(),
        redis=redis,
        settings=settings,
        stream_upstream=FakeStreamUpstream(chunks=_openai_stream_bytes(with_usage=False)),
        acquire_material=_acquire_fake(),
    )
    await _collect(sse)

    record = (await session.scalars(select(UsageRecord))).one()
    assert record.total_tokens == 0
    assert record.provider_metering_missing is True


async def test_stream_falls_back_on_establishment_failure(session, redis, settings):
    token = await _issue_token(session, settings)
    first = await _add_account(session)
    second = await _add_account(session)
    await create_chain(session, "default", [first, second])

    upstream = FakeStreamUpstream(fail_for={first})
    sse = await stream_chat_completion(
        session,
        presented_token=token,
        request=_request(),
        redis=redis,
        settings=settings,
        stream_upstream=upstream,
        acquire_material=_acquire_fake(),
    )
    events = await _collect(sse)

    # First credential failed to establish; the second served the stream.
    assert upstream.calls == [first, second]
    assert events[-1] == SSE_DONE

    record = (await session.scalars(select(UsageRecord))).one()
    assert record.account_id == second

    trace = (await session.scalars(select(TraceLog))).one()
    routing = trace.outbound_meta["routing"]
    assert routing["attempt_count"] == 2
    assert routing["attempts"][0]["outcome"] == "error"
    assert routing["attempts"][0]["fallback_taken"] is True
    assert routing["attempts"][1]["outcome"] == "success"
    assert routing["attempts"][1]["usage"]["total_tokens"] == 6


async def test_stream_subscription_401_refreshes_before_first_byte(
    session, redis, settings
):
    token = await _issue_token(session, settings)
    account_id = await _add_account(session)
    await create_chain(session, "default", [account_id])

    refreshed = {"done": False}
    refresh_calls: list[uuid.UUID] = []

    async def refresh_on_auth_error(session, refresh_account_id):
        refresh_calls.append(refresh_account_id)
        refreshed["done"] = True
        return True

    upstream = FailsFirstStreamWithStatus(401)
    sse = await stream_chat_completion(
        session,
        presented_token=token,
        request=_request(),
        redis=redis,
        settings=settings,
        stream_upstream=upstream,
        acquire_material=_refreshable_subscription_acquire(refreshed),
        refresh_on_auth_error=refresh_on_auth_error,
    )
    events = await _collect(sse)

    assert events[-1] == SSE_DONE
    assert refresh_calls == [account_id]
    assert upstream.calls == [
        (account_id, "access-old"),
        (account_id, "access-new"),
    ]


async def test_stream_all_fallbacks_failed(session, redis, settings):
    token = await _issue_token(session, settings)
    first = await _add_account(session)
    second = await _add_account(session)
    await create_chain(session, "default", [first, second])

    upstream = FakeStreamUpstream(fail_for={first, second})
    with pytest.raises(UpstreamError):
        await stream_chat_completion(
            session,
            presented_token=token,
            request=_request(),
            redis=redis,
            settings=settings,
            stream_upstream=upstream,
            acquire_material=_acquire_fake(),
        )
    assert upstream.calls == [first, second]

    trace = (await session.scalars(select(TraceLog))).one()
    assert trace.outcome == "all_fallbacks_failed"


async def test_stream_missing_token_rejected_without_upstream(session, redis, settings):
    upstream = FakeStreamUpstream()
    with pytest.raises(AuthError):
        await stream_chat_completion(
            session,
            presented_token=None,
            request=_request(),
            redis=redis,
            settings=settings,
            stream_upstream=upstream,
            acquire_material=_acquire_fake(),
        )
    assert upstream.calls == []

    trace = (await session.scalars(select(TraceLog))).one()
    assert trace.outcome == "client_error"
    assert trace.status_code == 401


async def test_stream_no_chain_yields_no_available_account(session, redis, settings):
    token = await _issue_token(session, settings)
    upstream = FakeStreamUpstream()
    with pytest.raises(NoAvailableAccount):
        await stream_chat_completion(
            session,
            presented_token=token,
            request=_request(),
            redis=redis,
            settings=settings,
            stream_upstream=upstream,
            acquire_material=_acquire_fake(),
        )
    assert upstream.calls == []
