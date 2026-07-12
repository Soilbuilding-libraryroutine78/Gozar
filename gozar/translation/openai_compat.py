"""Pass-through adapter for OpenAI-compatible Providers.

Many Providers (OpenAI itself, OpenRouter, and other OpenAI-compatible gateways)
already speak the OpenAI Chat Completions dialect. For these, no body
translation is required: the request and response shapes Gozar receives from the
Client_Application are exactly what the Provider expects and returns. The only
difference between calling Gozar and calling the Provider directly is the
authentication header, and that substitution happens in the upstream provider
client (which swaps the Client_Token for the selected Upstream_Credential's
secret) -- never in this adapter.

This adapter is therefore an identity mapping over the OpenAI types. It is the
basis for the response-translation pass-through identity property (design
Property 15): for an OpenAI-compatible Provider, translating a response in and
back out changes nothing.
"""

from __future__ import annotations

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


class OpenAICompatAdapter:
    """Identity/pass-through :class:`ProviderAdapter` for OpenAI-compatible Providers.

    Every method returns its input in the OpenAI shape unchanged. Header and auth
    substitution is handled by the provider client, so this adapter performs no
    body mutation at all.
    """

    def to_provider_request(self, req: OpenAIChatRequest) -> ProviderRequest:
        """Return the request unchanged; the Provider already speaks OpenAI."""
        return req

    def from_provider_response(self, resp: ProviderResponse) -> OpenAIChatResponse:
        """Return the response unchanged when it is already an OpenAI response.

        Accepts either a parsed :class:`OpenAIChatResponse` (returned as-is) or a
        raw mapping decoded from the upstream JSON body (validated into the
        OpenAI shape, preserving any extra fields).
        """
        if isinstance(resp, OpenAIChatResponse):
            return resp
        return OpenAIChatResponse.model_validate(resp)

    def from_provider_stream_chunk(
        self, chunk: ProviderChunk
    ) -> OpenAIStreamChunk | None:
        """Return the stream chunk unchanged.

        Accepts either a parsed :class:`OpenAIStreamChunk` or a raw mapping; a
        ``None`` chunk (no client-facing content) is passed through as ``None``.
        """
        if chunk is None:
            return None
        if isinstance(chunk, OpenAIStreamChunk):
            return chunk
        return OpenAIStreamChunk.model_validate(chunk)

    def extract_usage(self, resp: ProviderResponse) -> UsageCounts:
        """Read the ``usage`` field from an OpenAI-shaped response.

        Returns zeroed counts when the Provider omits usage, so the caller can
        flag the record as missing provider metering (Requirement 13.2).
        """
        usage: object = None
        if isinstance(resp, OpenAIChatResponse):
            usage = resp.usage
        elif isinstance(resp, dict):
            usage = resp.get("usage")

        if usage is None:
            return UsageCounts()
        if isinstance(usage, UsageCounts):
            return usage
        return UsageCounts.model_validate(usage)


# Register this adapter with the provider registry so importing the translation
# package (which imports this module) makes the pass-through adapter resolvable via
# ``registry.get_adapter(AdapterKind.OPENAI_COMPAT)`` for OpenAI / OpenRouter. The
# factory is invoked lazily by the registry, and the registry never imports this
# module at its own import time, so no import cycle is created.
register_adapter(AdapterKind.OPENAI_COMPAT, lambda: OpenAICompatAdapter())


__all__ = ["OpenAICompatAdapter"]
