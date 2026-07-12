"""Codex adapter: OpenAI Chat Completions <-> Codex Responses API.

ChatGPT subscription access routes to OpenAI's Codex backend
(``https://chatgpt.com/backend-api/codex``), which is a **Responses-API-shaped**
endpoint rather than a Chat Completions endpoint. This adapter performs the pure,
side-effect-free body translation between the canonical OpenAI Chat Completions
dialect that every Client_Application speaks and the Responses API shape the
Codex backend expects and returns (Requirement 7.2, 7.3).

What this adapter does
----------------------
* **Request** (:meth:`CodexAdapter.to_provider_request`): maps the OpenAI
  ``messages`` array into Responses ``input`` items, hoists ``system`` /
  ``developer`` messages into the Responses ``instructions`` field, maps
  assistant ``tool_calls`` into ``function_call`` items and ``tool`` results into
  ``function_call_output`` items, flattens OpenAI tool definitions into the
  Responses tool shape, and carries generation parameters (``temperature``,
  ``max_tokens`` -> ``max_output_tokens``, plus any extra fields).
* **Response** (:meth:`CodexAdapter.from_provider_response`): collapses the
  Responses ``output`` array back into a single OpenAI assistant message,
  **dropping reasoning items** so the client only ever sees the strict OpenAI
  shape, and reconstructs ``tool_calls`` from ``function_call`` output items.
* **Stream** (:meth:`CodexAdapter.from_provider_stream_chunk`): maps Responses
  streaming events to OpenAI SSE delta chunks; **reasoning events return
  ``None``** (dropped from client-facing output).
* **Usage** (:meth:`CodexAdapter.extract_usage`): maps Responses
  ``input_tokens`` / ``output_tokens`` / ``total_tokens`` to
  :class:`UsageCounts`, with zeros when usage is absent (Requirement 13.2).

What this adapter does *not* do
-------------------------------
It performs **no I/O** and fetches **no credentials**. The Codex backend requires
an account-identifier header in addition to the bearer token; this adapter only
*names* that header and *formats* its value via :meth:`CodexAdapter.account_id_headers`.
The actual account reference (and the bearer token) are supplied by the upstream
provider client from the selected Upstream_Credential -- credential fetching stays
out of the pure translation layer.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from gozar.providers.registry import AdapterKind, register_adapter

from .types import (
    OpenAIChatRequest,
    OpenAIChatResponse,
    OpenAIStreamChunk,
    ProviderChunk,
    ProviderRequest,
    ProviderResponse,
    UsageCounts,
)


def _as_dict(value: Any) -> dict[str, Any]:
    """Best-effort coercion of an upstream payload into a plain mapping.

    Accepts a raw ``dict`` (the common case for decoded upstream JSON) or any
    Pydantic model, returning a plain dict either way so the mapping logic does
    not care which form the provider client handed in.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump()
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return dict(value)


def _text_from_content(content: Any) -> str:
    """Flatten OpenAI message content into a plain string.

    Content may be ``None`` (tool-call-only assistant messages), a plain string,
    or an array of content parts (``{"type": "text"|"input_text"|"output_text",
    "text": ...}``). Non-text parts (e.g. images) are not represented in the
    Responses text shape and are skipped; the first version handles text chat
    only (design Non-Goals).
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if text is not None:
                    parts.append(str(text))
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return str(content)


def _map_tool(tool: Any) -> Any:
    """Flatten an OpenAI tool definition into the Responses tool shape.

    OpenAI Chat tools nest the schema under a ``function`` key
    (``{"type": "function", "function": {"name", "description", "parameters"}}``).
    The Responses API flattens these onto the tool object itself. Unknown tool
    shapes are passed through unchanged so nothing is silently dropped.
    """
    if isinstance(tool, dict) and tool.get("type") == "function" and "function" in tool:
        fn = tool.get("function") or {}
        return {
            "type": "function",
            "name": fn.get("name"),
            "description": fn.get("description"),
            "parameters": fn.get("parameters"),
        }
    return tool


class CodexAdapter:
    """:class:`ProviderAdapter` for the Codex Responses-API backend.

    Pure translation between OpenAI Chat Completions and the Codex Responses API.
    All methods take a value and return a value; no network or disk access occurs
    here.
    """

    #: The account-identifier header the Codex backend requires alongside the
    #: bearer token. This is a fixed part of the Codex wire protocol (not a
    #: per-deployment value), so it lives here rather than in configuration.
    ACCOUNT_ID_HEADER = "chatgpt-account-id"

    # -- request -------------------------------------------------------------
    def to_provider_request(self, req: OpenAIChatRequest) -> ProviderRequest:
        """Convert an OpenAI Chat request into a Codex Responses request body.

        Returns a plain ``dict`` ready to be JSON-encoded by the provider client.
        """
        instructions: list[str] = []
        input_items: list[dict[str, Any]] = []

        for msg in req.messages:
            role = msg.role
            if role in ("system", "developer"):
                text = _text_from_content(msg.content)
                if text:
                    instructions.append(text)
                continue
            if role == "assistant":
                for call in msg.tool_calls or []:
                    if not isinstance(call, dict):
                        continue
                    fn = call.get("function") or {}
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": call.get("id"),
                            "name": fn.get("name"),
                            "arguments": fn.get("arguments", ""),
                        }
                    )
                text = _text_from_content(msg.content)
                if text:
                    input_items.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}],
                        }
                    )
                continue
            if role == "tool":
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": msg.tool_call_id,
                        "output": _text_from_content(msg.content),
                    }
                )
                continue
            # user (and any other inbound role) -> Responses input message
            input_items.append(
                {
                    "type": "message",
                    "role": role,
                    "content": [
                        {"type": "input_text", "text": _text_from_content(msg.content)}
                    ],
                }
            )

        provider_req: dict[str, Any] = {
            "model": req.model,
            "input": input_items,
            "stream": req.stream,
            # The Codex Responses backend rejects every request without this: a
            # ChatGPT-subscription session has no conversation-store concept, so
            # "store": true is never valid here and the backend requires the field
            # to be explicitly present and false (see the Codex adapter tests).
            "store": False,
        }
        if instructions:
            provider_req["instructions"] = "\n\n".join(instructions)
        if req.temperature is not None:
            provider_req["temperature"] = req.temperature
        if req.max_tokens is not None:
            provider_req["max_output_tokens"] = req.max_tokens
        if req.tools:
            provider_req["tools"] = [_map_tool(tool) for tool in req.tools]
        if req.tool_choice is not None:
            provider_req["tool_choice"] = req.tool_choice

        # Carry any extra OpenAI generation parameters (top_p, seed, ...) that the
        # Responses API also accepts, without clobbering the keys mapped above.
        for key, value in (req.model_extra or {}).items():
            provider_req.setdefault(key, value)

        return provider_req

    # -- non-streaming response ---------------------------------------------
    def from_provider_response(self, resp: ProviderResponse) -> OpenAIChatResponse:
        """Convert a Codex Responses result into a clean OpenAI Chat response.

        Reasoning output items are dropped so the client only ever sees the strict
        OpenAI shape.
        """
        data = _as_dict(resp)
        text_segments: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        for item in data.get("output") or []:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "reasoning":
                # Reasoning content is never surfaced to the client.
                continue
            if item_type == "message":
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") in (
                        "output_text",
                        "text",
                    ):
                        if part.get("text") is not None:
                            text_segments.append(str(part["text"]))
            elif item_type == "function_call":
                tool_calls.append(
                    {
                        "id": item.get("call_id") or item.get("id"),
                        "type": "function",
                        "function": {
                            "name": item.get("name"),
                            "arguments": item.get("arguments", ""),
                        },
                    }
                )

        content = "".join(text_segments)
        message: dict[str, Any] = {
            "role": "assistant",
            "content": content if content else None,
        }
        if tool_calls:
            message["tool_calls"] = tool_calls

        return OpenAIChatResponse.model_validate(
            {
                "id": data.get("id", ""),
                "object": "chat.completion",
                "created": int(data.get("created_at") or data.get("created") or 0),
                "model": data.get("model", ""),
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": self._finish_reason(data, bool(tool_calls)),
                    }
                ],
                "usage": self.extract_usage(data).model_dump(),
            }
        )

    @staticmethod
    def _finish_reason(data: dict[str, Any], has_tool_calls: bool) -> str:
        """Map a Responses status/incomplete reason to an OpenAI finish reason."""
        incomplete = data.get("incomplete_details")
        if isinstance(incomplete, dict) and incomplete.get("reason") in (
            "max_output_tokens",
            "max_tokens",
        ):
            return "length"
        if has_tool_calls:
            return "tool_calls"
        return "stop"

    # -- streaming -----------------------------------------------------------
    def from_provider_stream_chunk(
        self, chunk: ProviderChunk
    ) -> OpenAIStreamChunk | None:
        """Map a single Codex Responses streaming event to an OpenAI SSE chunk.

        Returns ``None`` for events that carry no client-facing content -- notably
        every reasoning event, which is dropped to keep the strict OpenAI shape.
        """
        if chunk is None:
            return None
        event = _as_dict(chunk)
        event_type = str(event.get("type", ""))

        # Drop all reasoning events (reasoning summaries/deltas).
        if "reasoning" in event_type:
            return None

        chunk_id, created, model = self._stream_meta(event)

        if event_type == "response.output_text.delta":
            return OpenAIStreamChunk.model_validate(
                {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": event.get("delta", "")},
                            "finish_reason": None,
                        }
                    ],
                }
            )

        if event_type == "response.output_item.added":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                return OpenAIStreamChunk.model_validate(
                    {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": item.get("call_id")
                                            or item.get("id"),
                                            "type": "function",
                                            "function": {
                                                "name": item.get("name"),
                                                "arguments": item.get("arguments", ""),
                                            },
                                        }
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                )
            return None

        if event_type == "response.function_call_arguments.delta":
            return OpenAIStreamChunk.model_validate(
                {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {
                                            "arguments": event.get("delta", "")
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                }
            )

        if event_type == "response.completed":
            usage = self.extract_usage(event.get("response") or event)
            return OpenAIStreamChunk.model_validate(
                {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {"index": 0, "delta": {}, "finish_reason": "stop"}
                    ],
                    "usage": usage.model_dump(),
                }
            )

        # Any other event (created, in_progress, keep-alives, etc.) -> no chunk.
        return None

    @staticmethod
    def _stream_meta(event: dict[str, Any]) -> tuple[str, int, str]:
        """Extract ``(id, created, model)`` for an SSE chunk from an event.

        Codex events often embed the full ``response`` object (on lifecycle events)
        and omit it on incremental deltas; this reads whichever is present and
        falls back to safe defaults so a chunk can always be constructed.
        """
        response = event.get("response")
        if isinstance(response, dict):
            return (
                str(response.get("id", "")),
                int(response.get("created_at") or response.get("created") or 0),
                str(response.get("model", "")),
            )
        return (
            str(event.get("id", "")),
            int(event.get("created_at") or event.get("created") or 0),
            str(event.get("model", "")),
        )

    # -- usage ---------------------------------------------------------------
    def extract_usage(self, resp: ProviderResponse) -> UsageCounts:
        """Map Codex Responses usage to :class:`UsageCounts`, zeros when absent."""
        data = _as_dict(resp)
        usage = data.get("usage")
        if usage is None and isinstance(data.get("response"), dict):
            usage = data["response"].get("usage")
        if not isinstance(usage, dict):
            return UsageCounts()

        prompt = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        completion = int(
            usage.get("output_tokens") or usage.get("completion_tokens") or 0
        )
        total_raw = usage.get("total_tokens")
        total = int(total_raw) if total_raw is not None else prompt + completion
        return UsageCounts(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
        )

    # -- header helper (no credential fetching) ------------------------------
    def account_id_headers(self, account_ref: str | None) -> dict[str, str]:
        """Produce the Codex account-id header from a provider account reference.

        The gateway/provider client supplies ``account_ref`` from the selected
        Upstream_Credential; this method only formats the header. Returns an empty
        mapping when no reference is available so callers can merge it
        unconditionally.
        """
        if not account_ref:
            return {}
        return {self.ACCOUNT_ID_HEADER: str(account_ref)}


# Register this adapter with the provider registry so importing the translation
# package (which imports this module) makes the Codex adapter resolvable via
# ``registry.get_adapter(AdapterKind.CODEX)``. The factory is invoked lazily by
# the registry, and the registry never imports this module at its own import time,
# so no import cycle is created.
register_adapter(AdapterKind.CODEX, lambda: CodexAdapter())


__all__ = ["CodexAdapter"]
