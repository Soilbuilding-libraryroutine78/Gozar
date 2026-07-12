"""Async upstream provider HTTP client.

This module implements the single network seam between Gozar and the upstream
Providers (steering §7/§16: every external call considers timeout, retry with
backoff, and error handling). It is intentionally **provider-agnostic**: request
and response *translation* lives in the ``translation`` module, credential
acquisition lives in ``accounts``, and routing/fallback lives in ``routing``. This
client only knows how to make a resilient HTTP call to a Provider's base URL and
stream the response back.

Responsibilities (task 6.2 / Requirements 6.1, 6.3)
---------------------------------------------------
* **Per-call timeout** sourced from :class:`~gozar.core.config.Settings`
  (``GOZAR_UPSTREAM_REQUEST_TIMEOUT_SECONDS``); never hardcoded.
* **Retry only transient failures** - HTTP ``429``, any ``5xx``, and
  connection/timeout transport errors - using **exponential backoff with jitter**
  bounded by a configurable maximum number of attempts
  (``GOZAR_UPSTREAM_MAX_ATTEMPTS``, ``GOZAR_UPSTREAM_BACKOFF_BASE_SECONDS``,
  ``GOZAR_UPSTREAM_BACKOFF_MAX_SECONDS``). Non-retryable client errors (any ``4xx``
  other than ``429``) are surfaced immediately without retry.
* **Streaming pass-through** - :meth:`UpstreamClient.stream` yields raw response
  chunks as they arrive without buffering the whole body, so SSE responses are
  piped to the client with minimal latency. A stream is only retried while it has
  not yet emitted any bytes; once data has been forwarded, a mid-stream failure is
  raised rather than replayed.
* **Caller-provided auth headers** - the client injects whatever headers the
  caller prepares (bearer token / api key / account-id header). It never fetches
  or stores credentials itself.
* **Typed, secret-free errors** - on exhausted retries or a non-retryable upstream
  status it raises :class:`~gozar.core.errors.UpstreamError`. Error messages and
  details never include request headers (which carry the credential), so secrets
  cannot leak into logs or client responses.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any

import httpx

from gozar.core.config import Settings, get_settings
from gozar.core.errors import UpstreamError
from gozar.providers.registry import ProviderEntry

# Transport-level exceptions that indicate a transient, safe-to-retry failure
# (the request did not get a complete HTTP response). ``httpx.TimeoutException``
# covers connect/read/write/pool timeouts; ``httpx.ConnectError`` and the broader
# ``httpx.TransportError`` cover connection resets and network interruptions.
_RETRYABLE_TRANSPORT_ERRORS = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.NetworkError,
)

#: HTTP status that is always safe to retry (rate limited).
_RATE_LIMIT_STATUS = 429

#: Maximum number of body bytes copied into an error's details. Bounds memory and
#: avoids dumping large upstream payloads into logs. Provider error bodies are not
#: secret, but request headers (which are) are never included.
_MAX_ERROR_BODY_CHARS = 512


def _is_retryable_status(status_code: int) -> bool:
    """Return ``True`` for upstream statuses that should be retried.

    Only HTTP ``429`` (rate limited) and ``5xx`` (server errors) are transient.
    Every other ``4xx`` is a client error that retrying cannot fix.
    """
    return status_code == _RATE_LIMIT_STATUS or 500 <= status_code <= 599


SleepFn = Callable[[float], Awaitable[None]]


class UpstreamClient:
    """A resilient async HTTP client bound to a single Provider's base URL.

    Parameters
    ----------
    entry:
        The resolved :class:`ProviderEntry` whose ``base_url`` every request is
        made against.
    settings:
        Application settings supplying the per-call timeout and retry/backoff
        configuration. Defaults to the process settings singleton.
    client:
        An optional pre-built :class:`httpx.AsyncClient`. Inject one in tests
        (for example backed by :class:`httpx.MockTransport`) to mock at the
        transport level without touching the network. When omitted, the client
        owns and lazily creates an :class:`httpx.AsyncClient`.
    sleep:
        The async sleep used between retries. Defaults to :func:`asyncio.sleep`;
        inject a fake in tests to avoid real delays.
    rng:
        Random source for backoff jitter. Inject a seeded
        :class:`random.Random` for deterministic tests.

    Notes
    -----
    Adapters translate request/response *bodies*; this client only moves bytes and
    injects the caller-prepared headers. It is safe to share one instance across
    many concurrent requests.
    """

    def __init__(
        self,
        entry: ProviderEntry,
        *,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        sleep: SleepFn | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._entry = entry
        self._settings = settings or get_settings()
        self._sleep: SleepFn = sleep or asyncio.sleep
        self._rng = rng or random.Random()

        # When the client is injected we do not own its lifecycle and must not
        # close it; when we build our own we close it on aclose()/context exit.
        self._client = client
        self._owns_client = client is None

        self._timeout = self._settings.upstream_request_timeout_seconds
        self._max_attempts = max(1, self._settings.upstream_max_attempts)
        self._backoff_base = self._settings.upstream_backoff_base_seconds
        self._backoff_max = self._settings.upstream_backoff_max_seconds

    # --- lifecycle -----------------------------------------------------------
    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    async def aclose(self) -> None:
        """Close the underlying client if this instance owns it."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "UpstreamClient":
        self._ensure_client()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # --- helpers -------------------------------------------------------------
    def _url(self, path: str) -> str:
        """Join the provider base URL with a request path.

        ``httpx`` resolves relative paths against the client's base URL, but since
        we do not assume a base_url was set on an injected client, we join here so
        behavior is identical regardless of how the client was constructed.
        """
        base = self._entry.base_url.rstrip("/")
        if not path:
            return base
        if path.startswith(("http://", "https://")):
            return path
        return f"{base}/{path.lstrip('/')}"

    def _backoff_delay(self, attempt: int) -> float:
        """Compute the backoff delay (seconds) before the given retry ``attempt``.

        Uses capped exponential backoff with "equal jitter": the capped exponential
        term is ``min(backoff_max, base * 2**(attempt-1))`` and the actual delay is
        half of that plus a random amount up to the other half. Equal jitter keeps a
        non-zero floor (so retries always wait a little) while spreading retries to
        avoid a thundering herd. ``attempt`` is 1-based for the first retry.
        """
        if self._backoff_base <= 0:
            return 0.0
        exponential = self._backoff_base * (2 ** max(0, attempt - 1))
        capped = min(self._backoff_max, exponential) if self._backoff_max > 0 else exponential
        half = capped / 2.0
        return half + self._rng.uniform(0.0, half)

    def _error_body_snippet(self, body: bytes | str | None) -> str | None:
        """Return a bounded, decoded snippet of an upstream error body.

        Provider error bodies are safe to surface (they are the Provider's own
        message, not Gozar's credentials). Request headers - which carry the
        credential - are never passed here, so no secret can leak.
        """
        if not body:
            return None
        if isinstance(body, bytes):
            text = body.decode("utf-8", errors="replace")
        else:
            text = body
        text = text.strip()
        if not text:
            return None
        if len(text) > _MAX_ERROR_BODY_CHARS:
            text = text[:_MAX_ERROR_BODY_CHARS] + "..."
        return text

    def _status_error(
        self, status_code: int, body: bytes | str | None
    ) -> UpstreamError:
        """Build a secret-free :class:`UpstreamError` for a failed upstream status."""
        details: list[Any] = [{"upstream_status": status_code}]
        snippet = self._error_body_snippet(body)
        if snippet is not None:
            details.append({"upstream_body": snippet})
        return UpstreamError(
            f"upstream provider {self._entry.provider_id.value!r} returned "
            f"status {status_code}",
            details=details,
        )

    def _transport_error(self, attempts: int, exc: Exception) -> UpstreamError:
        """Build a secret-free :class:`UpstreamError` for an exhausted transport failure."""
        return UpstreamError(
            f"upstream provider {self._entry.provider_id.value!r} call failed after "
            f"{attempts} attempt(s): {type(exc).__name__}",
            details=[{"attempts": attempts, "error": type(exc).__name__}],
        )

    # --- non-streaming request ----------------------------------------------
    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: Any | None = None,
        content: bytes | str | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        """Make a non-streaming upstream request and return the response.

        Retries transient failures (429/5xx/transport errors) with exponential
        backoff and jitter, up to the configured maximum attempts. On a successful
        (2xx/3xx) response the :class:`httpx.Response` is returned with its body
        already read. On a non-retryable error status or after exhausting retries a
        :class:`~gozar.core.errors.UpstreamError` is raised.

        Auth headers must be supplied by the caller via ``headers``; this method
        does not add or fetch credentials.
        """
        client = self._ensure_client()
        url = self._url(path)
        attempt = 0
        last_exc: Exception | None = None

        while attempt < self._max_attempts:
            attempt += 1
            try:
                response = await client.request(
                    method,
                    url,
                    headers=dict(headers) if headers else None,
                    json=json,
                    content=content,
                    params=dict(params) if params else None,
                    timeout=self._timeout,
                )
            except _RETRYABLE_TRANSPORT_ERRORS as exc:
                last_exc = exc
                if attempt < self._max_attempts:
                    await self._sleep(self._backoff_delay(attempt))
                    continue
                raise self._transport_error(attempt, exc) from exc

            if _is_retryable_status(response.status_code) and attempt < self._max_attempts:
                # Drain so the connection can be reused, then back off and retry.
                await response.aread()
                await self._sleep(self._backoff_delay(attempt))
                continue

            if response.status_code >= 400:
                body = await response.aread()
                raise self._status_error(response.status_code, body)

            await response.aread()
            return response

        # Loop only exits via return/raise above except when the final attempt was
        # a retryable status (max_attempts reached). Reconstruct that terminal error.
        if last_exc is not None:  # pragma: no cover - defensive
            raise self._transport_error(attempt, last_exc) from last_exc
        raise UpstreamError(  # pragma: no cover - defensive
            f"upstream provider {self._entry.provider_id.value!r} exhausted "
            f"{self._max_attempts} attempt(s)",
            details=[{"attempts": self._max_attempts}],
        )

    # --- streaming pass-through ----------------------------------------------
    async def stream(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: Any | None = None,
        content: bytes | str | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[bytes]:
        """Stream an upstream response, yielding raw byte chunks as they arrive.

        The whole body is never buffered: chunks are yielded as soon as the
        transport delivers them, so SSE responses pass straight through to the
        client. Establishing the stream is retried on transient failures (429/5xx
        before any bytes are read, and transport errors), but once the first byte
        has been forwarded the stream is not retried - a mid-stream failure raises
        :class:`~gozar.core.errors.UpstreamError` rather than replaying the request.

        Auth headers must be supplied by the caller via ``headers``.
        """
        client = self._ensure_client()
        url = self._url(path)
        attempt = 0

        while attempt < self._max_attempts:
            attempt += 1
            yielded = False
            try:
                async with client.stream(
                    method,
                    url,
                    headers=dict(headers) if headers else None,
                    json=json,
                    content=content,
                    params=dict(params) if params else None,
                    timeout=self._timeout,
                ) as response:
                    if response.status_code >= 400:
                        if (
                            _is_retryable_status(response.status_code)
                            and attempt < self._max_attempts
                        ):
                            await response.aread()
                            await self._sleep(self._backoff_delay(attempt))
                            continue
                        body = await response.aread()
                        raise self._status_error(response.status_code, body)

                    async for chunk in response.aiter_bytes():
                        if chunk:
                            yielded = True
                            yield chunk
                    return
            except _RETRYABLE_TRANSPORT_ERRORS as exc:
                # Never replay a stream that already emitted bytes to the client.
                if not yielded and attempt < self._max_attempts:
                    await self._sleep(self._backoff_delay(attempt))
                    continue
                raise self._transport_error(attempt, exc) from exc


__all__ = ["UpstreamClient"]
