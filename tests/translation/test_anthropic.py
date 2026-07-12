"""Unit tests for the Anthropic Messages API adapter.

Focused examples covering the behaviours called out in task 7.3: system-prompt
hoisting, role/content-block mapping, ``max_tokens`` handling, stop-reason mapping,
streaming event mapping, and usage mapping. Property-based coverage of translation
validity lives in tasks 7.4 / 7.5.
"""

from __future__ import annotations

import json

from gozar.translation import AnthropicAdapter
from gozar.translation.anthropic import DEFAULT_MAX_TOKENS
from gozar.translation.types import (
    OpenAIChatRequest,
    OpenAIChatResponse,
    OpenAIStreamChunk,
)


def _adapter() -> AnthropicAdapter:
    return AnthropicAdapter()


# --- system-prompt hoisting -------------------------------------------------
def test_system_message_is_hoisted_to_top_level_system_field():
    req = OpenAIChatRequest(
        model="claude-3-5-sonnet",
        messages=[
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
        ],
        max_tokens=64,
    )
    body = _adapter().to_provider_request(req)

    assert body["system"] == "You are helpful."
    # System message must NOT appear in the Anthropic messages array.
    assert [m["role"] for m in body["messages"]] == ["user"]


def test_multiple_system_and_developer_messages_are_joined():
    req = OpenAIChatRequest(
        model="claude-3-5-sonnet",
        messages=[
            {"role": "system", "content": "Rule one."},
            {"role": "developer", "content": "Rule two."},
            {"role": "user", "content": "Hi"},
        ],
        max_tokens=64,
    )
    body = _adapter().to_provider_request(req)
    assert body["system"] == "Rule one.\n\nRule two."


def test_no_system_message_omits_system_field():
    req = OpenAIChatRequest(
        model="claude-3-5-sonnet",
        messages=[{"role": "user", "content": "Hi"}],
        max_tokens=64,
    )
    body = _adapter().to_provider_request(req)
    assert "system" not in body


# --- role / content mapping -------------------------------------------------
def test_user_and_assistant_roles_map_to_content_blocks():
    req = OpenAIChatRequest(
        model="claude-3-5-sonnet",
        messages=[
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ],
        max_tokens=64,
    )
    body = _adapter().to_provider_request(req)

    assert body["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Hi there"}]},
    ]


def test_assistant_tool_calls_map_to_tool_use_blocks():
    req = OpenAIChatRequest(
        model="claude-3-5-sonnet",
        messages=[
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
                            "arguments": '{"city": "Paris"}',
                        },
                    }
                ],
            },
        ],
        max_tokens=64,
    )
    body = _adapter().to_provider_request(req)
    assistant = body["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == [
        {
            "type": "tool_use",
            "id": "call_1",
            "name": "get_weather",
            "input": {"city": "Paris"},
        }
    ]


def test_tool_result_message_maps_to_user_tool_result_block():
    req = OpenAIChatRequest(
        model="claude-3-5-sonnet",
        messages=[
            {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
        ],
        max_tokens=64,
    )
    body = _adapter().to_provider_request(req)
    assert body["messages"][0] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": "sunny"}
        ],
    }


def test_array_content_parts_map_to_blocks():
    req = OpenAIChatRequest(
        model="claude-3-5-sonnet",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
                ],
            }
        ],
        max_tokens=64,
    )
    body = _adapter().to_provider_request(req)
    assert body["messages"][0]["content"] == [
        {"type": "text", "text": "look"},
        {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}},
    ]


# --- max_tokens handling ----------------------------------------------------
def test_max_tokens_is_taken_from_request_when_present():
    req = OpenAIChatRequest(
        model="claude-3-5-sonnet",
        messages=[{"role": "user", "content": "Hi"}],
        max_tokens=123,
    )
    body = _adapter().to_provider_request(req)
    assert body["max_tokens"] == 123


def test_max_tokens_falls_back_to_default_when_absent():
    req = OpenAIChatRequest(
        model="claude-3-5-sonnet",
        messages=[{"role": "user", "content": "Hi"}],
    )
    body = _adapter().to_provider_request(req)
    assert body["max_tokens"] == DEFAULT_MAX_TOKENS


def test_max_tokens_default_is_configurable():
    adapter = AnthropicAdapter(default_max_tokens=999)
    req = OpenAIChatRequest(
        model="claude-3-5-sonnet",
        messages=[{"role": "user", "content": "Hi"}],
    )
    body = adapter.to_provider_request(req)
    assert body["max_tokens"] == 999


# --- tools mapping ----------------------------------------------------------
def test_tools_and_tool_choice_are_mapped():
    req = OpenAIChatRequest(
        model="claude-3-5-sonnet",
        messages=[{"role": "user", "content": "Hi"}],
        max_tokens=64,
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        tool_choice="required",
    )
    body = _adapter().to_provider_request(req)
    assert body["tools"] == [
        {
            "name": "get_weather",
            "description": "Get weather",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]
    assert body["tool_choice"] == {"type": "any"}


# --- response mapping -------------------------------------------------------
def test_response_text_blocks_map_to_message_content():
    resp = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-3-5-sonnet",
        "content": [
            {"type": "text", "text": "Hello "},
            {"type": "text", "text": "world"},
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    out = _adapter().from_provider_response(resp)

    assert isinstance(out, OpenAIChatResponse)
    assert out.model == "claude-3-5-sonnet"
    assert out.choices[0].message.content == "Hello world"
    assert out.choices[0].finish_reason == "stop"
    assert out.usage is not None
    assert out.usage.prompt_tokens == 10
    assert out.usage.completion_tokens == 5
    assert out.usage.total_tokens == 15


def test_response_tool_use_block_maps_to_tool_calls():
    resp = {
        "id": "msg_2",
        "role": "assistant",
        "model": "claude-3-5-sonnet",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "get_weather",
                "input": {"city": "Paris"},
            }
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 8, "output_tokens": 12},
    }
    out = _adapter().from_provider_response(resp)
    msg = out.choices[0].message
    assert out.choices[0].finish_reason == "tool_calls"
    assert msg.tool_calls is not None
    call = msg.tool_calls[0]
    assert call["id"] == "toolu_1"
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "Paris"}


# --- stop reason mapping ----------------------------------------------------
def test_stop_reason_mapping_table():
    adapter = _adapter()
    assert adapter._map_stop_reason("end_turn") == "stop"
    assert adapter._map_stop_reason("stop_sequence") == "stop"
    assert adapter._map_stop_reason("max_tokens") == "length"
    assert adapter._map_stop_reason("tool_use") == "tool_calls"
    assert adapter._map_stop_reason(None) is None
    # Unknown reasons degrade to "stop" rather than crashing.
    assert adapter._map_stop_reason("something_new") == "stop"


# --- usage mapping ----------------------------------------------------------
def test_extract_usage_maps_input_output_tokens():
    usage = _adapter().extract_usage(
        {"usage": {"input_tokens": 30, "output_tokens": 7}}
    )
    assert usage.prompt_tokens == 30
    assert usage.completion_tokens == 7
    assert usage.total_tokens == 37


def test_extract_usage_absent_returns_zeros():
    usage = _adapter().extract_usage({"id": "msg", "content": []})
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0
    assert usage.total_tokens == 0


# --- streaming mapping ------------------------------------------------------
def test_stream_message_start_emits_role_delta():
    chunk = _adapter().from_provider_stream_chunk(
        {"type": "message_start", "message": {"usage": {"input_tokens": 4}}}
    )
    assert isinstance(chunk, OpenAIStreamChunk)
    assert chunk.choices[0].delta == {"role": "assistant", "content": ""}


def test_stream_text_delta_emits_content():
    chunk = _adapter().from_provider_stream_chunk(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Hello"},
        }
    )
    assert chunk is not None
    assert chunk.choices[0].delta == {"content": "Hello"}


def test_stream_message_delta_emits_finish_reason_and_usage():
    chunk = _adapter().from_provider_stream_chunk(
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 9},
        }
    )
    assert chunk is not None
    assert chunk.choices[0].finish_reason == "stop"
    assert chunk.usage is not None
    assert chunk.usage.completion_tokens == 9


def test_stream_non_content_events_return_none():
    adapter = _adapter()
    assert adapter.from_provider_stream_chunk({"type": "ping"}) is None
    assert adapter.from_provider_stream_chunk({"type": "content_block_stop"}) is None
    assert adapter.from_provider_stream_chunk({"type": "message_stop"}) is None
    assert adapter.from_provider_stream_chunk(None) is None


# --- registry coexistence ---------------------------------------------------
def test_anthropic_adapter_is_registered_in_registry():
    from gozar.providers import registry
    from gozar.providers.registry import AdapterKind

    adapter = registry.get_adapter(AdapterKind.ANTHROPIC)
    assert isinstance(adapter, AnthropicAdapter)
