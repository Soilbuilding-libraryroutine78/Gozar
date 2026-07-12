"""Property-based tests for Translation_Layer response translation (Property 15).

These tests validate design Property 15: for any provider response, translating it
back to an OpenAI Chat Completions response (or SSE stream) produces a valid
OpenAI-shaped response; and for the ``OpenAICompatAdapter`` (pass-through) the
response is returned unchanged (identity) aside from header/auth substitution.

The validity check is dialect-agnostic: a translated response is "valid OpenAI
shape" when it is an :class:`OpenAIChatResponse` (or :class:`OpenAIStreamChunk`)
that round-trips through Pydantic validation and JSON serialization, every choice
carries a non-empty string role and a ``None``/string finish reason, and any usage
counts are non-negative. The pass-through identity check asserts that the
``OpenAICompatAdapter`` mutates nothing: a parsed model is returned by identity and
a raw mapping round-trips byte-for-byte through ``model_dump``.

The Codex and Anthropic adapters are exercised with smart generators that emit
their native response/stream shapes so translation is checked across the whole
input space, not just hand-picked examples.
"""

from __future__ import annotations

import json

from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from gozar.translation import (
    AnthropicAdapter,
    CodexAdapter,
    OpenAICompatAdapter,
)
from gozar.translation.types import (
    OpenAIChatMessage,
    OpenAIChatResponse,
    OpenAIResponseChoice,
    OpenAIStreamChoice,
    OpenAIStreamChunk,
    UsageCounts,
)

# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

# Two disjoint alphabets so generated reasoning content can never collide with
# any other generated text. This lets the reasoning-drop check use an exact
# substring assertion without spurious failures.
_SAFE_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789 "
_REASONING_SENTINEL = "ZZQREASONINGQZZ"

# Plain text that never contains the reasoning sentinel.
_safe_text = st.text(alphabet=_SAFE_ALPHABET, min_size=0, max_size=40)
# Non-negative token counts within a realistic, well-conditioned range.
_count = st.integers(min_value=0, max_value=10_000_000)
_finish = st.sampled_from([None, "stop", "length", "tool_calls", "content_filter"])


@st.composite
def _usage_counts(draw: st.DrawFn) -> UsageCounts:
    prompt = draw(_count)
    completion = draw(_count)
    return UsageCounts(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
    )


@st.composite
def _openai_response(draw: st.DrawFn) -> OpenAIChatResponse:
    """A valid, already-OpenAI-shaped Chat Completions response."""
    n_choices = draw(st.integers(min_value=1, max_value=3))
    choices: list[OpenAIResponseChoice] = []
    for i in range(n_choices):
        content = draw(st.one_of(st.none(), _safe_text))
        choices.append(
            OpenAIResponseChoice(
                index=i,
                message=OpenAIChatMessage(role="assistant", content=content),
                finish_reason=draw(_finish),
            )
        )
    return OpenAIChatResponse(
        id=draw(_safe_text),
        object="chat.completion",
        created=draw(_count),
        model=draw(_safe_text),
        choices=choices,
        usage=draw(st.one_of(st.none(), _usage_counts())),
    )


@st.composite
def _openai_stream_chunk(draw: st.DrawFn) -> OpenAIStreamChunk:
    """A valid, already-OpenAI-shaped SSE chunk."""
    delta = draw(
        st.sampled_from(
            [
                {"role": "assistant", "content": ""},
                {"content": "hello"},
                {},
            ]
        )
    )
    return OpenAIStreamChunk(
        id=draw(_safe_text),
        object="chat.completion.chunk",
        created=draw(_count),
        model=draw(_safe_text),
        choices=[OpenAIStreamChoice(index=0, delta=delta, finish_reason=draw(_finish))],
        usage=draw(st.one_of(st.none(), _usage_counts())),
    )


@st.composite
def _codex_response(draw: st.DrawFn) -> dict:
    """A Codex Responses-API result, possibly carrying reasoning items."""
    output: list[dict] = []
    # Reasoning items (must be dropped from the client-facing response).
    for _ in range(draw(st.integers(min_value=0, max_value=2))):
        output.append(
            {
                "type": "reasoning",
                "summary": [{"text": _REASONING_SENTINEL + draw(_safe_text)}],
            }
        )
    if draw(st.booleans()):
        output.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": draw(_safe_text)}],
            }
        )
    if draw(st.booleans()):
        output.append(
            {
                "type": "function_call",
                "call_id": draw(_safe_text),
                "name": draw(_safe_text),
                "arguments": draw(_safe_text),
            }
        )
    data: dict = {
        "id": draw(_safe_text),
        "model": draw(_safe_text),
        "created_at": draw(_count),
        "status": draw(st.sampled_from(["completed", "incomplete"])),
        "output": output,
    }
    if draw(st.booleans()):
        prompt = draw(_count)
        completion = draw(_count)
        data["usage"] = {
            "input_tokens": prompt,
            "output_tokens": completion,
            "total_tokens": prompt + completion,
        }
    return data


@st.composite
def _anthropic_response(draw: st.DrawFn) -> dict:
    """An Anthropic Messages-API result with text and/or tool_use blocks."""
    content: list[dict] = []
    if draw(st.booleans()):
        content.append({"type": "text", "text": draw(_safe_text)})
    if draw(st.booleans()):
        content.append(
            {
                "type": "tool_use",
                "id": draw(_safe_text),
                "name": draw(_safe_text),
                "input": {"k": draw(_safe_text)},
            }
        )
    data: dict = {
        "id": draw(_safe_text),
        "type": "message",
        "role": "assistant",
        "model": draw(_safe_text),
        "content": content,
        "stop_reason": draw(
            st.sampled_from([None, "end_turn", "max_tokens", "tool_use", "stop_sequence"])
        ),
    }
    if draw(st.booleans()):
        data["usage"] = {
            "input_tokens": draw(_count),
            "output_tokens": draw(_count),
        }
    return data


@st.composite
def _codex_stream_event(draw: st.DrawFn) -> dict:
    """A plausible Codex Responses streaming event of any lifecycle type."""
    etype = draw(
        st.sampled_from(
            [
                "response.output_text.delta",
                "response.reasoning_summary_text.delta",
                "response.reasoning.delta",
                "response.function_call_arguments.delta",
                "response.output_item.added",
                "response.completed",
                "response.created",
                "response.in_progress",
            ]
        )
    )
    response: dict = {
        "id": draw(_safe_text),
        "model": draw(_safe_text),
        "created_at": draw(_count),
    }
    event: dict = {"type": etype, "response": response, "delta": draw(_safe_text)}
    if etype == "response.output_item.added":
        event["item"] = {
            "type": "function_call",
            "call_id": draw(_safe_text),
            "name": draw(_safe_text),
            "arguments": draw(_safe_text),
        }
    if etype == "response.completed":
        response["usage"] = {
            "input_tokens": draw(_count),
            "output_tokens": draw(_count),
            "total_tokens": draw(_count),
        }
    return event


@st.composite
def _anthropic_stream_event(draw: st.DrawFn) -> dict:
    """A plausible Anthropic Messages streaming event of any type."""
    etype = draw(
        st.sampled_from(
            [
                "message_start",
                "content_block_start",
                "content_block_delta",
                "content_block_stop",
                "message_delta",
                "message_stop",
                "ping",
            ]
        )
    )
    event: dict = {"type": etype, "index": draw(st.integers(min_value=0, max_value=3))}
    if etype == "message_start":
        event["message"] = {"usage": {"input_tokens": draw(_count)}}
    elif etype == "content_block_start":
        if draw(st.booleans()):
            event["content_block"] = {
                "type": "tool_use",
                "id": draw(_safe_text),
                "name": draw(_safe_text),
            }
        else:
            event["content_block"] = {"type": "text", "text": ""}
    elif etype == "content_block_delta":
        dtype = draw(st.sampled_from(["text_delta", "input_json_delta", "other"]))
        if dtype == "text_delta":
            event["delta"] = {"type": "text_delta", "text": draw(_safe_text)}
        elif dtype == "input_json_delta":
            event["delta"] = {"type": "input_json_delta", "partial_json": draw(_safe_text)}
        else:
            event["delta"] = {"type": "other"}
    elif etype == "message_delta":
        event["delta"] = {
            "stop_reason": draw(
                st.sampled_from([None, "end_turn", "max_tokens", "tool_use"])
            )
        }
        if draw(st.booleans()):
            event["usage"] = {"output_tokens": draw(_count)}
    return event


# ---------------------------------------------------------------------------
# Validity helpers
# ---------------------------------------------------------------------------
def _assert_valid_openai_response(resp: object) -> None:
    """Assert ``resp`` is a valid OpenAI Chat Completions response."""
    assert isinstance(resp, OpenAIChatResponse)
    dumped = resp.model_dump()
    # Round-trips through validation and is JSON-serializable (strict OpenAI shape).
    OpenAIChatResponse.model_validate(dumped)
    json.dumps(resp.model_dump(mode="json"))
    assert isinstance(resp.id, str)
    assert isinstance(resp.created, int)
    assert isinstance(resp.model, str)
    assert isinstance(resp.choices, list)
    for choice in resp.choices:
        assert isinstance(choice.message.role, str) and choice.message.role
        assert choice.finish_reason is None or isinstance(choice.finish_reason, str)
    if resp.usage is not None:
        assert resp.usage.prompt_tokens >= 0
        assert resp.usage.completion_tokens >= 0
        assert resp.usage.total_tokens >= 0


def _assert_valid_stream_chunk(chunk: object) -> None:
    """Assert ``chunk`` is ``None`` or a valid OpenAI SSE chunk."""
    if chunk is None:
        return
    assert isinstance(chunk, OpenAIStreamChunk)
    dumped = chunk.model_dump()
    OpenAIStreamChunk.model_validate(dumped)
    json.dumps(chunk.model_dump(mode="json"))
    assert chunk.object == "chat.completion.chunk"
    for choice in chunk.choices:
        assert isinstance(choice.delta, dict)
        assert choice.finish_reason is None or isinstance(choice.finish_reason, str)
    if chunk.usage is not None:
        assert chunk.usage.prompt_tokens >= 0
        assert chunk.usage.completion_tokens >= 0
        assert chunk.usage.total_tokens >= 0


# ===========================================================================
# Pass-through identity (OpenAICompatAdapter)
# ===========================================================================

# Feature: gozar, Property 15: For any provider response, translating it back to an
# OpenAI Chat Completions response (or SSE stream) produces a valid OpenAI-shaped
# response; and for the OpenAICompatAdapter (pass-through), the response is returned
# unchanged (identity) aside from header/auth substitution.
@hyp_settings(max_examples=200)
@given(resp=_openai_response())
def test_passthrough_response_is_identity_for_parsed_model(
    resp: OpenAIChatResponse,
) -> None:
    """Validates: Requirements 7.3.

    Given an already-parsed OpenAI response, the pass-through adapter returns the
    exact same object (identity) and that object is a valid OpenAI response.
    """
    out = OpenAICompatAdapter().from_provider_response(resp)
    assert out is resp
    _assert_valid_openai_response(out)


# Feature: gozar, Property 15: For any provider response, translating it back to an
# OpenAI Chat Completions response (or SSE stream) produces a valid OpenAI-shaped
# response; and for the OpenAICompatAdapter (pass-through), the response is returned
# unchanged (identity) aside from header/auth substitution.
@hyp_settings(max_examples=200)
@given(resp=_openai_response())
def test_passthrough_response_roundtrips_raw_mapping_unchanged(
    resp: OpenAIChatResponse,
) -> None:
    """Validates: Requirements 7.3.

    Given a raw OpenAI-shaped mapping (decoded upstream JSON), the pass-through
    adapter reproduces it byte-for-byte after a parse/serialize round-trip: nothing
    is added, dropped, or mutated.
    """
    raw = resp.model_dump()
    out = OpenAICompatAdapter().from_provider_response(raw)
    _assert_valid_openai_response(out)
    assert out.model_dump() == raw


# Feature: gozar, Property 15: For any provider response, translating it back to an
# OpenAI Chat Completions response (or SSE stream) produces a valid OpenAI-shaped
# response; and for the OpenAICompatAdapter (pass-through), the response is returned
# unchanged (identity) aside from header/auth substitution.
@hyp_settings(max_examples=200)
@given(chunk=_openai_stream_chunk())
def test_passthrough_stream_chunk_is_identity(chunk: OpenAIStreamChunk) -> None:
    """Validates: Requirements 7.3.

    The pass-through adapter returns each parsed SSE chunk unchanged (identity) and
    the chunk is a valid OpenAI SSE chunk.
    """
    out = OpenAICompatAdapter().from_provider_stream_chunk(chunk)
    assert out is chunk
    _assert_valid_stream_chunk(out)


# ===========================================================================
# Response translation validity (Codex adapter)
# ===========================================================================

# Feature: gozar, Property 15: For any provider response, translating it back to an
# OpenAI Chat Completions response (or SSE stream) produces a valid OpenAI-shaped
# response; and for the OpenAICompatAdapter (pass-through), the response is returned
# unchanged (identity) aside from header/auth substitution.
@hyp_settings(max_examples=200)
@given(data=_codex_response())
def test_codex_response_translates_to_valid_openai(data: dict) -> None:
    """Validates: Requirements 7.3.

    Translating any Codex Responses-API result yields a valid OpenAI Chat response,
    and reasoning content never leaks into the client-facing output.
    """
    out = CodexAdapter().from_provider_response(data)
    _assert_valid_openai_response(out)
    assert _REASONING_SENTINEL not in json.dumps(out.model_dump(mode="json"))


# Feature: gozar, Property 15: For any provider response, translating it back to an
# OpenAI Chat Completions response (or SSE stream) produces a valid OpenAI-shaped
# response; and for the OpenAICompatAdapter (pass-through), the response is returned
# unchanged (identity) aside from header/auth substitution.
@hyp_settings(max_examples=200)
@given(event=_codex_stream_event())
def test_codex_stream_chunk_translates_to_valid_openai(event: dict) -> None:
    """Validates: Requirements 7.3.

    Translating any Codex Responses streaming event yields either ``None`` (no
    client-facing content, e.g. reasoning events) or a valid OpenAI SSE chunk.
    """
    out = CodexAdapter().from_provider_stream_chunk(event)
    _assert_valid_stream_chunk(out)
    if str(event.get("type", "")).find("reasoning") != -1:
        assert out is None


# ===========================================================================
# Response translation validity (Anthropic adapter)
# ===========================================================================

# Feature: gozar, Property 15: For any provider response, translating it back to an
# OpenAI Chat Completions response (or SSE stream) produces a valid OpenAI-shaped
# response; and for the OpenAICompatAdapter (pass-through), the response is returned
# unchanged (identity) aside from header/auth substitution.
@hyp_settings(max_examples=200)
@given(data=_anthropic_response())
def test_anthropic_response_translates_to_valid_openai(data: dict) -> None:
    """Validates: Requirements 7.3.

    Translating any Anthropic Messages-API result yields a valid OpenAI Chat
    response.
    """
    out = AnthropicAdapter().from_provider_response(data)
    _assert_valid_openai_response(out)


# Feature: gozar, Property 15: For any provider response, translating it back to an
# OpenAI Chat Completions response (or SSE stream) produces a valid OpenAI-shaped
# response; and for the OpenAICompatAdapter (pass-through), the response is returned
# unchanged (identity) aside from header/auth substitution.
@hyp_settings(max_examples=200)
@given(event=_anthropic_stream_event())
def test_anthropic_stream_chunk_translates_to_valid_openai(event: dict) -> None:
    """Validates: Requirements 7.3.

    Translating any Anthropic Messages streaming event yields either ``None`` (no
    client-facing content) or a valid OpenAI SSE chunk.
    """
    out = AnthropicAdapter().from_provider_stream_chunk(event)
    _assert_valid_stream_chunk(out)
