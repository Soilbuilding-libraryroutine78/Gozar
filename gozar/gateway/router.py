"""Proxy_Gateway HTTP router: the OpenAI-compatible ``/v1`` data-path.

Exposes ``POST /v1/chat/completions`` and ``POST /v1/embeddings`` as drop-in OpenAI
endpoints. Chat supports non-streaming responses and ``stream=true`` SSE. The
``/v1`` surface is authenticated by Client_Token only (presented as an
``Authorization: Bearer`` header) and never shares a session with the admin
control-path. All error conditions are raised as
:class:`~gozar.core.errors.GozarError` subclasses and rendered as OpenAI-compatible
error JSON by the handler registered in :func:`gozar.core.errors.register_exception_handlers`.

``GET /v1/models`` advertises the models reachable via connected, available
Upstream_Credentials, in the OpenAI model-listing shape, and is authenticated by
Client_Token exactly like ``/v1/chat/completions``.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from gozar.core.db import get_session
from gozar.core.errors import AuthError, GozarError, ValidationError
from gozar.gateway.catalog import list_available_models_for_token
from gozar.gateway.embeddings import complete_embedding
from gozar.gateway.pipeline import complete_chat_completion, stream_chat_completion
from gozar.tokens.service import verify
from gozar.translation.types import (
    OpenAIChatRequest,
    OpenAIEmbeddingRequest,
    OpenAIModelList,
)
from gozar.usage.models import TraceLog
from gozar.usage.service import get_trace

# Header a Client_Application may supply to group related requests for session
# affinity (Requirement 12.1).
_SESSION_HEADER = "x-gozar-session"
# Optional per-call override. The same value may be supplied in the JSON body as
# ``gozar.chain_id`` for SDKs that expose provider-specific ``extra_body`` options.
_CHAIN_HEADER = "x-gozar-chain-id"
_TRACE_HEADER = "x-gozar-trace-id"
_UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"
)

# Response headers that keep an SSE stream flowing unbuffered through common
# reverse proxies (disable caching and proxy response buffering).
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _trace_headers(trace_id: uuid.UUID) -> dict[str, str]:
    """Return request-correlation headers compatible with OpenAI SDK tooling."""

    value = str(trace_id)
    return {"x-request-id": value, _TRACE_HEADER: value}


def _routing_headers(trace: TraceLog) -> dict[str, str]:
    """Expose a compact routing summary without changing the OpenAI JSON body."""

    routing = (trace.outbound_meta or {}).get("routing")
    if not isinstance(routing, dict):
        return {}
    headers: dict[str, str] = {}
    for key, header in (
        ("chain_id", "x-gozar-chain-id"),
        ("route", "x-gozar-route"),
        ("selected_node_id", "x-gozar-node-id"),
        ("selected_position", "x-gozar-node-position"),
        ("attempt_count", "x-gozar-attempt-count"),
    ):
        value = routing.get(key)
        if value is not None:
            headers[header] = str(value)

    attempts = routing.get("attempts")
    if isinstance(attempts, list):
        selected = next(
            (
                attempt
                for attempt in reversed(attempts)
                if isinstance(attempt, dict) and attempt.get("outcome") == "success"
            ),
            None,
        )
        if selected is not None:
            for key, header in (
                ("provider", "x-gozar-provider"),
                ("model", "x-gozar-model"),
            ):
                value = selected.get(key)
                if value is not None:
                    headers[header] = str(value)
    return headers


def _gozar_extension(trace: TraceLog) -> dict[str, Any]:
    """Build a client-safe extension without exposing internal account identity."""

    extension: dict[str, Any] = {"trace_id": str(trace.correlation_id)}
    routing = (trace.outbound_meta or {}).get("routing")
    if isinstance(routing, dict):
        public_routing: dict[str, Any] = {
            key: routing[key]
            for key in (
                "chain_id",
                "route",
                "selected_node_id",
                "selected_position",
                "attempt_count",
            )
            if key in routing
        }
        attempts = routing.get("attempts")
        if isinstance(attempts, list):
            public_routing["attempts"] = [
                _public_attempt(attempt)
                for attempt in attempts
                if isinstance(attempt, dict)
            ]
        extension["routing"] = public_routing
    return extension


def _public_attempt(attempt: dict[str, Any]) -> dict[str, Any]:
    """Remove credential labels and account ids from app-facing metadata."""

    public = {
        key: attempt[key]
        for key in (
            "node_id",
            "position",
            "provider",
            "model",
            "duration_ms",
            "outcome",
            "fallback_taken",
            "credential_refreshed",
            "usage",
        )
        if key in attempt
    }
    error = attempt.get("error")
    if isinstance(error, dict):
        public_error = {
            key: error[key]
            for key in (
                "category",
                "code",
                "type",
                "retryable",
                "upstream_status",
            )
            if key in error
        }
        message = error.get("message")
        if isinstance(message, str):
            public_error["message"] = _UUID_PATTERN.sub("[redacted-id]", message)
        public["error"] = public_error
    return public

router = APIRouter(prefix="/v1", tags=["proxy"])


def _bearer_token(authorization: str | None) -> str | None:
    """Extract the Client_Token from an ``Authorization: Bearer <token>`` header.

    Returns ``None`` when the header is absent or not a well-formed bearer header, so
    the pipeline rejects it with a 401 (Requirement 6.2). The token value is never
    logged.
    """
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def _chain_override(
    header_value: str | None,
    body_value: uuid.UUID | None,
) -> uuid.UUID | None:
    """Resolve header/body chain controls and reject ambiguous requests."""

    header_id: uuid.UUID | None = None
    if header_value:
        try:
            header_id = uuid.UUID(header_value.strip())
        except (ValueError, AttributeError) as exc:
            raise ValidationError("x-gozar-chain-id must be a valid UUID") from exc
    if header_id is not None and body_value is not None and header_id != body_value:
        raise ValidationError(
            "x-gozar-chain-id and gozar.chain_id must match when both are supplied"
        )
    return body_value or header_id


@router.post(
    "/chat/completions",
    summary="OpenAI-compatible chat completions",
    response_model=None,
)
async def chat_completions(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse | StreamingResponse:
    """Serve an OpenAI Chat Completions request, streaming or non-streaming.

    Authenticates the Client_Token, enforces its limit, routes through the
    Fallback_Chain with session affinity, translates to/from the selected Provider,
    records usage and a trace, and returns the OpenAI-shaped response. When the
    request sets ``stream=true``, the response is delivered as an OpenAI Server-Sent
    Events stream (Requirement 6.3); otherwise a single JSON body is returned.
    """
    correlation_id = uuid.uuid4()
    request.state.gozar_trace_id = str(correlation_id)
    presented_token = _bearer_token(request.headers.get("Authorization"))
    session_id = request.headers.get(_SESSION_HEADER)

    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 - any decode failure is a client error
        raise ValidationError("request body must be valid JSON") from exc

    try:
        chat_request = OpenAIChatRequest.model_validate(payload)
    except PydanticValidationError as exc:
        raise ValidationError(
            "invalid chat completion request",
            details=exc.errors(include_url=False),
        ) from exc

    chain_override_id = _chain_override(
        request.headers.get(_CHAIN_HEADER),
        chat_request.gozar.chain_id if chat_request.gozar else None,
    )

    try:
        if chat_request.stream:
            # Establish the stream (auth/limit/routing terminal errors raise here and
            # are rendered as OpenAI error JSON) before handing the iterator to
            # Starlette.
            sse_stream = await stream_chat_completion(
                session,
                presented_token=presented_token,
                request=chat_request,
                session_id=session_id,
                chain_override_id=chain_override_id,
                correlation_id=correlation_id,
            )
            return StreamingResponse(
                sse_stream,
                media_type="text/event-stream",
                headers={**_SSE_HEADERS, **_trace_headers(correlation_id)},
            )

        response = await complete_chat_completion(
            session,
            presented_token=presented_token,
            request=chat_request,
            session_id=session_id,
            chain_override_id=chain_override_id,
            correlation_id=correlation_id,
        )
    except GozarError:
        # The pipeline already finalized the request trace for terminal domain
        # errors. Commit that observability record before the transactional dependency
        # rolls back the raised exception, so failed /v1 requests remain debuggable.
        await session.commit()
        raise

    trace = await get_trace(session, correlation_id)
    content = response.model_dump(mode="json", exclude_none=True)
    if chat_request.gozar is not None and chat_request.gozar.include_metadata:
        content["gozar"] = _gozar_extension(trace)
    return JSONResponse(
        status_code=200,
        content=content,
        headers={**_trace_headers(correlation_id), **_routing_headers(trace)},
    )


@router.post(
    "/embeddings",
    summary="OpenAI-compatible embeddings",
    response_model=None,
)
async def embeddings(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Create real provider embeddings through a capability-aware fallback chain."""

    correlation_id = uuid.uuid4()
    request.state.gozar_trace_id = str(correlation_id)
    presented_token = _bearer_token(request.headers.get("Authorization"))
    session_id = request.headers.get(_SESSION_HEADER)

    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 - any decode failure is a client error
        raise ValidationError("request body must be valid JSON") from exc

    try:
        embedding_request = OpenAIEmbeddingRequest.model_validate(payload)
    except PydanticValidationError as exc:
        raise ValidationError(
            "invalid embeddings request",
            details=exc.errors(include_url=False),
        ) from exc

    chain_override_id = _chain_override(
        request.headers.get(_CHAIN_HEADER),
        embedding_request.gozar.chain_id if embedding_request.gozar else None,
    )
    try:
        response = await complete_embedding(
            session,
            presented_token=presented_token,
            request=embedding_request,
            session_id=session_id,
            chain_override_id=chain_override_id,
            correlation_id=correlation_id,
        )
    except GozarError:
        await session.commit()
        raise

    trace = await get_trace(session, correlation_id)
    content = response.model_dump(mode="json", exclude_none=True)
    if embedding_request.gozar is not None and embedding_request.gozar.include_metadata:
        content["gozar"] = _gozar_extension(trace)
    return JSONResponse(
        status_code=200,
        content=content,
        headers={**_trace_headers(correlation_id), **_routing_headers(trace)},
    )


@router.get(
    "/models",
    summary="OpenAI-compatible model listing",
    response_model=None,
)
async def list_models(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Advertise the models reachable via connected, available credentials.

    Authenticated by Client_Token (presented as ``Authorization: Bearer``) exactly
    like ``/v1/chat/completions`` -- a missing or invalid token is rejected with a
    401 and the catalog is never disclosed (Requirements 7.1, 18.1). The body is the
    OpenAI ``GET /v1/models`` shape (``{"object": "list", "data": [{"id", "object":
    "model", ...}]}``) so OpenAI-compatible client libraries can list models without
    changes. For every Provider that currently has a connected, available
    Upstream_Credential, the advertised models are its live listing when one is
    available (see :mod:`gozar.gateway.catalog`), falling back to the
    deployment-configured list otherwise.
    """
    request_id = uuid.uuid4()
    request.state.gozar_trace_id = str(request_id)
    presented_token = _bearer_token(request.headers.get("Authorization"))
    if not presented_token:
        raise AuthError("a valid Client_Token is required")
    settings = request.app.state.settings
    auth = await verify(session, presented_token, settings=settings)
    if auth is None:
        raise AuthError("the presented Client_Token is invalid or not active")

    listing: OpenAIModelList = await list_available_models_for_token(
        session,
        auth.token_id,
        settings=settings,
    )
    return JSONResponse(
        status_code=200,
        content=listing.model_dump(mode="json"),
        headers=_trace_headers(request_id),
    )


__all__ = ["router"]
