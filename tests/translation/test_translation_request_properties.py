"""Property-based tests for request translation validity and preservation.

These tests validate Property 14 from the Gozar design: for any OpenAI chat
request, translating it to a provider request via each ``ProviderAdapter``
produces a *valid* provider request that *preserves* the essential content
(messages, model intent, tool definitions) per the adapter's mapping rules.

Three adapters are exercised:

* ``OpenAICompatAdapter`` -- identity/pass-through, so the request must survive
  unchanged.
* ``CodexAdapter`` -- OpenAI Chat -> Codex Responses API (system hoisted to
  ``instructions``, non-system messages -> ordered ``input`` items, tools
  flattened).
* ``AnthropicAdapter`` -- OpenAI Chat -> Anthropic Messages API (system hoisted
  to the top-level ``system`` field, non-system messages -> ``messages`` blocks,
  ``max_tokens`` always present, tools -> ``input_schema`` definitions).
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from gozar.translation import (
    AnthropicAdapter,
    CodexAdapter,
    OpenAICompatAdapter,
)
from gozar.translation.types import OpenAIChatRequest

# Textual content / identifiers drawn from an alphanumeric alphabet so generated
# values are always non-empty and survive translation verbatim (adapters drop
# empty/falsy content, which would make preservation assertions ambiguous).
_token_text = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    min_size=1,
    max_size=24,
)

# Model identifiers: non-empty, allowing the punctuation real model names use.
_model_names = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.",
    min_size=1,
    max_size=30,
)

_SYSTEM_ROLES = ("system", "developer")


@st.composite
def _message(draw: Any) -> dict[str, Any]:
    """Draw a single OpenAI chat message with non-empty string content.

    Covers the four inbound role shapes the adapters branch on: system/developer
    (hoisted), user, assistant (plain text, no tool calls here), and tool results
    (which carry a ``tool_call_id``).
    """
    role = draw(st.sampled_from(["system", "developer", "user", "assistant", "tool"]))
    if role == "tool":
        return {
            "role": "tool",
            "tool_call_id": draw(_token_text),
            "content": draw(_token_text),
        }
    return {"role": role, "content": draw(_token_text)}


@st.composite
def _chat_request(draw: Any, *, with_tools: bool = False) -> OpenAIChatRequest:
    """Draw a valid OpenAI chat request.

    The message list mixes system/non-system roles in arbitrary order. ``max_tokens``
    is sometimes omitted (so the Anthropic default-fill path is exercised). When
    ``with_tools`` is set, a non-empty list of uniquely named function tools is added.
    """
    kwargs: dict[str, Any] = {
        "model": draw(_model_names),
        "messages": draw(st.lists(_message(), min_size=1, max_size=8)),
    }
    max_tokens = draw(st.one_of(st.none(), st.integers(min_value=1, max_value=4096)))
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens

    if with_tools:
        names = draw(st.lists(_token_text, min_size=1, max_size=4, unique=True))
        kwargs["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": draw(_token_text),
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in names
        ]
    return OpenAIChatRequest.model_validate(kwargs)


def _split(req: OpenAIChatRequest) -> tuple[list[Any], list[Any]]:
    """Partition messages into (system/developer, everything else), order preserved."""
    system = [m for m in req.messages if m.role in _SYSTEM_ROLES]
    other = [m for m in req.messages if m.role not in _SYSTEM_ROLES]
    return system, other


# Feature: gozar, Property 14: For any OpenAI chat request, translating it to a
# provider request via each ProviderAdapter produces a valid provider request that
# preserves the essential content (messages, model intent, tool definitions) per
# the adapter's mapping rules.
@hyp_settings(max_examples=150)
@given(req=_chat_request())
def test_translation_preserves_model_intent(req: OpenAIChatRequest) -> None:
    """Validates: Requirements 7.1, 7.2.

    The model intent is carried through every adapter: the pass-through adapter
    keeps the whole request (model included), and the Codex and Anthropic adapters
    emit the model verbatim on the provider body.
    """
    passthrough = OpenAICompatAdapter().to_provider_request(req)
    assert passthrough.model == req.model

    assert CodexAdapter().to_provider_request(req)["model"] == req.model
    assert AnthropicAdapter().to_provider_request(req)["model"] == req.model


# Feature: gozar, Property 14: For any OpenAI chat request, translating it to a
# provider request via each ProviderAdapter produces a valid provider request that
# preserves the essential content (messages, model intent, tool definitions) per
# the adapter's mapping rules.
@hyp_settings(max_examples=150)
@given(req=_chat_request())
def test_translation_produces_valid_provider_request(req: OpenAIChatRequest) -> None:
    """Validates: Requirements 7.1, 7.2.

    Each adapter yields a well-formed provider request: the pass-through adapter a
    valid OpenAI request, Codex a Responses body (``model``/``input``/``stream`` with
    typed input items), and Anthropic a Messages body with a positive ``max_tokens``
    and only user/assistant content-block messages.
    """
    passthrough = OpenAICompatAdapter().to_provider_request(req)
    assert isinstance(passthrough, OpenAIChatRequest)

    codex = CodexAdapter().to_provider_request(req)
    assert isinstance(codex, dict)
    assert isinstance(codex["model"], str)
    assert isinstance(codex["input"], list)
    assert isinstance(codex["stream"], bool)
    for item in codex["input"]:
        assert isinstance(item, dict)
        assert "type" in item

    anthropic = AnthropicAdapter().to_provider_request(req)
    assert isinstance(anthropic, dict)
    assert isinstance(anthropic["model"], str)
    # Anthropic *requires* max_tokens; it must always be present and positive.
    assert isinstance(anthropic["max_tokens"], int)
    assert anthropic["max_tokens"] >= 1
    assert isinstance(anthropic["messages"], list)
    for message in anthropic["messages"]:
        assert message["role"] in ("user", "assistant")
        assert isinstance(message["content"], list)


# Feature: gozar, Property 14: For any OpenAI chat request, translating it to a
# provider request via each ProviderAdapter produces a valid provider request that
# preserves the essential content (messages, model intent, tool definitions) per
# the adapter's mapping rules.
@hyp_settings(max_examples=150)
@given(req=_chat_request())
def test_passthrough_translation_is_identity(req: OpenAIChatRequest) -> None:
    """Validates: Requirements 7.1, 7.2.

    For an OpenAI-compatible Provider no body translation occurs: the request is
    returned unchanged, so all messages and the model are preserved exactly.
    """
    result = OpenAICompatAdapter().to_provider_request(req)
    assert result is req
    assert result.messages == req.messages
    assert result.model == req.model


# Feature: gozar, Property 14: For any OpenAI chat request, translating it to a
# provider request via each ProviderAdapter produces a valid provider request that
# preserves the essential content (messages, model intent, tool definitions) per
# the adapter's mapping rules.
@hyp_settings(max_examples=150)
@given(req=_chat_request())
def test_codex_translation_preserves_messages(req: OpenAIChatRequest) -> None:
    """Validates: Requirements 7.1, 7.2.

    The Codex adapter hoists every system/developer message into ``instructions``
    and maps each remaining message to one ``input`` item, in order, preserving its
    text (and the tool-call-id linkage for tool results).
    """
    system_msgs, other_msgs = _split(req)
    codex = CodexAdapter().to_provider_request(req)

    instructions = codex.get("instructions", "")
    for msg in system_msgs:
        assert msg.content in instructions

    items = codex["input"]
    assert len(items) == len(other_msgs)
    for src, item in zip(other_msgs, items):
        if src.role == "tool":
            assert item["type"] == "function_call_output"
            assert item["call_id"] == src.tool_call_id
            assert item["output"] == src.content
        else:
            assert item["type"] == "message"
            assert item["role"] == src.role
            texts = [part["text"] for part in item["content"]]
            assert src.content in texts


# Feature: gozar, Property 14: For any OpenAI chat request, translating it to a
# provider request via each ProviderAdapter produces a valid provider request that
# preserves the essential content (messages, model intent, tool definitions) per
# the adapter's mapping rules.
@hyp_settings(max_examples=150)
@given(req=_chat_request())
def test_anthropic_translation_preserves_messages(req: OpenAIChatRequest) -> None:
    """Validates: Requirements 7.1, 7.2.

    The Anthropic adapter hoists system/developer messages into the top-level
    ``system`` field and maps each remaining message to one ``messages`` entry, in
    order, preserving its text as content blocks (tool results become a
    ``tool_result`` block on a user turn).
    """
    system_msgs, other_msgs = _split(req)
    anthropic = AnthropicAdapter().to_provider_request(req)

    system_field = anthropic.get("system", "")
    for msg in system_msgs:
        assert msg.content in system_field

    messages = anthropic["messages"]
    assert len(messages) == len(other_msgs)
    for src, message in zip(other_msgs, messages):
        if src.role == "tool":
            assert message["role"] == "user"
            block = message["content"][0]
            assert block["type"] == "tool_result"
            assert block["tool_use_id"] == src.tool_call_id
            assert block["content"] == src.content
        else:
            expected_role = "assistant" if src.role == "assistant" else "user"
            assert message["role"] == expected_role
            assert {"type": "text", "text": src.content} in message["content"]


# Feature: gozar, Property 14: For any OpenAI chat request, translating it to a
# provider request via each ProviderAdapter produces a valid provider request that
# preserves the essential content (messages, model intent, tool definitions) per
# the adapter's mapping rules.
@hyp_settings(max_examples=150)
@given(req=_chat_request(with_tools=True))
def test_translation_preserves_tool_definitions(req: OpenAIChatRequest) -> None:
    """Validates: Requirements 7.1, 7.2.

    Tool definitions survive translation through every adapter: the pass-through
    keeps them verbatim, Codex flattens them into the Responses tool shape (name,
    description, parameters preserved), and Anthropic maps ``parameters`` to
    ``input_schema`` while preserving names, all in order.
    """
    names = [tool["function"]["name"] for tool in req.tools]

    passthrough = OpenAICompatAdapter().to_provider_request(req)
    assert passthrough.tools == req.tools

    codex_tools = CodexAdapter().to_provider_request(req)["tools"]
    assert [tool["name"] for tool in codex_tools] == names
    for src, mapped in zip(req.tools, codex_tools):
        fn = src["function"]
        assert mapped["name"] == fn["name"]
        assert mapped["description"] == fn["description"]
        assert mapped["parameters"] == fn["parameters"]

    anthropic_tools = AnthropicAdapter().to_provider_request(req)["tools"]
    assert [tool["name"] for tool in anthropic_tools] == names
    for src, mapped in zip(req.tools, anthropic_tools):
        fn = src["function"]
        assert mapped["name"] == fn["name"]
        assert mapped["input_schema"] == fn["parameters"]
