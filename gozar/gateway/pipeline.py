"""Proxy_Gateway orchestration: the non-streaming ``/v1/chat/completions`` hot path.

This module wires together the modules built in earlier tasks into the single
request data-path the design describes (design: "Request data-path (proxy hot
path)"). It is deliberately the only place that knows the *order* of the steps; each
step delegates to the module that owns it, so the pipeline stays a thin orchestrator:

1. **Authenticate the Client_Token** via :func:`gozar.tokens.service.verify`. A
   missing or invalid token is rejected with a 401 and **no upstream call is made**
   (Requirement 6.2).
2. **Enforce token status and limit** -- ``verify`` already rejects disabled/revoked
   tokens (Requirements 9.3, 9.4); here we additionally reject a token that has
   reached its configured Usage_Limit (Requirement 9.2) using the pure
   :func:`gozar.usage.limits.limit_reached` against the Redis consumption counters.
3. **Resolve session affinity** from the ``x-gozar-session`` header (Requirement 12).
4. **Build the attempt order** via the Flow_Controller: use the Client_Token's
   assigned Fallback_Chain when one is configured; otherwise select the chain for
   the requested model, snapshot each entry's :class:`CredentialState`, and apply
   :func:`gozar.routing.session.get_attempt_order` (Requirements 10.1, 11.x, 12.2).
5. **Translate, call, and fall back**: for each available credential, acquire usable
   material (:func:`gozar.accounts.service.get_usable_token`, which lazily refreshes
   subscription tokens), translate the request, call upstream, and on any error
   advance to the next available entry (Requirements 6.1, 7.x, 10.2, 11.x). If no
   credential is available up front -> "no available account" (Requirement 6.4); if
   every attempt fails -> "all fallbacks failed" (Requirement 10.3).
6. **Record the outcome** via the Usage_Recorder: a Trace_Log is opened at entry and
   finalized at the end, and a UsageRecord + consumption counters are written on
   success (Requirements 13.1, 14.1, 14.2).

Streaming (task 12.2) and ``GET /v1/models`` (task 12.3) are separate tasks. The
pipeline is factored so streaming can reuse the same selection/fallback logic: the
network call is isolated behind the injectable :data:`UpstreamCaller`, and credential
acquisition behind :data:`MaterialAcquirer`, so a streaming variant only needs to
swap the call/return half.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Protocol

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gozar.accounts.models import (
    AccountUsageLimit,
    CredentialKind,
    CredentialStatus,
    UpstreamCredential,
)
from gozar.accounts.service import (
    ProviderCredentialMaterial,
    get_usable_token,
    refresh_subscription,
)
from gozar.core.config import Settings, get_settings
from gozar.core.errors import (
    AuthError,
    GozarError,
    NotFound,
    NoAvailableAccount,
    RateLimitError,
    UpstreamError,
)
from gozar.core.redis import get_redis
from gozar.gateway.streaming import SSE_DONE, format_sse_chunk, is_done, iter_sse_data
from gozar.gateway.upstream import call_upstream, call_upstream_stream
from gozar.providers.registry import ProviderEntry, get_provider
from gozar.routing.chains import FallbackPolicy, RouteKind, RoutingChain, RoutingTarget
from gozar.routing.service import list_chains, load_routing_chain
from gozar.routing.session import get_attempt_order, record_session_binding
from gozar.routing.state import CredentialState
from gozar.tokens.models import ClientToken, TokenStatus, TokenUsageLimit
from gozar.tokens.service import verify
from gozar.translation.types import OpenAIChatRequest, OpenAIChatResponse, UsageCounts
from gozar.usage.limits import LimitMetric, LimitWindow, UsageLimitSpec, limit_reached
from gozar.usage.service import (
    SUBJECT_ACCOUNT,
    SUBJECT_TOKEN,
    CredentialTraceSnapshot,
    InboundMeta,
    OutboundMeta,
    UsageEvent,
    finalize_trace,
    open_trace,
    read_counter,
    record_usage,
    set_trace_chain,
)

# Injectable network seam: given the resolved provider entry, the decrypted
# credential material, the provider adapter, and the translated provider request
# body, perform the upstream call and return the decoded JSON response. Defaults to
# :func:`gozar.gateway.upstream.call_upstream`; tests inject a fake.
UpstreamCaller = Callable[
    [ProviderEntry, ProviderCredentialMaterial, Any, Any],
    Awaitable[dict[str, Any]],
]

# Injectable credential-acquisition seam: given a session and an account id, return
# usable :class:`ProviderCredentialMaterial` (refreshing lazily as needed). Defaults
# to :func:`gozar.accounts.service.get_usable_token`; tests may inject a fake.
MaterialAcquirer = Callable[
    [AsyncSession, uuid.UUID],
    Awaitable[ProviderCredentialMaterial],
]

# Injectable reactive refresh seam: when a subscription access token is rejected by
# upstream with 401 before any response bytes are committed, force-refresh that
# account and let the caller retry once with freshly acquired material.
ReactiveRefresher = Callable[[AsyncSession, uuid.UUID], Awaitable[bool]]


class ModelRequest(Protocol):
    """Minimal request contract shared by chat and embeddings routing."""

    model: str

# Injectable streaming network seam: the streaming analogue of :data:`UpstreamCaller`.
# Given the resolved provider entry, decrypted credential material, the provider
# adapter, and the translated provider request body, open the upstream streaming call
# and return an async iterator of raw response byte chunks. Defaults to
# :func:`gozar.gateway.upstream.call_upstream_stream`; tests inject a fake.
StreamingUpstreamCaller = Callable[
    [ProviderEntry, ProviderCredentialMaterial, Any, Any],
    AsyncIterator[bytes],
]

# Sentinel distinguishing "the upstream stream established but produced no bytes"
# from a real first chunk, so an empty-but-successful stream is not mistaken for a
# fallback-worthy failure.
_STREAM_EMPTY = object()


@dataclass
class _PipelineTrace:
    """Mutable, secret-free routing facts collected while one request executes."""

    chain_id: uuid.UUID | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    selected_node_id: uuid.UUID | None = None
    selected_position: int | None = None
    route_kind: RouteKind | None = None

    def routing_meta(self) -> dict[str, Any] | None:
        """Return the namespaced routing metadata persisted with the trace."""

        if self.chain_id is None and not self.attempts:
            return None
        meta: dict[str, Any] = {
            "attempt_count": len(self.attempts),
            "attempts": list(self.attempts),
        }
        if self.chain_id is not None:
            meta["chain_id"] = str(self.chain_id)
        if self.route_kind is not None:
            meta["route"] = self.route_kind.value
        if self.selected_node_id is not None:
            meta["selected_node_id"] = str(self.selected_node_id)
        if self.selected_position is not None:
            meta["selected_position"] = self.selected_position
        return meta


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _spec_from_token_row(row: TokenUsageLimit) -> UsageLimitSpec:
    """Reconstruct a :class:`UsageLimitSpec` from a persisted token-limit row."""
    return UsageLimitSpec(
        metric=LimitMetric(row.metric),
        limit_value=float(row.limit_value),
        capacity=float(row.capacity) if row.capacity is not None else None,
        window=LimitWindow(row.window),
    )


def _spec_from_account_row(row: AccountUsageLimit) -> UsageLimitSpec:
    """Reconstruct a :class:`UsageLimitSpec` from a persisted account-limit row."""
    return UsageLimitSpec(
        metric=row.metric,
        limit_value=float(row.limit_value),
        capacity=float(row.capacity) if row.capacity is not None else None,
        window=row.window,
    )


def _counter_metric(spec: UsageLimitSpec) -> LimitMetric:
    """Pick which metered counter backs a limit's consumption read.

    The Usage_Recorder meters two counters per request: ``request_count`` and
    ``token_count``. A ``request_count`` limit reads the request counter; every other
    metric (``token_count``, ``cost_estimate``, ``percentage``) is evaluated against
    accumulated token consumption, which is the only token-volume signal available.
    """
    if spec.metric is LimitMetric.REQUEST_COUNT:
        return LimitMetric.REQUEST_COUNT
    return LimitMetric.TOKEN_COUNT


async def _limit_reached_for(
    redis: Redis,
    subject_kind: str,
    subject_id: uuid.UUID,
    spec: UsageLimitSpec,
    *,
    now: datetime,
) -> bool:
    """Return whether ``subject_id`` has reached ``spec`` given current counters."""
    consumption = await read_counter(
        redis, subject_kind, subject_id, _counter_metric(spec), spec.window, now=now
    )
    return limit_reached(consumption, spec)


async def _enforce_token_limit(
    session: AsyncSession,
    redis: Redis,
    token_id: uuid.UUID,
    *,
    now: datetime,
) -> None:
    """Reject a request whose Client_Token has reached its limit (Requirement 9.2).

    Reads the most recently persisted token limit (Requirement 9.1) and the live
    consumption counter for the limit's window. Raises :class:`RateLimitError` (429)
    when the limit is reached; returns silently when no limit is configured or the
    token is still under it.
    """
    row = await session.scalar(
        select(TokenUsageLimit).where(TokenUsageLimit.subject_id == token_id)
    )
    if row is None:
        return
    spec = _spec_from_token_row(row)
    if await _limit_reached_for(redis, SUBJECT_TOKEN, token_id, spec, now=now):
        raise RateLimitError(
            "client token has reached its configured usage limit"
        )


async def _select_chain(
    session: AsyncSession,
    model: str,
    *,
    route_kind: RouteKind = RouteKind.CHAT,
    assigned_chain_id: uuid.UUID | None = None,
    override_chain_id: uuid.UUID | None = None,
) -> RoutingChain | None:
    """Select the Fallback_Chain to route a request through.

    When the authenticated Client_Token is pinned to a chain, that chain is used
    directly and its model selector is ignored; the token itself becomes the routing
    choice. Otherwise auto mode considers only chains containing the requested
    endpoint lane, prefers an exact ``model_selector`` match, then falls back to the
    first eligible chain without a selector. Returns ``None`` when no chain applies.
    """
    selected_chain_id = override_chain_id or assigned_chain_id
    if selected_chain_id is not None:
        try:
            return await load_routing_chain(session, selected_chain_id)
        except NotFound:
            return None

    chains = await list_chains(session)
    if not chains:
        return None

    eligible = [
        chain
        for chain in chains
        if any(entry.route_kind is route_kind for entry in chain.entries)
    ]
    matched = next((c for c in eligible if c.model_selector == model), None)
    if matched is None:
        matched = next((c for c in eligible if c.model_selector is None), None)
    if matched is None:
        return None

    return await load_routing_chain(session, matched.chain_id)


async def _snapshot_states(
    session: AsyncSession,
    redis: Redis,
    account_ids: tuple[uuid.UUID, ...],
    *,
    now: datetime,
) -> dict[uuid.UUID, CredentialState]:
    """Build the :class:`CredentialState` snapshot for the chain's entries.

    For each referenced credential the snapshot captures the four facts the routing
    availability predicate consumes: deleted (soft-deleted or missing), enabled (not
    disabled), requires-reauth, and limit-reached. The limit-reached flag is computed
    from the account's configured Usage_Limit and the live consumption counter
    (Requirements 4.2, 11.x). A credential id with no live row is reported as deleted
    so the evaluation layer skips it.
    """
    if not account_ids:
        return {}

    unique_ids = list(dict.fromkeys(account_ids))
    cred_rows = (
        await session.scalars(
            select(UpstreamCredential).where(UpstreamCredential.id.in_(unique_ids))
        )
    ).all()
    creds_by_id = {cred.id: cred for cred in cred_rows}

    limit_rows = (
        await session.scalars(
            select(AccountUsageLimit).where(
                AccountUsageLimit.subject_id.in_(unique_ids)
            )
        )
    ).all()
    specs_by_id = {row.subject_id: _spec_from_account_row(row) for row in limit_rows}

    states: dict[uuid.UUID, CredentialState] = {}
    for account_id in unique_ids:
        cred = creds_by_id.get(account_id)
        if cred is None or cred.deleted_at is not None:
            states[account_id] = CredentialState(deleted=True)
            continue

        spec = specs_by_id.get(account_id)
        reached = False
        if spec is not None:
            reached = await _limit_reached_for(
                redis, SUBJECT_ACCOUNT, account_id, spec, now=now
            )

        states[account_id] = CredentialState(
            deleted=False,
            enabled=cred.status is not CredentialStatus.DISABLED,
            requires_reauth=cred.status is CredentialStatus.REQUIRES_REAUTH,
            limit_reached=reached,
        )
    return states


def _usage_event(
    correlation_id: uuid.UUID,
    token_id: uuid.UUID,
    account_id: uuid.UUID,
    provider: str,
    counts: UsageCounts,
) -> UsageEvent:
    """Build a :class:`UsageEvent`, flagging missing provider metering.

    When the provider reported no token volume at all, the count fields are left
    ``None`` so :func:`record_usage` stores zeros and flags the record
    ``provider_metering_missing`` (Requirement 13.2). Otherwise the reported counts
    are forwarded.
    """
    reported = bool(
        counts.prompt_tokens or counts.completion_tokens or counts.total_tokens
    )
    if not reported:
        return UsageEvent(
            correlation_id=correlation_id,
            client_token_id=token_id,
            account_id=account_id,
            provider=provider,
        )
    return UsageEvent(
        correlation_id=correlation_id,
        client_token_id=token_id,
        account_id=account_id,
        provider=provider,
        prompt_tokens=counts.prompt_tokens,
        completion_tokens=counts.completion_tokens,
        total_tokens=counts.total_tokens,
    )


def _detail_value(details: list[Any], key: str) -> str | None:
    """Return a string value from a structured error detail list, if present."""
    for detail in details:
        if isinstance(detail, Mapping) and key in detail:
            value = detail[key]
            return str(value) if value is not None else None
    return None


def _fallback_exhausted_error(last_error: GozarError | None) -> UpstreamError:
    """Build a terminal fallback error that preserves the useful upstream reason."""
    message = (
        "all fallbacks failed: every available credential errored while serving "
        "the request"
    )
    details = last_error.details if last_error is not None else []
    if last_error is not None:
        message = f"{message}; last error: {last_error.message}"
        upstream_body = _detail_value(last_error.details, "upstream_body")
        if upstream_body is not None:
            message = f"{message}; upstream said: {upstream_body}"
    return UpstreamError(message, details=details)


def _upstream_status(exc: GozarError) -> int | None:
    """Return an upstream HTTP status carried in a provider error, if any."""
    raw = _detail_value(exc.details, "upstream_status")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _is_retryable_target_error(exc: GozarError) -> bool:
    """Return whether another provider may plausibly repair this failed attempt."""

    if not isinstance(exc, UpstreamError):
        return False
    status = _upstream_status(exc)
    # A status-less UpstreamError represents transport exhaustion or a provider
    # stream failure. Both are transient from the routing layer's perspective.
    return status is None or status == 429 or 500 <= status <= 599


def _is_target_auth_error(exc: GozarError) -> bool:
    """Return whether this attempt failed because its upstream credential was rejected."""

    status = _upstream_status(exc)
    if status in (401, 403):
        return True
    return isinstance(exc, NoAvailableAccount) and "reauth" in exc.message.lower()


def _allows_fallback(target: RoutingTarget, exc: GozarError) -> bool:
    """Apply the persisted policy on the failed node's outgoing fallback edge."""

    if target.fallback_policy is FallbackPolicy.ANY_ERROR:
        return True
    retryable = _is_retryable_target_error(exc)
    if target.fallback_policy is FallbackPolicy.RETRYABLE:
        return retryable
    return retryable or _is_target_auth_error(exc)


def _should_refresh_after_upstream_auth_error(
    exc: UpstreamError,
    material: ProviderCredentialMaterial,
) -> bool:
    """True when a rejected upstream call can be repaired by subscription refresh."""
    return (
        material.kind is CredentialKind.SUBSCRIPTION
        and _upstream_status(exc) == 401
    )


async def _default_reactive_refresh(
    session: AsyncSession,
    account_id: uuid.UUID,
    *,
    settings: Settings,
) -> bool:
    """Force-refresh a subscription account after upstream rejects its access token."""
    outcome = await refresh_subscription(session, account_id, settings=settings)
    if outcome.requires_reauth:
        raise NoAvailableAccount(
            f"subscription account {account_id} requires reauthorization after "
            f"upstream rejected its access token"
        )
    return outcome.refreshed


def _finish_reason(response: OpenAIChatResponse) -> str | None:
    """Read the first choice's finish reason for the trace, if any."""
    if response.choices:
        return response.choices[0].finish_reason
    return None


def _request_for_target(
    request: OpenAIChatRequest,
    target: RoutingTarget,
) -> OpenAIChatRequest:
    """Return the request shape for one chain node without mutating the inbound value."""

    if target.model_id is None or target.model_id == request.model:
        return request
    return request.model_copy(update={"model": target.model_id})


async def _credential_trace_snapshot(
    session: AsyncSession, account_id: uuid.UUID
) -> CredentialTraceSnapshot | None:
    """Return non-secret credential facts for trace display, if the row exists."""
    row = await session.get(UpstreamCredential, account_id)
    if row is None:
        return None
    status = "deleted" if row.deleted_at is not None else row.status.value
    return CredentialTraceSnapshot(
        account_id=account_id,
        label=row.label,
        provider=row.provider,
        kind=row.kind.value,
        status=status,
    )


async def _credential_trace_snapshots(
    session: AsyncSession, targets: list[RoutingTarget]
) -> dict[uuid.UUID, CredentialTraceSnapshot]:
    """Load non-secret credential display facts for all attempted nodes at once."""

    account_ids = list(dict.fromkeys(target.account_id for target in targets))
    if not account_ids:
        return {}
    rows = (
        await session.scalars(
            select(UpstreamCredential).where(UpstreamCredential.id.in_(account_ids))
        )
    ).all()
    snapshots: dict[uuid.UUID, CredentialTraceSnapshot] = {}
    for row in rows:
        status = "deleted" if row.deleted_at is not None else row.status.value
        snapshots[row.id] = CredentialTraceSnapshot(
            account_id=row.id,
            label=row.label,
            provider=row.provider,
            kind=row.kind.value,
            status=status,
        )
    return snapshots


def _attempt_base(
    target: RoutingTarget,
    snapshot: CredentialTraceSnapshot | None,
    *,
    model: str,
    duration_ms: float,
) -> dict[str, Any]:
    """Build the common, non-secret fields for one chain-node attempt."""

    attempt: dict[str, Any] = {
        "account_id": str(target.account_id),
        "model": model,
        "duration_ms": round(duration_ms, 3),
    }
    if target.node_id is not None:
        attempt["node_id"] = str(target.node_id)
    if target.position is not None:
        attempt["position"] = target.position
    if snapshot is not None:
        attempt["provider"] = snapshot.provider
        attempt["credential"] = snapshot.as_meta()
    return attempt


def _attempt_error_category(exc: GozarError) -> str:
    """Map a typed failure to a stable routing-attempt category."""

    status = _upstream_status(exc)
    if status in (401, 403) or _is_target_auth_error(exc):
        return "authentication"
    if status == 429 or isinstance(exc, RateLimitError):
        return "rate_limit"
    if isinstance(exc, UpstreamError) and status is None:
        return "transport"
    if isinstance(exc, NoAvailableAccount):
        return "unavailable"
    return "upstream"


def _record_failed_attempt(
    trace: _PipelineTrace,
    target: RoutingTarget,
    snapshot: CredentialTraceSnapshot | None,
    exc: GozarError,
    *,
    model: str,
    duration_ms: float,
    fallback_taken: bool,
) -> None:
    """Append a sanitized failed-node record to the request trace."""

    attempt = _attempt_base(
        target, snapshot, model=model, duration_ms=duration_ms
    )
    status = _upstream_status(exc)
    attempt.update(
        {
            "outcome": "error",
            "fallback_taken": fallback_taken,
            "error": {
                "category": _attempt_error_category(exc),
                "code": exc.code,
                "type": exc.openai_type,
                "message": exc.message[:500],
                "retryable": _is_retryable_target_error(exc),
                **({"upstream_status": status} if status is not None else {}),
            },
        }
    )
    trace.attempts.append(attempt)


def _record_successful_attempt(
    trace: _PipelineTrace,
    target: RoutingTarget,
    snapshot: CredentialTraceSnapshot | None,
    *,
    provider: str,
    model: str,
    duration_ms: float,
    refreshed: bool,
    usage: UsageCounts | None = None,
) -> None:
    """Append the selected successful node and its normalized usage counts."""

    attempt = _attempt_base(
        target, snapshot, model=model, duration_ms=duration_ms
    )
    attempt.update(
        {
            "provider": provider,
            "outcome": "success",
            "fallback_taken": False,
            "credential_refreshed": refreshed,
        }
    )
    if usage is not None:
        attempt["usage"] = {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
        }
    trace.attempts.append(attempt)
    trace.selected_node_id = target.node_id
    trace.selected_position = target.position


def _update_selected_stream_attempt(
    trace: _PipelineTrace,
    *,
    usage: UsageCounts | None = None,
    error: GozarError | None = None,
) -> None:
    """Finalize the already-selected stream node after the stream ends or fails."""

    if not trace.attempts:
        return
    attempt = trace.attempts[-1]
    if usage is not None:
        attempt["usage"] = {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
        }
    if error is not None:
        status = _upstream_status(error)
        attempt["outcome"] = "stream_error"
        attempt["error"] = {
            "category": _attempt_error_category(error),
            "code": error.code,
            "type": error.openai_type,
            "message": error.message[:500],
            "retryable": False,
            **({"upstream_status": status} if status is not None else {}),
        }


async def complete_chat_completion(
    session: AsyncSession,
    *,
    presented_token: str | None,
    request: OpenAIChatRequest,
    session_id: str | None = None,
    chain_override_id: uuid.UUID | None = None,
    trusted_token_id: uuid.UUID | None = None,
    redis: Redis | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
    upstream: UpstreamCaller | None = None,
    acquire_material: MaterialAcquirer | None = None,
    refresh_on_auth_error: ReactiveRefresher | None = None,
    correlation_id: uuid.UUID | None = None,
) -> OpenAIChatResponse:
    """Run the non-streaming ``/v1/chat/completions`` pipeline and return the response.

    See the module docstring for the full step sequence. Raises the typed terminal
    errors the gateway renders as OpenAI-compatible error JSON:

    * :class:`AuthError` (401) -- missing/invalid Client_Token; no upstream call.
    * :class:`RateLimitError` (429) -- the token reached its usage limit.
    * :class:`NoAvailableAccount` (503) -- no credential available to attempt.
    * :class:`UpstreamError` (502) -- every available credential failed.

    Args:
        session: Async DB session (committed by the caller / request dependency).
        presented_token: The raw Client_Token string from the ``Authorization``
            header, or ``None`` when absent.
        request: The parsed inbound OpenAI Chat Completions request.
        session_id: Optional session-affinity id (``x-gozar-session`` header).
        redis: Optional Redis client; defaults to the process-wide client.
        settings: Optional settings; defaults to the process settings.
        now: Optional reference time (counters/trace); defaults to current UTC.
        upstream: Optional injectable upstream caller; defaults to the real client.
        acquire_material: Optional injectable credential acquirer; defaults to
            :func:`get_usable_token`.

    Returns:
        The translated, OpenAI-shaped :class:`OpenAIChatResponse`.
    """
    settings = settings or get_settings()
    redis = redis or get_redis()
    now = now or _utcnow()
    upstream = upstream or (
        lambda entry, material, adapter, body: call_upstream(
            entry, material, adapter, body, settings=settings
        )
    )
    acquire = acquire_material or (
        lambda sess, account_id: get_usable_token(
            sess, account_id, settings=settings, now=now
        )
    )
    reactive_refresh = refresh_on_auth_error or (
        lambda sess, account_id: _default_reactive_refresh(
            sess, account_id, settings=settings
        )
    )

    correlation_id = correlation_id or uuid.uuid4()
    trace_context = _PipelineTrace()
    await open_trace(
        session,
        correlation_id,
        InboundMeta(
            method="POST",
            endpoint="chat.completions",
            model=request.model,
            stream=request.stream,
            session_id=session_id,
            chain_id=chain_override_id,
        ),
        now=now,
    )

    try:
        return await _run_pipeline(
            session,
            redis=redis,
            settings=settings,
            now=now,
            correlation_id=correlation_id,
            presented_token=presented_token,
            request=request,
            session_id=session_id,
            chain_override_id=chain_override_id,
            trusted_token_id=trusted_token_id,
            upstream=upstream,
            acquire=acquire,
            reactive_refresh=reactive_refresh,
            trace_context=trace_context,
        )
    except GozarError as exc:
        # Terminal error: finalize the trace with the matching outcome, then re-raise
        # so the router renders the OpenAI-compatible error envelope.
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


def _outcome_for_error(exc: GozarError) -> str:
    """Map a terminal error to a Trace_Log outcome (see ``TRACE_OUTCOMES``)."""
    if isinstance(exc, NoAvailableAccount):
        return "no_account"
    if isinstance(exc, UpstreamError):
        return "all_fallbacks_failed"
    return "client_error"


async def _authenticate_and_route(
    session: AsyncSession,
    *,
    redis: Redis,
    settings: Settings,
    now: datetime,
    presented_token: str | None,
    request: ModelRequest,
    session_id: str | None,
    chain_override_id: uuid.UUID | None = None,
    trusted_token_id: uuid.UUID | None = None,
    correlation_id: uuid.UUID,
    trace_context: _PipelineTrace,
    route_kind: RouteKind = RouteKind.CHAT,
) -> tuple[uuid.UUID, list[RoutingTarget]]:
    """Run the pre-upstream half of the hot path shared by both response modes.

    Performs steps 1-4 of the pipeline -- authenticate the Client_Token, enforce its
    status/limit, select the Fallback_Chain for the model, and compute the
    session-aware attempt order -- and returns ``(token_id, attempt_order)``. Raises
    the typed terminal errors (:class:`AuthError`, :class:`RateLimitError`,
    :class:`NoAvailableAccount`) before any upstream call is made, so both the
    non-streaming and streaming paths reject identically (Requirements 6.2, 9.2, 6.4).
    """
    # 1. Authenticate the Client_Token. Invalid/missing -> 401 and NO upstream call.
    if trusted_token_id is not None:
        token = await session.get(ClientToken, trusted_token_id)
        if token is None or token.status != TokenStatus.ACTIVE.value:
            raise AuthError("the selected Gozar API key is invalid or not active")
        token_id = token.id
        assigned_chain_id = token.assigned_chain_id
    else:
        if not presented_token:
            raise AuthError("a valid Client_Token is required")
        auth = await verify(session, presented_token, settings=settings)
        if auth is None:
            raise AuthError("the presented Client_Token is invalid or not active")
        token_id = auth.token_id
        assigned_chain_id = auth.assigned_chain_id

    # 2. Enforce token enabled/under-limit (status already checked by verify).
    await _enforce_token_limit(session, redis, token_id, now=now)

    # 3-4. Resolve the chain from the token assignment or model selector, then
    # compute the available attempt order with session affinity (Requirement 12.2).
    chain = await _select_chain(
        session,
        request.model,
        route_kind=route_kind,
        assigned_chain_id=assigned_chain_id,
        override_chain_id=chain_override_id,
    )
    if chain is None:
        raise NoAvailableAccount(
            "no available account: no fallback chain is configured for the "
            "requested model"
        )

    trace_context.chain_id = chain.chain_id
    trace_context.route_kind = route_kind
    await set_trace_chain(session, correlation_id, chain.chain_id)

    chain = chain.for_route(route_kind)
    if not chain.entries:
        if route_kind is RouteKind.EMBEDDINGS:
            raise NoAvailableAccount(
                "no available embeddings account: the selected chain has no "
                "embeddings nodes; add an active OpenAI or OpenRouter API-key "
                "account to its Embeddings route"
            )
        raise NoAvailableAccount(
            "no available chat route: the selected chain has no LLM nodes"
        )

    states = await _snapshot_states(session, redis, chain.account_ids, now=now)
    attempt_order = await get_attempt_order(
        chain,
        states,
        session_id,
        redis=redis,
        route_kind=route_kind,
    )
    if not attempt_order:
        raise NoAvailableAccount(
            "no available account: every credential in the fallback chain is "
            "disabled, deleted, over its limit, or requires reauthorization"
        )

    return token_id, list(attempt_order)


async def _run_pipeline(
    session: AsyncSession,
    *,
    redis: Redis,
    settings: Settings,
    now: datetime,
    correlation_id: uuid.UUID,
    presented_token: str | None,
    request: OpenAIChatRequest,
    session_id: str | None,
    chain_override_id: uuid.UUID | None,
    trusted_token_id: uuid.UUID | None,
    upstream: UpstreamCaller,
    acquire: MaterialAcquirer,
    reactive_refresh: ReactiveRefresher,
    trace_context: _PipelineTrace,
) -> OpenAIChatResponse:
    """Execute the authenticated hot path; see :func:`complete_chat_completion`."""
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
    )

    # 5. Translate -> call -> fall back across the available credentials.
    last_error: GozarError | None = None
    snapshots = await _credential_trace_snapshots(session, attempt_order)
    for index, target in enumerate(attempt_order):
        account_id = target.account_id
        effective_request = _request_for_target(request, target)
        started = perf_counter()
        try:
            material, adapter, raw, refreshed = await _call_with_reactive_refresh(
                session,
                account_id,
                settings=settings,
                request=effective_request,
                acquire=acquire,
                upstream=upstream,
                reactive_refresh=reactive_refresh,
            )
        except GozarError as exc:
            # This credential could not serve the request (reauth needed, provider
            # misconfigured, or upstream failure); advance to the next entry.
            last_error = exc
            fallback_allowed = _allows_fallback(target, exc)
            fallback_taken = fallback_allowed and index < len(attempt_order) - 1
            _record_failed_attempt(
                trace_context,
                target,
                snapshots.get(account_id),
                exc,
                model=effective_request.model,
                duration_ms=(perf_counter() - started) * 1000,
                fallback_taken=fallback_taken,
            )
            if not fallback_allowed:
                break
            continue

        # Success: translate the response and record the outcome.
        response = adapter.from_provider_response(raw)
        counts = adapter.extract_usage(raw)
        _record_successful_attempt(
            trace_context,
            target,
            snapshots.get(account_id),
            provider=material.provider,
            model=effective_request.model,
            duration_ms=(perf_counter() - started) * 1000,
            refreshed=refreshed,
            usage=counts,
        )

        await record_usage(
            session,
            _usage_event(
                correlation_id, token_id, account_id, material.provider, counts
            ),
            redis=redis,
            now=now,
        )
        if session_id:
            await record_session_binding(
                session_id,
                account_id,
                redis=redis,
                settings=settings,
                route_kind=RouteKind.CHAT,
            )
        await finalize_trace(
            session,
            correlation_id,
            OutboundMeta(
                outcome="success",
                status_code=200,
                account_id=account_id,
                credential=snapshots.get(account_id),
                finish_reason=_finish_reason(response),
                model=effective_request.model,
                routing=trace_context.routing_meta(),
            ),
            now=_utcnow(),
        )
        return response

    # 6. Every available credential failed -> "all fallbacks failed" (Req 10.3).
    raise _fallback_exhausted_error(last_error)


async def _call_with_reactive_refresh(
    session: AsyncSession,
    account_id: uuid.UUID,
    *,
    settings: Settings,
    request: OpenAIChatRequest,
    acquire: MaterialAcquirer,
    upstream: UpstreamCaller,
    reactive_refresh: ReactiveRefresher,
) -> tuple[ProviderCredentialMaterial, Any, dict[str, Any], bool]:
    """Call one credential, forcing a one-shot subscription refresh on upstream 401."""
    material = await acquire(session, account_id)
    entry = get_provider(material.provider, settings=settings)
    adapter = entry.adapter
    provider_body = adapter.to_provider_request(request)
    try:
        raw = await upstream(entry, material, adapter, provider_body)
    except UpstreamError as exc:
        if not _should_refresh_after_upstream_auth_error(exc, material):
            raise
        refreshed = await reactive_refresh(session, account_id)
        if not refreshed:
            raise

        material = await acquire(session, account_id)
        entry = get_provider(material.provider, settings=settings)
        adapter = entry.adapter
        provider_body = adapter.to_provider_request(request)
        raw = await upstream(entry, material, adapter, provider_body)
        return material, adapter, raw, True
    return material, adapter, raw, False


async def _first_chunk(byte_iter: AsyncIterator[bytes]) -> bytes | object:
    """Pull the first byte chunk to force upstream stream establishment.

    Returns the first chunk, or :data:`_STREAM_EMPTY` when the stream established but
    produced no bytes. Any :class:`GozarError` raised here (a non-retryable upstream
    status or exhausted transport retries before a byte was forwarded) propagates to
    the caller, which treats it as a failed attempt and falls back.
    """
    try:
        return await byte_iter.__anext__()
    except StopAsyncIteration:
        return _STREAM_EMPTY


async def _rechain(
    first: bytes | object, byte_iter: AsyncIterator[bytes]
) -> AsyncIterator[bytes]:
    """Re-emit the peeked first chunk, then the remainder of the upstream stream."""
    if first is not _STREAM_EMPTY:
        yield first  # type: ignore[misc]
    async for chunk in byte_iter:
        yield chunk


class _EstablishedStream:
    """A successfully opened upstream stream plus the metadata to translate/record it."""

    __slots__ = (
        "account_id",
        "provider",
        "model",
        "adapter",
        "byte_stream",
        "refreshed",
    )

    def __init__(
        self,
        account_id: uuid.UUID,
        provider: str,
        model: str,
        adapter: Any,
        byte_stream: AsyncIterator[bytes],
        refreshed: bool = False,
    ) -> None:
        self.account_id = account_id
        self.provider = provider
        self.model = model
        self.adapter = adapter
        self.byte_stream = byte_stream
        self.refreshed = refreshed


async def _establish_stream(
    session: AsyncSession,
    *,
    settings: Settings,
    request: OpenAIChatRequest,
    attempt_order: list[RoutingTarget],
    acquire: MaterialAcquirer,
    stream_upstream: StreamingUpstreamCaller,
    reactive_refresh: ReactiveRefresher,
    trace_context: _PipelineTrace,
) -> _EstablishedStream:
    """Open the upstream stream, falling back across credentials on failures.

    Mirrors the non-streaming fallback loop, but the "did this credential work?"
    decision is made at stream-establishment time: a credential whose upstream call
    fails to establish (acquisition/translation error, non-retryable status, or
    exhausted transport retries before any byte) is skipped in favor of the next
    available entry (Requirements 10.2, 11.x). Once a stream yields its first byte it
    is committed -- a mid-stream failure is never replayed against another credential.

    Raises :class:`UpstreamError` ("all fallbacks failed") when every available
    credential fails to establish (Requirement 10.3).
    """
    last_error: GozarError | None = None
    snapshots = await _credential_trace_snapshots(session, attempt_order)
    for index, target in enumerate(attempt_order):
        account_id = target.account_id
        effective_request = _request_for_target(request, target)
        started = perf_counter()
        try:
            established = await _establish_stream_with_reactive_refresh(
                session,
                account_id,
                settings=settings,
                request=effective_request,
                acquire=acquire,
                stream_upstream=stream_upstream,
                reactive_refresh=reactive_refresh,
            )
        except GozarError as exc:
            last_error = exc
            fallback_allowed = _allows_fallback(target, exc)
            fallback_taken = fallback_allowed and index < len(attempt_order) - 1
            _record_failed_attempt(
                trace_context,
                target,
                snapshots.get(account_id),
                exc,
                model=effective_request.model,
                duration_ms=(perf_counter() - started) * 1000,
                fallback_taken=fallback_taken,
            )
            if not fallback_allowed:
                break
            continue
        _record_successful_attempt(
            trace_context,
            target,
            snapshots.get(account_id),
            provider=established.provider,
            model=effective_request.model,
            duration_ms=(perf_counter() - started) * 1000,
            refreshed=established.refreshed,
        )
        return established

    raise _fallback_exhausted_error(last_error)


async def _establish_stream_once(
    account_id: uuid.UUID,
    *,
    settings: Settings,
    request: OpenAIChatRequest,
    material: ProviderCredentialMaterial,
    stream_upstream: StreamingUpstreamCaller,
    refreshed: bool = False,
) -> _EstablishedStream:
    """Open one upstream stream and pull the first chunk to prove establishment."""
    entry = get_provider(material.provider, settings=settings)
    adapter = entry.adapter
    provider_body = adapter.to_provider_request(request)
    byte_iter = stream_upstream(entry, material, adapter, provider_body).__aiter__()
    first = await _first_chunk(byte_iter)
    return _EstablishedStream(
        account_id=account_id,
        provider=material.provider,
        model=request.model,
        adapter=adapter,
        byte_stream=_rechain(first, byte_iter),
        refreshed=refreshed,
    )


async def _establish_stream_with_reactive_refresh(
    session: AsyncSession,
    account_id: uuid.UUID,
    *,
    settings: Settings,
    request: OpenAIChatRequest,
    acquire: MaterialAcquirer,
    stream_upstream: StreamingUpstreamCaller,
    reactive_refresh: ReactiveRefresher,
) -> _EstablishedStream:
    """Establish one stream, forcing a one-shot subscription refresh on 401."""
    material = await acquire(session, account_id)
    try:
        return await _establish_stream_once(
            account_id,
            settings=settings,
            request=request,
            material=material,
            stream_upstream=stream_upstream,
            refreshed=True,
        )
    except UpstreamError as exc:
        if not _should_refresh_after_upstream_auth_error(exc, material):
            raise
        refreshed = await reactive_refresh(session, account_id)
        if not refreshed:
            raise
        material = await acquire(session, account_id)
        return await _establish_stream_once(
            account_id,
            settings=settings,
            request=request,
            material=material,
            stream_upstream=stream_upstream,
        )


async def _sse_response_stream(
    session: AsyncSession,
    *,
    redis: Redis,
    settings: Settings,
    now: datetime,
    correlation_id: uuid.UUID,
    token_id: uuid.UUID,
    established: _EstablishedStream,
    session_id: str | None,
    trace_context: _PipelineTrace,
) -> AsyncIterator[str]:
    """Translate the upstream stream to OpenAI SSE and record usage/trace at the end.

    Parses the upstream SSE byte stream, translates each provider event to an OpenAI
    chunk via the adapter (dropping events that carry no client-facing content),
    frames each as ``data: <json>`` and terminates with ``data: [DONE]`` (Requirement
    6.3). When the stream completes, records the usage reported on the stream (zeros
    flagged as missing provider metering when absent, mirroring the non-streaming
    path) and finalizes the Trace_Log (Requirements 13.1, 14.2). A mid-stream failure
    finalizes the trace as a failed outcome before propagating.
    """
    adapter = established.adapter
    last_usage: UsageCounts | None = None
    finish_reason: str | None = None
    try:
        async for data in iter_sse_data(established.byte_stream):
            if is_done(data):
                # Upstream's own terminator: stop translating; we emit our own below.
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                # Not a JSON event (e.g. a provider keep-alive payload); skip it.
                continue
            chunk = adapter.from_provider_stream_chunk(event)
            if chunk is None:
                # Event carried no client-facing content (reasoning, ping, etc.).
                continue
            if chunk.usage is not None:
                last_usage = chunk.usage
            if chunk.choices and chunk.choices[0].finish_reason is not None:
                finish_reason = chunk.choices[0].finish_reason
            yield format_sse_chunk(chunk)

        yield SSE_DONE
    except GozarError as exc:
        _update_selected_stream_attempt(trace_context, error=exc)
        await finalize_trace(
            session,
            correlation_id,
            OutboundMeta(
                outcome=_outcome_for_error(exc),
                status_code=exc.status_code,
                account_id=established.account_id,
                credential=await _credential_trace_snapshot(
                    session, established.account_id
                ),
                model=established.model,
                routing=trace_context.routing_meta(),
            ),
            now=_utcnow(),
        )
        raise

    # Success: meter the reported usage and finalize the trace.
    counts = last_usage or UsageCounts()
    _update_selected_stream_attempt(trace_context, usage=counts)
    await record_usage(
        session,
        _usage_event(
            correlation_id, token_id, established.account_id, established.provider, counts
        ),
        redis=redis,
        now=now,
    )
    if session_id:
        await record_session_binding(
            session_id, established.account_id, redis=redis, settings=settings
        )
    await finalize_trace(
        session,
        correlation_id,
        OutboundMeta(
            outcome="success",
            status_code=200,
            account_id=established.account_id,
            credential=await _credential_trace_snapshot(
                session, established.account_id
            ),
            finish_reason=finish_reason,
            model=established.model,
            routing=trace_context.routing_meta(),
        ),
        now=_utcnow(),
    )


async def stream_chat_completion(
    session: AsyncSession,
    *,
    presented_token: str | None,
    request: OpenAIChatRequest,
    session_id: str | None = None,
    chain_override_id: uuid.UUID | None = None,
    trusted_token_id: uuid.UUID | None = None,
    redis: Redis | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
    stream_upstream: StreamingUpstreamCaller | None = None,
    acquire_material: MaterialAcquirer | None = None,
    refresh_on_auth_error: ReactiveRefresher | None = None,
    correlation_id: uuid.UUID | None = None,
) -> AsyncIterator[str]:
    """Run the streaming ``/v1/chat/completions`` pipeline; return an OpenAI SSE stream.

    The streaming sibling of :func:`complete_chat_completion`. It shares the same
    authenticate -> enforce-limit -> route -> fall-back logic, then pipes the selected
    Provider's streamed response through the Translation_Layer and re-frames it as an
    OpenAI Server-Sent Events stream (Requirement 6.3).

    Pre-stream terminal errors are raised synchronously (before the returned iterator
    is consumed) so the router can render an OpenAI-compatible error envelope:

    * :class:`AuthError` (401) -- missing/invalid Client_Token; no upstream call.
    * :class:`RateLimitError` (429) -- the token reached its usage limit.
    * :class:`NoAvailableAccount` (503) -- no credential available to attempt.
    * :class:`UpstreamError` (502) -- every available credential failed to establish.

    The Trace_Log is opened here and finalized inside the returned iterator once the
    stream completes (or fails mid-stream), and usage is recorded at end-of-stream, so
    streaming requests are metered and traced just like non-streaming ones
    (Requirements 13.1, 14.1, 14.2).

    Returns:
        An async iterator of SSE event strings (``data: <json>\\n\\n`` ... terminated
        by ``data: [DONE]\\n\\n``) suitable for a FastAPI ``StreamingResponse``.
    """
    settings = settings or get_settings()
    redis = redis or get_redis()
    now = now or _utcnow()
    stream_upstream = stream_upstream or (
        lambda entry, material, adapter, body: call_upstream_stream(
            entry, material, adapter, body, settings=settings
        )
    )
    acquire = acquire_material or (
        lambda sess, account_id: get_usable_token(
            sess, account_id, settings=settings, now=now
        )
    )
    reactive_refresh = refresh_on_auth_error or (
        lambda sess, account_id: _default_reactive_refresh(
            sess, account_id, settings=settings
        )
    )

    correlation_id = correlation_id or uuid.uuid4()
    trace_context = _PipelineTrace()
    await open_trace(
        session,
        correlation_id,
        InboundMeta(
            method="POST",
            endpoint="chat.completions",
            model=request.model,
            stream=True,
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
        )
        established = await _establish_stream(
            session,
            settings=settings,
            request=request,
            attempt_order=attempt_order,
            acquire=acquire,
            stream_upstream=stream_upstream,
            reactive_refresh=reactive_refresh,
            trace_context=trace_context,
        )
    except GozarError as exc:
        # Terminal before any byte streamed: finalize the trace and re-raise so the
        # router renders the OpenAI-compatible error envelope (no SSE response).
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

    return _sse_response_stream(
        session,
        redis=redis,
        settings=settings,
        now=now,
        correlation_id=correlation_id,
        token_id=token_id,
        established=established,
        session_id=session_id,
        trace_context=trace_context,
    )


__all__ = [
    "MaterialAcquirer",
    "ReactiveRefresher",
    "StreamingUpstreamCaller",
    "UpstreamCaller",
    "complete_chat_completion",
    "stream_chat_completion",
]
