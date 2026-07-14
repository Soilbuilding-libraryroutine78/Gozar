"""OpenAI-compatible embeddings orchestration.

Embeddings use the same stable Gozar API key, limits, chain selection, account
availability, fallback policy, usage metering, and trace storage as Chat
Completions. Only providers that explicitly advertise an embeddings endpoint are
eligible; unsupported subscription backends are skipped rather than asked to
produce a fabricated vector.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from time import perf_counter
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from gozar.accounts.service import ProviderCredentialMaterial, get_usable_token
from gozar.core.config import Settings, get_settings
from gozar.core.errors import GozarError, NoAvailableAccount, UpstreamError
from gozar.core.redis import get_redis
from gozar.gateway.pipeline import (
    MaterialAcquirer,
    ReactiveRefresher,
    _PipelineTrace,
    _allows_fallback,
    _authenticate_and_route,
    _credential_trace_snapshots,
    _default_reactive_refresh,
    _fallback_exhausted_error,
    _outcome_for_error,
    _record_failed_attempt,
    _record_successful_attempt,
    _should_refresh_after_upstream_auth_error,
    _usage_event,
    _utcnow,
)
from gozar.gateway.upstream import call_upstream_embeddings
from gozar.providers.registry import ProviderEntry, get_provider
from gozar.routing.chains import RouteKind, RoutingTarget
from gozar.routing.session import record_session_binding
from gozar.translation.types import (
    OpenAIEmbeddingRequest,
    OpenAIEmbeddingResponse,
    UsageCounts,
)
from gozar.usage.service import (
    InboundMeta,
    OutboundMeta,
    finalize_trace,
    open_trace,
    record_usage,
)

EmbeddingUpstreamCaller = Callable[
    [ProviderEntry, ProviderCredentialMaterial, OpenAIEmbeddingRequest],
    Awaitable[dict[str, Any]],
]


def _usage_counts(response: OpenAIEmbeddingResponse) -> UsageCounts:
    return UsageCounts(
        prompt_tokens=response.usage.prompt_tokens,
        completion_tokens=0,
        total_tokens=response.usage.total_tokens,
    )


def _parse_response(
    raw: dict[str, Any],
    *,
    provider: str,
) -> OpenAIEmbeddingResponse:
    try:
        return OpenAIEmbeddingResponse.model_validate(raw)
    except PydanticValidationError as exc:
        raise UpstreamError(
            f"upstream provider {provider!r} returned an invalid embeddings response",
            details=[{"validation_errors": exc.error_count()}],
        ) from exc


def _request_for_target(
    request: OpenAIEmbeddingRequest,
    target: RoutingTarget,
) -> OpenAIEmbeddingRequest:
    """Apply the embedding model selected for this lane node, when configured."""

    if target.model_id is None or target.model_id == request.model:
        return request
    return request.model_copy(update={"model": target.model_id})


def _embedding_capable_targets(
    targets: list[RoutingTarget],
    snapshots: dict[uuid.UUID, Any],
    *,
    settings: Settings,
) -> list[RoutingTarget]:
    capable: list[RoutingTarget] = []
    for target in targets:
        snapshot = snapshots.get(target.account_id)
        if snapshot is None:
            continue
        entry = get_provider(snapshot.provider, settings=settings)
        if entry.embeddings_path is not None:
            capable.append(target)
    return capable


async def _call_with_reactive_refresh(
    session: AsyncSession,
    target: RoutingTarget,
    request: OpenAIEmbeddingRequest,
    *,
    settings: Settings,
    acquire: MaterialAcquirer,
    upstream: EmbeddingUpstreamCaller,
    reactive_refresh: ReactiveRefresher,
) -> tuple[ProviderCredentialMaterial, dict[str, Any], bool]:
    material = await acquire(session, target.account_id)
    entry = get_provider(material.provider, settings=settings)
    try:
        return material, await upstream(entry, material, request), False
    except UpstreamError as exc:
        if not _should_refresh_after_upstream_auth_error(exc, material):
            raise
        if not await reactive_refresh(session, target.account_id):
            raise

    material = await acquire(session, target.account_id)
    entry = get_provider(material.provider, settings=settings)
    return material, await upstream(entry, material, request), True


async def complete_embedding(
    session: AsyncSession,
    *,
    presented_token: str | None,
    request: OpenAIEmbeddingRequest,
    session_id: str | None = None,
    chain_override_id: uuid.UUID | None = None,
    trusted_token_id: uuid.UUID | None = None,
    redis: Redis | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
    upstream: EmbeddingUpstreamCaller | None = None,
    acquire_material: MaterialAcquirer | None = None,
    refresh_on_auth_error: ReactiveRefresher | None = None,
    correlation_id: uuid.UUID | None = None,
) -> OpenAIEmbeddingResponse:
    """Route one embeddings request through the selected capability-aware chain."""

    settings = settings or get_settings()
    redis = redis or get_redis()
    now = now or _utcnow()
    upstream = upstream or (
        lambda entry, material, body: call_upstream_embeddings(
            entry,
            material,
            body,
            settings=settings,
        )
    )
    acquire = acquire_material or (
        lambda sess, account_id: get_usable_token(
            sess,
            account_id,
            settings=settings,
            now=now,
        )
    )
    reactive_refresh = refresh_on_auth_error or (
        lambda sess, account_id: _default_reactive_refresh(
            sess,
            account_id,
            settings=settings,
        )
    )

    correlation_id = correlation_id or uuid.uuid4()
    trace_context = _PipelineTrace()
    await open_trace(
        session,
        correlation_id,
        InboundMeta(
            method="POST",
            endpoint="embeddings",
            model=request.model,
            stream=False,
            session_id=session_id,
            chain_id=chain_override_id,
        ),
        now=now,
    )

    try:
        token_id, attempt_order = await _authenticate_and_route(
            session,
            redis=redis,
            settings=settings,
            now=now,
            presented_token=presented_token,
            request=request,
            session_id=session_id,
            chain_override_id=chain_override_id,
            trusted_token_id=trusted_token_id,
            correlation_id=correlation_id,
            trace_context=trace_context,
            route_kind=RouteKind.EMBEDDINGS,
        )
        snapshots = await _credential_trace_snapshots(session, attempt_order)
        capable_targets = _embedding_capable_targets(
            attempt_order,
            snapshots,
            settings=settings,
        )
        if not capable_targets:
            raise NoAvailableAccount(
                "no available embeddings account: the selected chain needs an "
                "active OpenAI or OpenRouter API-key credential"
            )

        last_error: GozarError | None = None
        for index, target in enumerate(capable_targets):
            started = perf_counter()
            effective_request = _request_for_target(request, target)
            try:
                material, raw, refreshed = await _call_with_reactive_refresh(
                    session,
                    target,
                    effective_request,
                    settings=settings,
                    acquire=acquire,
                    upstream=upstream,
                    reactive_refresh=reactive_refresh,
                )
                response = _parse_response(raw, provider=material.provider)
            except GozarError as exc:
                last_error = exc
                fallback_allowed = _allows_fallback(target, exc)
                fallback_taken = fallback_allowed and index < len(capable_targets) - 1
                _record_failed_attempt(
                    trace_context,
                    target,
                    snapshots.get(target.account_id),
                    exc,
                    model=effective_request.model,
                    duration_ms=(perf_counter() - started) * 1000,
                    fallback_taken=fallback_taken,
                )
                if not fallback_allowed:
                    break
                continue

            counts = _usage_counts(response)
            _record_successful_attempt(
                trace_context,
                target,
                snapshots.get(target.account_id),
                provider=material.provider,
                model=effective_request.model,
                duration_ms=(perf_counter() - started) * 1000,
                refreshed=refreshed,
                usage=counts,
            )
            await record_usage(
                session,
                _usage_event(
                    correlation_id,
                    token_id,
                    target.account_id,
                    material.provider,
                    counts,
                ),
                redis=redis,
                now=now,
            )
            if session_id:
                await record_session_binding(
                    session_id,
                    target.account_id,
                    redis=redis,
                    settings=settings,
                    route_kind=RouteKind.EMBEDDINGS,
                )
            await finalize_trace(
                session,
                correlation_id,
                OutboundMeta(
                    outcome="success",
                    status_code=200,
                    account_id=target.account_id,
                    credential=snapshots.get(target.account_id),
                    model=response.model,
                    routing=trace_context.routing_meta(),
                ),
                now=_utcnow(),
            )
            return response

        raise _fallback_exhausted_error(last_error)
    except GozarError as exc:
        await finalize_trace(
            session,
            correlation_id,
            OutboundMeta(
                outcome=_outcome_for_error(exc),
                status_code=exc.status_code,
                routing=trace_context.routing_meta(),
            ),
            now=_utcnow(),
        )
        raise


__all__ = ["EmbeddingUpstreamCaller", "complete_embedding"]
