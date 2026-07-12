"""Anthropic Messages API adapter.

Translates between the canonical OpenAI Chat Completions contract Gozar speaks to
its Client_Applications and the native Anthropic Messages API shape (Requirement
7.2, 7.3). The adapter is pure and side-effect-free: it converts request and
response *bodies* only. Header and authentication substitution (the Anthropic
``x-api-key`` / OAuth bearer and the required ``anthropic-version`` /
``anthropic-beta`` headers) is the responsibility of the upstream provider client,
never of this adapter.

Key differences between the two dialects that this adapter reconciles:

* **System prompt hoisting.** OpenAI carries the system instruction as a message
  with ``role == "system"`` (or the newer ``"developer"`` role) inside the
  ``messages`` array. Anthropic instead takes the system instruction as a
  top-level ``system`` field and only allows ``user`` / ``assistant`` roles inside
  ``messages``. The adapter hoists every system/developer message out of the array
  and joins them into the ``system`` field.
* **Required ``max_tokens``.** Anthropic *requires* ``max_tokens`` on every
  request; OpenAI treats it as optional. The adapter uses the request's
  ``max_tokens`` when present and otherwise falls back to a documented, overridable
  default (:data:`DEFAULT_MAX_TOKENS`, injectable via the constructor so a
  deployment can drive it from configuration). It never silently emits a request
  without ``max_tokens``.
* **Content blocks.** OpenAI message content is either a plain string or an array
  of typed parts; Anthropic uses an array of typed content blocks. Assistant
  ``tool_calls`` map to Anthropic ``tool_use`` blocks, and OpenAI ``role == "tool"``
  result messages map to Anthropic ``tool_result`` blocks carried on a user turn.
* **Stop reason.** Anthropic's ``stop_reason`` enum maps onto OpenAI's
  ``finish_reason`` enum.
* **Usage.** Anthropic reports ``input_tokens`` / ``output_tokens``; OpenAI reports
  ``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``.

This module backs design Properties 14 and 15 (request/response translation
validity); the property-based tests themselves are tasks 7.4 / 7.5.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from gozar.providers.registry import AdapterKind, register_adapter

from .types import (
    OpenAIChatMessage,
    OpenAIChatRequest,
    OpenAIChatResponse,
    OpenAIResponseChoice,
    OpenAIStreamChunk,
    ProviderChunk,
    ProviderRequest,
    ProviderResponse,
    UsageCounts,
)

#: Documented fallback for Anthropic's required ``max_tokens`` field, used only
#: when the inbound OpenAI request does not specify ``max_tokens``. It is injected
#: through :class:`AnthropicAdapter`'s constructor so a deployment can override it
#: from configuration; this constant is merely the safe default that keeps the
#: adapter from ever producing an Anthropic request that omits ``max_tokens``.
DEFAULT_MAX_TOKENS = 4096

#: Roles that OpenAI carries inside ``messages`` but Anthropic expects as the
#: top-level ``system`` field. The ``developer`` role is the newer OpenAI alias for
#: the system instruction and is hoisted identically.
_SYSTEM_ROLES = frozenset({"system", "developer"})

#: Anthropic ``stop_reason`` -> OpenAI ``finish_reason``.
_STOP_REASON_MAP: dict[str, str] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
}


def _as_mapping(value: Any) -> dict[str, Any]:
    """Return ``value`` as a plain dict.

    Accepts a Pydantic model (dumped with field aliases) or a mapping, so the
    adapter works whether the provider client hands it a parsed model or the raw
    JSON body decoded from the wire.
    """
    if hasattr(value, "model_dump"):
        return value.model_dump(by_alias=True, exclude_none=True)
    if isinstance(value, dict):
        return dict(value)
    raise TypeError(f"expected a mapping or pydantic model, got {type(value)!r}")


def _content_to_text(content: Any) -> str:
    """Flatten OpenAI message content into a plain string.

    System/developer content may be a string or an array of text parts; Anthropic's
    ``system`` field is plain text, so text parts are concatenated.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
        return "\n".join(parts)
    return str(content)


def _content_to_blocks(content: Any) -> list[dict[str, Any]]:
    """Map OpenAI user/assistant content into Anthropic content blocks.

    A plain string becomes a single ``text`` block. An array of OpenAI content
    parts maps part-by-part: ``text`` parts become Anthropic ``text`` blocks and
    ``image_url`` parts become Anthropic ``image`` blocks (URL source). Unknown
    part shapes are passed through best-effort so no content is silently dropped.
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        blocks: list[dict[str, Any]] = []
        for part in content:
            if isinstance(part, str):
                if part:
                    blocks.append({"type": "text", "text": part})
                continue
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type == "text":
                blocks.append({"type": "text", "text": str(part.get("text", ""))})
            elif part_type == "image_url":
                url = part.get("image_url", {})
                url_value = url.get("url") if isinstance(url, dict) else url
                blocks.append(
                    {
                        "type": "image",
                        "source": {"type": "url", "url": str(url_value)},
                    }
                )
            else:
                blocks.append(part)
        return blocks
    return [{"type": "text", "text": str(content)}]


def _parse_tool_arguments(arguments: Any) -> Any:
    """Decode an OpenAI tool-call ``arguments`` string into a JSON object.

    OpenAI serializes function-call arguments as a JSON string; Anthropic expects a
    structured ``input`` object. Falls back to wrapping the raw string when it is
    not valid JSON so malformed arguments never crash translation.
    """
    if isinstance(arguments, (dict, list)):
        return arguments
    if isinstance(arguments, str):
        if not arguments:
            return {}
        try:
            return json.loads(arguments)
        except json.JSONDecodeError:
            return {"_raw_arguments": arguments}
    return {}


def _assistant_tool_calls_to_blocks(
    tool_calls: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Map OpenAI assistant ``tool_calls`` into Anthropic ``tool_use`` blocks."""
    blocks: list[dict[str, Any]] = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function", {}) or {}
        blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id", ""),
                "name": function.get("name", ""),
                "input": _parse_tool_arguments(function.get("arguments")),
            }
        )
    return blocks


def _map_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map OpenAI tool definitions into Anthropic tool definitions.

    OpenAI: ``{"type": "function", "function": {name, description, parameters}}``.
    Anthropic: ``{name, description, input_schema}``.
    """
    mapped: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") if tool.get("type") == "function" else tool
        function = function or {}
        entry: dict[str, Any] = {"name": function.get("name", "")}
        if function.get("description") is not None:
            entry["description"] = function["description"]
        entry["input_schema"] = function.get("parameters") or {
            "type": "object",
            "properties": {},
        }
        mapped.append(entry)
    return mapped


def _map_tool_choice(tool_choice: Any) -> dict[str, Any] | None:
    """Map an OpenAI ``tool_choice`` value into Anthropic's ``tool_choice`` shape."""
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        if tool_choice == "auto":
            return {"type": "auto"}
        if tool_choice == "required":
            return {"type": "any"}
        if tool_choice == "none":
            return None
        return {"type": "auto"}
    if isinstance(tool_choice, dict):
        if tool_choice.get("type") == "function":
            name = (tool_choice.get("function") or {}).get("name")
            if name:
                return {"type": "tool", "name": name}
        return {"type": "auto"}
    return None


class AnthropicAdapter:
    """:class:`~gozar.translation.types.ProviderAdapter` for the Anthropic Messages API.

    Pure, side-effect-free translation of request and response bodies between the
    OpenAI Chat Completions contract and the Anthropic Messages API.
    """

    def __init__(self, default_max_tokens: int = DEFAULT_MAX_TOKENS) -> None:
        """Create the adapter.

        :param default_max_tokens: Fallback for Anthropic's required ``max_tokens``
            used only when the inbound request omits it. Overridable so a deployment
            can drive it from configuration; defaults to :data:`DEFAULT_MAX_TOKENS`.
        """
        self._default_max_tokens = default_max_tokens

    # -- request -------------------------------------------------------------
    def to_provider_request(self, req: OpenAIChatRequest) -> ProviderRequest:
        """Convert an OpenAI Chat Completions request into an Anthropic request.

        Hoists system/developer messages into the top-level ``system`` field, maps
        user/assistant/tool messages into Anthropic content blocks, guarantees
        ``max_tokens`` is set, and maps tools and ``tool_choice`` when present.
        """
        system_texts: list[str] = []
        messages: list[dict[str, Any]] = []

        for message in req.messages:
            role = message.role
            if role in _SYSTEM_ROLES:
                text = _content_to_text(message.content)
                if text:
                    system_texts.append(text)
                continue

            if role == "tool":
                # An OpenAI tool result becomes an Anthropic user turn carrying a
                # tool_result block referencing the originating tool call.
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message.tool_call_id or "",
                                "content": _content_to_text(message.content),
                            }
                        ],
                    }
                )
                continue

            blocks = _content_to_blocks(message.content)
            if role == "assistant" and message.tool_calls:
                blocks.extend(_assistant_tool_calls_to_blocks(message.tool_calls))

            anthropic_role = "assistant" if role == "assistant" else "user"
            messages.append({"role": anthropic_role, "content": blocks})

        body: dict[str, Any] = {
            "model": req.model,
            "messages": messages,
            "max_tokens": req.max_tokens
            if req.max_tokens is not None
            else self._default_max_tokens,
        }

        if system_texts:
            body["system"] = "\n\n".join(system_texts)
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.stream:
            body["stream"] = True
        if req.tools:
            body["tools"] = _map_tools(req.tools)
            choice = _map_tool_choice(req.tool_choice)
            if choice is not None:
                body["tool_choice"] = choice

        return body

    # -- non-streaming response ---------------------------------------------
    def from_provider_response(self, resp: ProviderResponse) -> OpenAIChatResponse:
        """Convert an Anthropic Messages response into an OpenAI Chat response.

        Concatenates ``text`` content blocks into the assistant message content,
        rebuilds ``tool_calls`` from ``tool_use`` blocks, maps ``stop_reason`` to
        ``finish_reason``, and maps usage.
        """
        data = _as_mapping(resp)
        content_blocks = data.get("content") or []

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(str(block.get("text", "")))
            elif block_type == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    }
                )

        message_kwargs: dict[str, Any] = {
            "role": data.get("role", "assistant"),
            "content": "".join(text_parts) if text_parts else None,
        }
        if tool_calls:
            message_kwargs["tool_calls"] = tool_calls

        finish_reason = self._map_stop_reason(data.get("stop_reason"))

        return OpenAIChatResponse(
            id=data.get("id", f"chatcmpl-{uuid.uuid4().hex}"),
            object="chat.completion",
            created=int(time.time()),
            model=data.get("model", ""),
            choices=[
                OpenAIResponseChoice(
                    index=0,
                    message=OpenAIChatMessage(**message_kwargs),
                    finish_reason=finish_reason,
                )
            ],
            usage=self.extract_usage(data),
        )

    # -- streaming -----------------------------------------------------------
    def from_provider_stream_chunk(
        self, chunk: ProviderChunk
    ) -> OpenAIStreamChunk | None:
        """Convert one Anthropic SSE event into an OpenAI stream chunk.

        Anthropic emits a sequence of typed events; only those that carry
        client-facing content map to an OpenAI chunk. Events with no client-facing
        payload (``content_block_start``, ``content_block_stop``, ``ping``,
        ``message_stop``) return ``None``.
        """
        if chunk is None:
            return None
        event = _as_mapping(chunk)
        event_type = event.get("type")

        stream_id = event.get("_gozar_stream_id", "")
        model = event.get("_gozar_model", "")
        created = int(time.time())

        def _chunk(delta: dict[str, Any], finish_reason: str | None,
                   usage: UsageCounts | None = None) -> OpenAIStreamChunk:
            return OpenAIStreamChunk(
                id=stream_id,
                object="chat.completion.chunk",
                created=created,
                model=model,
                choices=[{"index": 0, "delta": delta, "finish_reason": finish_reason}],
                usage=usage,
            )

        if event_type == "message_start":
            return _chunk({"role": "assistant", "content": ""}, None)

        if event_type == "content_block_delta":
            delta = event.get("delta") or {}
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                return _chunk({"content": delta.get("text", "")}, None)
            if delta_type == "input_json_delta":
                # Partial tool-call arguments.
                index = event.get("index", 0)
                return _chunk(
                    {
                        "tool_calls": [
                            {
                                "index": index,
                                "function": {
                                    "arguments": delta.get("partial_json", "")
                                },
                            }
                        ]
                    },
                    None,
                )
            return None

        if event_type == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                index = event.get("index", 0)
                return _chunk(
                    {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": block.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": block.get("name", ""),
                                    "arguments": "",
                                },
                            }
                        ]
                    },
                    None,
                )
            return None

        if event_type == "message_delta":
            delta = event.get("delta") or {}
            finish_reason = self._map_stop_reason(delta.get("stop_reason"))
            usage = None
            raw_usage = event.get("usage")
            if raw_usage is not None:
                usage = self._usage_from_counts(raw_usage)
            if finish_reason is None and usage is None:
                return None
            return _chunk({}, finish_reason, usage)

        return None

    # -- usage ---------------------------------------------------------------
    def extract_usage(self, resp: ProviderResponse) -> UsageCounts:
        """Map Anthropic ``usage`` (``input_tokens`` / ``output_tokens``) to counts."""
        data = _as_mapping(resp)
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return UsageCounts()
        return self._usage_from_counts(usage)

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _usage_from_counts(usage: dict[str, Any]) -> UsageCounts:
        """Build :class:`UsageCounts` from an Anthropic usage mapping."""
        prompt = int(usage.get("input_tokens", 0) or 0)
        completion = int(usage.get("output_tokens", 0) or 0)
        return UsageCounts(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
        )

    @staticmethod
    def _map_stop_reason(stop_reason: Any) -> str | None:
        """Map an Anthropic ``stop_reason`` to an OpenAI ``finish_reason``."""
        if stop_reason is None:
            return None
        return _STOP_REASON_MAP.get(str(stop_reason), "stop")


# Register this adapter with the provider registry so the Codex registration in a
# sibling module is never disturbed. The factory is invoked lazily by the registry.
register_adapter(AdapterKind.ANTHROPIC, lambda: AnthropicAdapter())


__all__ = ["AnthropicAdapter", "DEFAULT_MAX_TOKENS"]
