"""Provider-specific upstream invocation for the Proxy_Gateway hot path.

The Translation_Layer adapters convert request/response *bodies* but are
deliberately ignorant of *where* and *how* a provider request is sent. This module
fills that small remaining gap for the data path: for each provider adapter kind it
knows the upstream request path and how to assemble the authentication headers from
the selected Upstream_Credential's decrypted material, then drives the resilient
:class:`~gozar.providers.client.UpstreamClient` to make the call.

Keeping this here (rather than in the pure adapters or the generic client) preserves
the existing separation of concerns: adapters stay pure and side-effect-free, the
:class:`UpstreamClient` stays provider-agnostic, and credential acquisition stays in
the Account_Manager. The gateway is the orchestration seam that ties them together.

The request path and the provider-protocol header names are intrinsic to each
provider's wire contract (not deployment-varying values), mirroring how
:class:`~gozar.translation.codex.CodexAdapter` owns its ``chatgpt-account-id`` header
constant. Deployment-varying values (base URLs, timeouts, retries) continue to come
from configuration via the registry and :class:`~gozar.core.config.Settings`.

No secret material is ever logged here; the assembled ``Authorization`` header is
handed straight to the client, which is documented to keep request headers out of its
error messages (see :mod:`gozar.providers.client`).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel

from gozar.accounts.service import ProviderCredentialMaterial
from gozar.core.config import Settings, get_settings
from gozar.core.errors import ConfigError, UpstreamError
from gozar.gateway.streaming import iter_sse_data
from gozar.providers.client import UpstreamClient
from gozar.providers.registry import AdapterKind, ProviderEntry
from gozar.translation.types import OpenAIEmbeddingRequest

# Upstream request path per adapter kind. These are fixed parts of each provider's
# wire protocol, joined onto the configured provider base URL by the UpstreamClient:
#
# * OpenAI / OpenRouter (pass-through): the Chat Completions path.
# * Codex: the Responses API path.
# * Anthropic: the Messages API path.
_UPSTREAM_PATHS: dict[AdapterKind, str] = {
    AdapterKind.OPENAI_COMPAT: "/chat/completions",
    AdapterKind.CODEX: "/responses",
    AdapterKind.ANTHROPIC: "/v1/messages",
}

# Anthropic Messages API requires an API-version header on every request; OAuth
# (Claude subscription) access additionally requires the beta opt-in headers that
# the first-party Claude client sends. These are wire-protocol constants for the
# Anthropic contract, not deployment config.
_ANTHROPIC_VERSION_HEADER = "anthropic-version"
_ANTHROPIC_VERSION = "2023-06-01"
_ANTHROPIC_BETA_HEADER = "anthropic-beta"
# Both betas are required for subscription (OAuth) access: the Claude Code opt-in
# and the OAuth opt-in, sent as a comma-separated list exactly as the first-party
# client does.
_ANTHROPIC_BETA = "claude-code-20250219,oauth-2025-04-20"

# Codex (ChatGPT subscription) Responses API headers. Alongside the bearer token
# and the chatgpt-account-id header (formatted by the adapter), the Codex backend
# expects the experimental Responses beta opt-in and an originator identifying the
# calling client, matching what the first-party Codex client sends. These are
# wire-protocol constants, not deployment config.
_OPENAI_BETA_HEADER = "OpenAI-Beta"
_OPENAI_BETA_RESPONSES = "responses=experimental"
_CODEX_ORIGINATOR_HEADER = "originator"
_CODEX_ORIGINATOR = "codex_cli_rs"


def upstream_path(entry: ProviderEntry) -> str:
    """Return the upstream request path for ``entry``'s provider.

    Raises :class:`ConfigError` (fail closed) for an adapter kind with no known
    path, rather than silently calling the wrong endpoint.
    """
    path = _UPSTREAM_PATHS.get(entry.adapter_kind)
    if path is None:  # pragma: no cover - defensive; all kinds are mapped
        raise ConfigError(
            f"no upstream request path is configured for adapter kind "
            f"{entry.adapter_kind.value!r}"
        )
    return path


def embeddings_path(entry: ProviderEntry) -> str:
    """Return the provider's embeddings path or fail closed when unsupported."""

    if entry.embeddings_path is None:
        raise ConfigError(
            f"upstream provider {entry.provider_id.value!r} does not support "
            "embeddings through Gozar"
        )
    return entry.embeddings_path


def build_auth_headers(
    entry: ProviderEntry,
    material: ProviderCredentialMaterial,
    adapter: Any,
) -> dict[str, str]:
    """Assemble the provider auth headers from decrypted credential material.

    Both subscription (OAuth access token) and API-key credentials authenticate with
    an ``Authorization: Bearer`` header. Codex additionally needs its account-id
    header (formatted by the adapter from the credential's provider account
    reference), the experimental Responses beta opt-in, and an originator header;
    Anthropic additionally needs its version and beta (Claude Code + OAuth) headers.

    The returned mapping carries secret material (the bearer token) and is handed
    directly to the client; it is never logged.
    """
    headers: dict[str, str] = {}
    token = material.access_token or material.api_key
    if token:
        headers["Authorization"] = f"Bearer {token}"

    if entry.adapter_kind is AdapterKind.CODEX:
        # The adapter formats the chatgpt-account-id header (empty mapping when no
        # reference is available), so credential-to-header shaping stays in one place.
        account_headers = getattr(adapter, "account_id_headers", None)
        if callable(account_headers):
            headers.update(account_headers(material.provider_account_ref))
        # The Codex Responses backend additionally requires the experimental
        # Responses beta opt-in and an originator identifying the client.
        headers[_OPENAI_BETA_HEADER] = _OPENAI_BETA_RESPONSES
        headers[_CODEX_ORIGINATOR_HEADER] = _CODEX_ORIGINATOR
    elif entry.adapter_kind is AdapterKind.ANTHROPIC:
        headers[_ANTHROPIC_VERSION_HEADER] = _ANTHROPIC_VERSION
        headers[_ANTHROPIC_BETA_HEADER] = _ANTHROPIC_BETA

    return headers


async def _call_codex_non_streaming(
    entry: ProviderEntry,
    headers: dict[str, str],
    path: str,
    body: Any,
    *,
    settings: Settings,
) -> dict[str, Any]:
    """Serve a "non-streaming" Codex Responses call by aggregating its SSE stream.

    The Codex backend (unlike the OpenAI Chat Completions and Anthropic Messages
    APIs) only ever serves this endpoint as Server-Sent Events -- it rejects a
    request whose body sets ``"stream": false`` with a 400 ("Stream must be set to
    true"), regardless of what the Client_Application asked Gozar for. To honor a
    non-streaming request, Gozar opens the SSE stream itself and aggregates it into
    the single ``response.completed`` event's ``response`` object, which is exactly
    the shape :meth:`~gozar.translation.codex.CodexAdapter.from_provider_response`
    already expects -- so the adapter needs no Codex-specific streaming awareness.

    Raises :class:`UpstreamError` if the stream never emits a completed response
    (a dropped connection or an error event), mirroring the error behavior of a
    buffered call.
    """
    stream_body = dict(body) if isinstance(body, dict) else body
    if isinstance(stream_body, dict):
        stream_body["stream"] = True

    # Codex's completed-response event always carries an empty "output" array; the
    # actual output items (messages, function calls) only appear on the
    # intermediate "response.output_item.done" events during the stream. Collect
    # those and splice them into the completed response's "output" before handing
    # it to the adapter, so the aggregated object is shaped exactly like a real
    # buffered Responses result.
    output_items: list[Any] = []

    client = UpstreamClient(entry, settings=settings)
    try:
        byte_iter = client.stream("POST", path, headers=headers, json=stream_body)
        async for data in iter_sse_data(byte_iter):
            try:
                event = json.loads(data)
            except (TypeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")

            if event_type == "response.output_item.done":
                item = event.get("item")
                if isinstance(item, dict):
                    output_items.append(item)
                continue

            if event_type == "response.completed":
                response = event.get("response")
                if isinstance(response, dict):
                    if not response.get("output"):
                        response["output"] = output_items
                    return response
                continue

            if event_type == "response.failed":
                response = event.get("response")
                error = (response or {}).get("error") if response else event.get("error")
                raise UpstreamError(
                    f"upstream provider {entry.provider_id.value!r} reported a "
                    f"failed response",
                    details=[{"upstream_error": error}] if error else [],
                )
    finally:
        await client.aclose()

    raise UpstreamError(
        f"upstream provider {entry.provider_id.value!r} stream ended without a "
        f"completed response"
    )


def to_json_body(provider_body: Any) -> Any:
    """Coerce an adapter's provider request into a JSON-serialisable payload.

    The Codex and Anthropic adapters already return plain ``dict`` bodies; the
    pass-through adapter returns the original :class:`OpenAIChatRequest` model, which
    is dumped to JSON here (preserving any extra fields the gateway does not model,
    dropping unset/``None`` fields so the upstream payload stays clean).
    """
    if isinstance(provider_body, BaseModel):
        return provider_body.model_dump(mode="json", by_alias=True, exclude_none=True)
    return provider_body


async def call_upstream(
    entry: ProviderEntry,
    material: ProviderCredentialMaterial,
    adapter: Any,
    provider_body: Any,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Make one non-streaming upstream provider call and return the decoded JSON.

    Assembles the auth headers and request path for ``entry``'s provider, then POSTs
    the translated body through the resilient :class:`UpstreamClient`. Propagates
    :class:`~gozar.core.errors.UpstreamError` on a failed call so the pipeline can
    advance to the next available credential.

    This is the default :data:`~gozar.gateway.pipeline.UpstreamCaller`; tests inject a
    fake to exercise the pipeline without real network access.
    """
    settings = settings or get_settings()
    headers = build_auth_headers(entry, material, adapter)
    path = upstream_path(entry)
    body = to_json_body(provider_body)

    if entry.adapter_kind is AdapterKind.CODEX:
        # The Codex backend only serves this endpoint as SSE; aggregate it into the
        # single completed-response object a buffered caller expects (see
        # _call_codex_non_streaming for why this cannot be a plain buffered POST).
        return await _call_codex_non_streaming(
            entry, headers, path, body, settings=settings
        )

    async with UpstreamClient(entry, settings=settings) as client:
        response = await client.request("POST", path, headers=headers, json=body)
    return response.json()


async def call_upstream_embeddings(
    entry: ProviderEntry,
    material: ProviderCredentialMaterial,
    request: OpenAIEmbeddingRequest,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Forward one OpenAI-compatible embeddings request to a capable provider."""

    settings = settings or get_settings()
    headers = build_auth_headers(entry, material, entry.adapter)
    body = request.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
    )
    async with UpstreamClient(entry, settings=settings) as client:
        response = await client.request(
            "POST",
            embeddings_path(entry),
            headers=headers,
            json=body,
        )
    return response.json()


async def call_upstream_stream(
    entry: ProviderEntry,
    material: ProviderCredentialMaterial,
    adapter: Any,
    provider_body: Any,
    *,
    settings: Settings | None = None,
) -> AsyncIterator[bytes]:
    """Open one streaming upstream provider call and yield raw response byte chunks.

    Mirrors :func:`call_upstream` but drives the resilient
    :meth:`~gozar.providers.client.UpstreamClient.stream` pass-through instead of a
    buffered request, so the upstream Server-Sent Events flow to the caller as they
    arrive (Requirement 6.3). The whole body is never buffered.

    The :class:`UpstreamClient` is kept open for the full lifetime of the stream and
    closed when iteration completes or the consumer stops early; establishment
    failures (a non-retryable status, or transport errors before any byte is
    forwarded) surface as :class:`~gozar.core.errors.UpstreamError` on the first
    iteration, which lets the gateway advance to the next available credential. Once
    bytes have been forwarded the stream is never replayed.

    This is the default streaming caller; tests inject a fake to exercise the
    pipeline without real network access.
    """
    settings = settings or get_settings()
    headers = build_auth_headers(entry, material, adapter)
    path = upstream_path(entry)
    body = to_json_body(provider_body)

    client = UpstreamClient(entry, settings=settings)
    try:
        async for chunk in client.stream("POST", path, headers=headers, json=body):
            yield chunk
    finally:
        await client.aclose()


__all__ = [
    "build_auth_headers",
    "call_upstream",
    "call_upstream_embeddings",
    "call_upstream_stream",
    "embeddings_path",
    "to_json_body",
    "upstream_path",
]
