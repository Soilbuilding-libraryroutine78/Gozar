"""Shared Translation_Layer types and the ``ProviderAdapter`` protocol.

The canonical inbound contract for Gozar is the OpenAI Chat Completions API
(``POST /v1/chat/completions``) plus ``GET /v1/models``. Every Client_Application
speaks this dialect, and the Translation_Layer is responsible for converting it
to and from each upstream Provider's native shape (Requirement 7).

This module defines:

* Strict, typed Pydantic models for the OpenAI Chat Completions request, the
  non-streaming response, the streaming (SSE delta) chunk, and the usage counts.
  The models are intentionally **permissive about extra and optional fields** so
  that any valid OpenAI request or response round-trips through Gozar without
  losing fields the gateway does not itself interpret (for example ``top_p``,
  ``seed``, ``logit_bias``, ``response_format``, provider-specific extensions).
* The :class:`ProviderAdapter` protocol that every per-Provider adapter
  implements. Adapters are pure, side-effect-free functions over these types
  (the most property-test-friendly module in the system).

The ``ProviderRequest`` / ``ProviderResponse`` / ``ProviderChunk`` aliases are
deliberately opaque (``Any``): each concrete adapter narrows them to the native
shape it produces. For an OpenAI-compatible Provider the native shape *is* the
OpenAI shape, so the pass-through adapter uses these models directly.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Opaque provider-side payload aliases.
#
# Each adapter defines what these concretely are. They are kept opaque at the
# protocol boundary so the gateway/routing layers never depend on a specific
# Provider's wire format. The pass-through adapter narrows all three to the
# OpenAI models below.
# ---------------------------------------------------------------------------
ProviderRequest = Any
ProviderResponse = Any
ProviderChunk = Any


class _OpenAIModel(BaseModel):
    """Base for all OpenAI-shaped models.

    ``extra="allow"`` preserves any standard or provider-specific fields that
    Gozar does not explicitly model, so arbitrary valid OpenAI payloads survive a
    parse/serialize round-trip unchanged (Requirement 7.1, 7.3).
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class OpenAIChatMessage(_OpenAIModel):
    """A single message in an OpenAI Chat Completions ``messages`` array.

    Only ``role`` is strictly required; ``content`` may be ``None`` for assistant
    messages that carry ``tool_calls`` instead of text. Tool-related fields and
    any additional OpenAI message fields are preserved.
    """

    role: str = Field(description="One of system, user, assistant, tool, developer.")
    content: Any | None = Field(
        default=None,
        description="String content, an array of content parts, or None for "
        "tool-call-only assistant messages.",
    )
    name: str | None = Field(
        default=None,
        description="Optional author name; for tool messages it identifies the tool.",
    )
    tool_calls: list[dict[str, Any]] | None = Field(
        default=None,
        description="Assistant tool calls, preserved verbatim.",
    )
    tool_call_id: str | None = Field(
        default=None,
        description="For role=tool messages, the id of the tool call being answered.",
    )


class GozarRoutingOptions(BaseModel):
    """Optional Gozar-only routing controls embedded through SDK ``extra_body``."""

    model_config = ConfigDict(extra="forbid")

    chain_id: uuid.UUID | None = None
    include_metadata: bool = Field(
        default=False,
        description=(
            "Opt in to a namespaced 'gozar' response extension on non-streaming "
            "requests. The default response remains strictly OpenAI-compatible."
        ),
    )


class OpenAIChatRequest(_OpenAIModel):
    """An inbound OpenAI Chat Completions request.

    Standard OpenAI fields are typed explicitly; every other valid field
    (``top_p``, ``seed``, ``stop``, ``logit_bias``, ``response_format``,
    ``presence_penalty``, ``frequency_penalty``, ``user``, ...) is accepted and
    preserved via ``extra="allow"``.
    """

    model: str = Field(description="Requested model identifier.")
    messages: list[OpenAIChatMessage] = Field(
        description="Ordered conversation messages.",
    )
    stream: bool = Field(
        default=False,
        description="When true, the response is delivered as an SSE stream.",
    )
    temperature: float | None = Field(default=None)
    max_tokens: int | None = Field(default=None)
    tools: list[dict[str, Any]] | None = Field(
        default=None,
        description="Tool/function definitions, preserved verbatim.",
    )
    tool_choice: Any | None = Field(default=None)
    # This extension is consumed by the gateway and must never reach an upstream
    # provider. Field-level exclusion keeps pass-through adapters safe by default.
    gozar: GozarRoutingOptions | None = Field(default=None, exclude=True)


class OpenAIResponseChoice(_OpenAIModel):
    """One choice in a non-streaming Chat Completions response."""

    index: int = Field(default=0)
    message: OpenAIChatMessage = Field(description="The assistant message produced.")
    finish_reason: str | None = Field(
        default=None,
        description="Why generation stopped: stop, length, tool_calls, etc.",
    )


class UsageCounts(_OpenAIModel):
    """Token accounting reported for a completion.

    These three counts feed the Usage_Recorder and limit evaluation. When a
    Provider does not report usage, callers substitute zeros and flag the record
    as missing provider metering (Requirement 13.2).
    """

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class OpenAIChatResponse(_OpenAIModel):
    """A non-streaming OpenAI Chat Completions response."""

    id: str = Field(description="Unique completion id.")
    object: str = Field(default="chat.completion")
    created: int = Field(description="Unix timestamp (seconds) of creation.")
    model: str = Field(description="Model that produced the completion.")
    choices: list[OpenAIResponseChoice] = Field(default_factory=list)
    usage: UsageCounts | None = Field(default=None)


class OpenAIStreamChoice(_OpenAIModel):
    """One choice in a streaming (SSE) chunk.

    Streaming choices carry a ``delta`` (a partial message fragment) rather than a
    complete ``message``.
    """

    index: int = Field(default=0)
    delta: dict[str, Any] = Field(
        default_factory=dict,
        description="Incremental message fragment (role, content, tool_calls).",
    )
    finish_reason: str | None = Field(default=None)


class OpenAIStreamChunk(_OpenAIModel):
    """A single OpenAI SSE chunk (``object == "chat.completion.chunk"``).

    A stream is a sequence of these chunks serialized as ``data: <json>`` lines
    terminated by ``data: [DONE]`` (the terminator itself is emitted by the
    gateway, not modeled here).
    """

    id: str = Field(description="Completion id shared by all chunks in the stream.")
    object: str = Field(default="chat.completion.chunk")
    created: int = Field(description="Unix timestamp (seconds) of the stream.")
    model: str = Field(description="Model producing the stream.")
    choices: list[OpenAIStreamChoice] = Field(default_factory=list)
    usage: UsageCounts | None = Field(
        default=None,
        description="Some providers include usage on the final chunk.",
    )


class OpenAIModelCard(_OpenAIModel):
    """A single entry in an OpenAI ``GET /v1/models`` listing.

    Mirrors the OpenAI ``Model`` object so OpenAI-compatible client libraries
    (and the official SDK's ``models.list()``) parse the listing unchanged
    (Requirements 7.1, 18.1). ``id`` is the model identifier the client passes back
    as ``model`` on a chat request; ``owned_by`` names the upstream Provider that
    can serve it; ``created`` is a Unix timestamp (seconds).
    """

    id: str = Field(description="Model identifier usable as the request 'model'.")
    object: str = Field(default="model")
    created: int = Field(description="Unix timestamp (seconds).")
    owned_by: str = Field(description="Upstream Provider that owns the model.")


class OpenAIModelList(_OpenAIModel):
    """The OpenAI ``GET /v1/models`` response envelope (``object == "list"``)."""

    object: str = Field(default="list")
    data: list[OpenAIModelCard] = Field(default_factory=list)


@runtime_checkable
class ProviderAdapter(Protocol):
    """Pure conversion between the OpenAI contract and a Provider's native shape.

    Implementations MUST be side-effect-free: they take a value and return a
    value, performing no I/O. Header and authentication substitution is the
    responsibility of the upstream provider client, not the adapter; adapters
    only translate request/response *bodies*.
    """

    def to_provider_request(self, req: OpenAIChatRequest) -> ProviderRequest:
        """Convert an inbound OpenAI request into the Provider's request shape."""
        ...

    def from_provider_response(self, resp: ProviderResponse) -> OpenAIChatResponse:
        """Convert a Provider's non-streaming response into the OpenAI shape."""
        ...

    def from_provider_stream_chunk(
        self, chunk: ProviderChunk
    ) -> OpenAIStreamChunk | None:
        """Convert one Provider stream chunk into an OpenAI SSE chunk.

        Returns ``None`` for upstream chunks that carry no client-facing content
        (for example provider keep-alives or reasoning fragments that must not
        appear in the strict OpenAI output).
        """
        ...

    def extract_usage(self, resp: ProviderResponse) -> UsageCounts:
        """Read token usage from a Provider response, or zeros when absent."""
        ...


__all__ = [
    "ProviderRequest",
    "ProviderResponse",
    "ProviderChunk",
    "OpenAIChatMessage",
    "GozarRoutingOptions",
    "OpenAIChatRequest",
    "OpenAIResponseChoice",
    "UsageCounts",
    "OpenAIChatResponse",
    "OpenAIStreamChoice",
    "OpenAIStreamChunk",
    "OpenAIModelCard",
    "OpenAIModelList",
    "ProviderAdapter",
]
