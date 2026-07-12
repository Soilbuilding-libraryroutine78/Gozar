"""Server-Sent Events (SSE) parsing and OpenAI framing for the streaming hot path.

The streaming ``/v1/chat/completions`` path (Requirement 6.3) pipes an upstream
Provider's SSE response through the Translation_Layer and re-frames it as a clean
OpenAI SSE stream for the Client_Application. This module owns the two small,
pure-ish concerns that sit on either side of the adapter translation:

* **Parsing the upstream SSE byte stream** (:func:`iter_sse_data`). Providers speak
  the SSE wire format: UTF-8 text, events separated by a blank line, each event a set
  of ``field: value`` lines. The only field Gozar needs is ``data``; per the SSE
  specification, multiple ``data`` lines in one event are joined with ``\n`` and a
  single optional leading space after the colon is stripped. Comment lines (starting
  with ``:``, used by some providers as keep-alives) and non-``data`` fields
  (``event``, ``id``, ``retry``) are ignored -- the provider's ``data`` JSON already
  carries the event ``type`` the adapters key off. The parser buffers across
  arbitrary byte-chunk boundaries so an event split across two network chunks is
  reassembled correctly.

* **Framing OpenAI chunks back out** (:func:`format_sse_chunk`, :data:`SSE_DONE`).
  Each translated :class:`~gozar.translation.types.OpenAIStreamChunk` is serialized as
  a ``data: <json>\n\n`` event and the stream is terminated by ``data: [DONE]\n\n``,
  exactly the framing the OpenAI SDKs and LangChain's ``ChatOpenAI`` expect (design:
  research findings on OpenAI SSE).

The adapter translation itself (provider event -> OpenAI chunk) lives in the
Translation_Layer; this module never interprets a provider's event semantics.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from gozar.translation.types import OpenAIStreamChunk

#: The OpenAI SSE end-of-stream sentinel. Emitted once, after the final chunk, to
#: mark the stream complete (design: ``data: [DONE]`` terminator).
SSE_DONE = "data: [DONE]\n\n"

#: The upstream ``data`` payload that marks an OpenAI-compatible Provider's own
#: end-of-stream. It is consumed (not forwarded); the gateway emits its own
#: :data:`SSE_DONE` so the client always sees a single, well-formed terminator.
_UPSTREAM_DONE = "[DONE]"


def is_done(data: str) -> bool:
    """Return whether an upstream ``data`` payload is the ``[DONE]`` terminator."""
    return data.strip() == _UPSTREAM_DONE


def format_sse_chunk(chunk: OpenAIStreamChunk) -> str:
    """Serialize an OpenAI stream chunk as a single ``data: <json>`` SSE event.

    Unset/``None`` fields are dropped so the wire payload stays clean (matching the
    non-streaming response framing), and the JSON is compact (no superfluous
    whitespace) as the OpenAI SDKs emit it.
    """
    payload = chunk.model_dump(mode="json", exclude_none=True)
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


def _data_value(line: str) -> str | None:
    """Return the ``data`` field value for an SSE line, or ``None`` if not ``data``.

    Per the SSE spec the value is everything after the first colon with a single
    optional leading space removed. A line with no colon is a field with an empty
    value; a line whose field is not ``data`` is ignored here.
    """
    field, sep, value = line.partition(":")
    if field != "data":
        return None
    if sep and value.startswith(" "):
        value = value[1:]
    return value


async def iter_sse_data(byte_stream: AsyncIterator[bytes]) -> AsyncIterator[str]:
    """Parse a provider SSE byte stream, yielding each event's ``data`` payload.

    Decodes the incoming bytes as UTF-8 (replacing malformed sequences rather than
    crashing), splits the stream into SSE events on blank lines, and yields the
    concatenated ``data`` value of each event. Buffers across byte-chunk boundaries so
    events that span multiple network chunks are reassembled. Comment lines and
    non-``data`` fields are skipped.
    """
    buffer = ""
    data_lines: list[str] = []

    async for raw in byte_stream:
        if not raw:
            continue
        buffer += raw.decode("utf-8", errors="replace")
        while "\n" in buffer:
            line, _, buffer = buffer.partition("\n")
            line = line.rstrip("\r")
            if line == "":
                # Blank line: dispatch the accumulated event, if any.
                if data_lines:
                    yield "\n".join(data_lines)
                    data_lines = []
                continue
            if line.startswith(":"):
                # SSE comment / keep-alive.
                continue
            value = _data_value(line)
            if value is not None:
                data_lines.append(value)

    # Flush any trailing event that was not terminated by a blank line.
    tail = buffer.rstrip("\r")
    if tail and not tail.startswith(":"):
        value = _data_value(tail)
        if value is not None:
            data_lines.append(value)
    if data_lines:
        yield "\n".join(data_lines)


__all__ = [
    "SSE_DONE",
    "format_sse_chunk",
    "is_done",
    "iter_sse_data",
]
