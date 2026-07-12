"""Unit/example tests for the Codex adapter (OpenAI Chat <-> Codex Responses API).

These focus on the mappings called out by the task: request mapping preserves
messages and tools (system hoisted to ``instructions``, tool calls/results mapped
to Responses items), reasoning is dropped from client-facing output, streaming
reasoning events are dropped, and usage extraction maps Responses token counts to
:class:`UsageCounts` (zeros when absent). The property-based tests for the
Translation_Layer are tasks 7.4 / 7.5 and are intentionally not written here.
"""

from __future__ import annotations

from gozar.providers import registry
from gozar.providers.registry import AdapterKind
from gozar.translation import CodexAdapter
from gozar.translation.codex import CodexAdapter as CodexAdapterDirect
from gozar.translation.types import (
    OpenAIChatRequest,
    OpenAIStreamChunk,
    UsageCounts,
)


def _adapter() -> CodexAdapter:
    return CodexAdapter()


# ---------------------------------------------------------------------------
# Request mapping
# ---------------------------------------------------------------------------
def test_request_hoists_system_and_preserves_user_assistant_messages():
    req = OpenAIChatRequest.model_validate(
        {
            "model": "gpt-5-codex",
            "messages": [
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
                {"role": "user", "content": "bye"},
            ],
        }
    )

    out = _adapter().to_provider_request(req)

    assert out["model"] == "gpt-5-codex"
    assert out["instructions"] == "be terse"
    # System message is hoisted out of input; the three non-system messages remain
    # as input items in order.
    assert [item["role"] for item in out["input"]] == ["user", "assistant", "user"]


def test_request_always_sets_store_false():
    """The Codex Responses backend rejects any request that omits ``store: false``
    (a ChatGPT-subscription session has no conversation-store concept, and
    ``store: true`` is never accepted -- see the community-reported 400 "Store must
    be set to false"). The adapter must always send it explicitly."""
    req = OpenAIChatRequest.model_validate(
        {
            "model": "gpt-5-codex",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )

    out = _adapter().to_provider_request(req)

    assert out["store"] is False


def test_request_joins_multiple_system_messages_into_instructions():
    req = OpenAIChatRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {"role": "system", "content": "rule one"},
                {"role": "developer", "content": "rule two"},
                {"role": "user", "content": "go"},
            ],
        }
    )
    out = _adapter().to_provider_request(req)
    assert out["instructions"] == "rule one\n\nrule two"


def test_request_maps_tools_and_generation_params():
    req = OpenAIChatRequest.model_validate(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.3,
            "max_tokens": 256,
            "top_p": 0.9,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Look up weather",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "tool_choice": "auto",
        }
    )

    out = _adapter().to_provider_request(req)

    assert out["temperature"] == 0.3
    # OpenAI max_tokens -> Responses max_output_tokens.
    assert out["max_output_tokens"] == 256
    # Extra OpenAI generation params are carried through.
    assert out["top_p"] == 0.9
    assert out["tool_choice"] == "auto"
    # Tool definition flattened into the Responses tool shape.
    assert out["tools"] == [
        {
            "type": "function",
            "name": "get_weather",
            "description": "Look up weather",
            "parameters": {"type": "object", "properties": {}},
        }
    ]


def test_request_maps_tool_calls_and_tool_results():
    req = OpenAIChatRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {"role": "user", "content": "weather?"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": '{"city":"NYC"}',
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
            ],
        }
    )

    items = _adapter().to_provider_request(req)["input"]

    # user message, then function_call, then function_call_output.
    assert items[0]["type"] == "message" and items[0]["role"] == "user"
    assert items[1] == {
        "type": "function_call",
        "call_id": "call_1",
        "name": "get_weather",
        "arguments": '{"city":"NYC"}',
    }
    assert items[2] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "sunny",
    }


# ---------------------------------------------------------------------------
# Response mapping (reasoning dropped)
# ---------------------------------------------------------------------------
def test_response_drops_reasoning_and_keeps_text():
    provider_resp = {
        "id": "resp_123",
        "model": "gpt-5-codex",
        "created_at": 1700,
        "status": "completed",
        "output": [
            {"type": "reasoning", "summary": [{"text": "secret chain of thought"}]},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "the answer is 42"}],
            },
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }

    resp = _adapter().from_provider_response(provider_resp)

    assert resp.id == "resp_123"
    assert resp.object == "chat.completion"
    assert resp.created == 1700
    assert resp.model == "gpt-5-codex"
    choice = resp.choices[0]
    assert choice.message.role == "assistant"
    assert choice.message.content == "the answer is 42"
    assert choice.finish_reason == "stop"
    # Reasoning text must not leak into the client-facing response.
    assert "secret chain of thought" not in (choice.message.content or "")
    assert resp.usage == UsageCounts(
        prompt_tokens=10, completion_tokens=5, total_tokens=15
    )


def test_response_maps_function_call_to_tool_calls():
    provider_resp = {
        "id": "resp_t",
        "model": "m",
        "output": [
            {"type": "reasoning", "summary": []},
            {
                "type": "function_call",
                "call_id": "call_9",
                "name": "do_thing",
                "arguments": '{"x":1}',
            },
        ],
    }

    resp = _adapter().from_provider_response(provider_resp)
    choice = resp.choices[0]

    assert choice.finish_reason == "tool_calls"
    assert choice.message.content is None
    assert choice.message.tool_calls == [
        {
            "id": "call_9",
            "type": "function",
            "function": {"name": "do_thing", "arguments": '{"x":1}'},
        }
    ]


def test_response_maps_incomplete_max_tokens_to_length():
    provider_resp = {
        "id": "r",
        "model": "m",
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "truncated"}],
            }
        ],
    }
    resp = _adapter().from_provider_response(provider_resp)
    assert resp.choices[0].finish_reason == "length"


# ---------------------------------------------------------------------------
# Usage extraction
# ---------------------------------------------------------------------------
def test_extract_usage_zero_when_absent():
    assert _adapter().extract_usage({"id": "x", "output": []}) == UsageCounts()


def test_extract_usage_computes_total_when_missing():
    usage = _adapter().extract_usage(
        {"usage": {"input_tokens": 7, "output_tokens": 3}}
    )
    assert usage == UsageCounts(prompt_tokens=7, completion_tokens=3, total_tokens=10)


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------
def test_stream_text_delta_maps_to_content_chunk():
    event = {
        "type": "response.output_text.delta",
        "delta": "hel",
        "response": {"id": "resp_s", "model": "m", "created_at": 99},
    }
    chunk = _adapter().from_provider_stream_chunk(event)
    assert isinstance(chunk, OpenAIStreamChunk)
    assert chunk.id == "resp_s"
    assert chunk.model == "m"
    assert chunk.created == 99
    assert chunk.choices[0].delta == {"content": "hel"}
    assert chunk.choices[0].finish_reason is None


def test_stream_reasoning_event_is_dropped():
    assert (
        _adapter().from_provider_stream_chunk(
            {"type": "response.reasoning_summary_text.delta", "delta": "thinking"}
        )
        is None
    )
    assert (
        _adapter().from_provider_stream_chunk(
            {"type": "response.reasoning.delta", "delta": "more thinking"}
        )
        is None
    )


def test_stream_completed_emits_finish_and_usage():
    event = {
        "type": "response.completed",
        "response": {
            "id": "resp_done",
            "model": "m",
            "created_at": 5,
            "usage": {"input_tokens": 2, "output_tokens": 4, "total_tokens": 6},
        },
    }
    chunk = _adapter().from_provider_stream_chunk(event)
    assert chunk is not None
    assert chunk.choices[0].finish_reason == "stop"
    assert chunk.usage == UsageCounts(
        prompt_tokens=2, completion_tokens=4, total_tokens=6
    )


def test_stream_function_call_arguments_delta_maps_to_tool_call_delta():
    event = {
        "type": "response.function_call_arguments.delta",
        "delta": '{"x"',
        "response": {"id": "r", "model": "m"},
    }
    chunk = _adapter().from_provider_stream_chunk(event)
    assert chunk is not None
    assert chunk.choices[0].delta["tool_calls"][0]["function"]["arguments"] == '{"x"'


def test_stream_none_passthrough():
    assert _adapter().from_provider_stream_chunk(None) is None


def test_stream_unknown_event_returns_none():
    assert (
        _adapter().from_provider_stream_chunk({"type": "response.created"}) is None
    )


# ---------------------------------------------------------------------------
# Account-id header injection (no credential fetching in the adapter)
# ---------------------------------------------------------------------------
def test_account_id_headers_present_and_absent():
    adapter = _adapter()
    assert adapter.account_id_headers("acct-123") == {
        "chatgpt-account-id": "acct-123"
    }
    # No reference -> empty mapping (callers can merge unconditionally).
    assert adapter.account_id_headers(None) == {}
    assert adapter.account_id_headers("") == {}


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------
def test_codex_adapter_resolves_through_registry():
    # Importing the translation package registers the Codex factory; the registry
    # resolves it lazily by AdapterKind.
    adapter = registry.get_adapter(AdapterKind.CODEX)
    assert isinstance(adapter, CodexAdapterDirect)
