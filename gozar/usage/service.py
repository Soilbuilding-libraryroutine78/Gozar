"""Usage_Recorder service: metering and atomic consumption counters.

This module implements the metering half of the Usage_Recorder described in the
design (Requirements 13, 16.4). It records exactly one durable
:class:`~gozar.usage.models.UsageRecord` row per completed proxied request and
atomically increments the Redis consumption counters that limit evaluation reads.

Responsibilities
----------------
* :func:`record_usage` -- persist one ``usage_record`` row (correlation id, the
  Client_Token used, the Upstream_Credential used, the Provider, the
  provider-reported token counts, and the request timestamp) and then atomically
  increment the consumption counters for both the token and the credential
  (Requirements 13.1, 13.3). When the Provider reports no token counts the row is
  stored with zero counts and flagged ``provider_metering_missing`` (Requirement
  13.2).
* :func:`read_counter` -- read a subject's accumulated consumption for a metric and
  window; the natural counterpart of the increment used by limit evaluation and the
  token/account usage listings.

Counter keying
--------------
Counters live in Redis for atomic increments while Postgres ``usage_record`` remains
the durable, rebuildable source of truth. Keys follow the design's scheme::

    usage:{subject_kind}:{subject_id}:{metric}:{window_bucket}

where ``subject_kind`` is ``account`` or ``token``, ``metric`` is one of the tracked
:class:`~gozar.usage.limits.LimitMetric` values, and ``window_bucket`` identifies the
measurement window's current period (see :func:`window_bucket`). A single request is
metered into every window so that whichever window a Usage_Limit is configured for
has a live counter to read; the ``window`` enum value selects which key the limit
evaluation consults.

Security
--------
No secret material (subscription tokens, API keys, client-token secrets) is ever
persisted, logged, or placed into a counter key here -- only opaque UUID identifiers
and aggregate counts (Requirement 16.4).

Extensibility
-------------
This module covers metering (above) and request tracing (the clearly delimited
section at the end of the file): :func:`open_trace` / :func:`finalize_trace` and
their :class:`InboundMeta` / :class:`OutboundMeta` value objects (Requirements 14,
16.4).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gozar.core.errors import NotFound, ValidationError
from gozar.core.redis import get_redis
from gozar.usage.limits import LimitMetric, LimitWindow, UsageLimitSpec
from gozar.usage.models import TraceLog, UsageRecord

# ---------------------------------------------------------------------------
# Consumption-counter keying
# ---------------------------------------------------------------------------

# Redis key namespace for the consumption counters.
_COUNTER_PREFIX = "usage:"

# Subject kinds a Usage_Limit may attach to (mirrors the ``subject_kind`` column).
SUBJECT_ACCOUNT = "account"
SUBJECT_TOKEN = "token"

# Metrics that are directly metered from a completed request. ``cost_estimate`` and
# ``percentage`` are derived/relative metrics and are not incremented here.
_TRACKED_METRICS: tuple[LimitMetric, ...] = (
    LimitMetric.REQUEST_COUNT,
    LimitMetric.TOKEN_COUNT,
)

# Number of trailing hourly buckets that make up the rolling 24h window.
_ROLLING_24H_HOURS = 24

# Cleanup TTLs (seconds) so windowed counters self-expire well after their period
# closes. These are operational safety margins for Redis housekeeping, not business
# limits: Postgres ``usage_record`` remains the durable source of truth and counters
# are rebuildable from it. ``NONE`` is cumulative and never expires.
_WINDOW_TTL_SECONDS: dict[LimitWindow, int | None] = {
    LimitWindow.NONE: None,
    LimitWindow.DAILY: 2 * 24 * 60 * 60,  # keep a day's counter ~2 days
    LimitWindow.MONTHLY: 40 * 24 * 60 * 60,  # keep a month's counter ~40 days
    LimitWindow.ROLLING_24H: 25 * 60 * 60,  # keep each hourly bucket ~25 hours
}


def _utcnow() -> datetime:
    """Return the current UTC time (isolated for deterministic testing)."""
    return datetime.now(timezone.utc)


def _as_utc(moment: datetime) -> datetime:
    """Normalise a datetime to UTC, treating a naive value as already-UTC.

    All timestamps in this module are written in UTC; this guards the duration
    subtraction in :func:`trace_elapsed` against naive values read back from stores
    that do not preserve timezone information.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def window_bucket(window: LimitWindow, now: datetime) -> str:
    """Return the bucket label identifying ``window``'s current period at ``now``.

    The bucket is the volatile part of a counter key; a new period produces a new
    bucket, which is how windowed limits reset (Requirement 4.2) without mutating any
    stored consumption. Buckets are computed in UTC:

    * ``NONE`` -- a single fixed ``"all"`` bucket (cumulative, never resets).
    * ``DAILY`` -- ``YYYYMMDD``.
    * ``MONTHLY`` -- ``YYYYMM``.
    * ``ROLLING_24H`` -- ``YYYYMMDDHH`` (hourly granularity); a rolling-24h
      consumption is the sum of the trailing 24 hourly buckets (see
      :func:`read_counter`).
    """
    moment = now.astimezone(timezone.utc)
    if window is LimitWindow.NONE:
        return "all"
    if window is LimitWindow.DAILY:
        return moment.strftime("%Y%m%d")
    if window is LimitWindow.MONTHLY:
        return moment.strftime("%Y%m")
    # ROLLING_24H
    return moment.strftime("%Y%m%d%H")


def counter_key(
    subject_kind: str,
    subject_id: uuid.UUID,
    metric: LimitMetric,
    window: LimitWindow,
    *,
    now: datetime | None = None,
) -> str:
    """Build the Redis key ``usage:{subject_kind}:{subject_id}:{metric}:{bucket}``.

    ``now`` defaults to the current UTC time and selects the ``window``'s bucket.
    Only opaque identifiers appear in the key -- never secret material
    (Requirement 16.4).
    """
    moment = now or _utcnow()
    bucket = window_bucket(window, moment)
    return f"{_COUNTER_PREFIX}{subject_kind}:{subject_id}:{metric.value}:{bucket}"


# ---------------------------------------------------------------------------
# Usage recording
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageEvent:
    """A completed request to be metered (Requirement 13.1).

    Carries only non-secret identifiers and the provider-reported token counts. The
    token-count fields are optional: leave them all unset (``None``) when the
    Provider reported no usage, and :func:`record_usage` will store zero counts and
    flag the record ``provider_metering_missing`` (Requirement 13.2).

    Attributes
    ----------
    correlation_id:
        Links the metering row to its Trace_Log entry (Requirement 14.1).
    client_token_id:
        The Client_Token that authorized the request.
    account_id:
        The Upstream_Credential actually used to serve the request.
    provider:
        The Provider id the credential belongs to.
    prompt_tokens / completion_tokens / total_tokens:
        Provider-reported token counts, or ``None`` when not reported. When
        ``total_tokens`` is ``None`` but a prompt/completion count is present, the
        total is derived as their sum.
    """

    correlation_id: uuid.UUID
    client_token_id: uuid.UUID
    account_id: uuid.UUID
    provider: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class _ResolvedCounts:
    """Token counts after applying the missing-metering rule (Requirement 13.2)."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    metering_missing: bool


def _resolve_counts(event: UsageEvent) -> _ResolvedCounts:
    """Apply Requirement 13.2: normalise counts and detect missing metering.

    Metering is considered missing only when the Provider reported no counts at all
    (every field ``None``); in that case all counts are stored as zero. Otherwise the
    reported values are used, any unset field defaults to zero, and a missing total is
    derived from the prompt/completion sum. Negative counts are rejected as invalid.
    """
    missing = (
        event.prompt_tokens is None
        and event.completion_tokens is None
        and event.total_tokens is None
    )
    if missing:
        return _ResolvedCounts(0, 0, 0, True)

    prompt = event.prompt_tokens or 0
    completion = event.completion_tokens or 0
    total = event.total_tokens if event.total_tokens is not None else prompt + completion

    if prompt < 0 or completion < 0 or total < 0:
        raise ValidationError("token counts must be non-negative")

    return _ResolvedCounts(prompt, completion, total, False)


async def _increment_counters(
    redis: Redis,
    *,
    account_id: uuid.UUID,
    client_token_id: uuid.UUID,
    total_tokens: int,
    now: datetime,
) -> None:
    """Atomically bump every consumption counter for this request (Req 13.3).

    For both subjects (the credential and the token) and every measurement window,
    increment the ``request_count`` counter by one and the ``token_count`` counter by
    the request's total tokens. All increments for the call run in a single Redis
    transaction so the counters advance together; each windowed key is given a
    cleanup TTL so stale periods expire on their own.
    """
    increments: dict[LimitMetric, int] = {
        LimitMetric.REQUEST_COUNT: 1,
        LimitMetric.TOKEN_COUNT: total_tokens,
    }
    subjects = (
        (SUBJECT_ACCOUNT, account_id),
        (SUBJECT_TOKEN, client_token_id),
    )

    pipe = redis.pipeline(transaction=True)
    for subject_kind, subject_id in subjects:
        for window in LimitWindow:
            ttl = _WINDOW_TTL_SECONDS[window]
            for metric in _TRACKED_METRICS:
                key = counter_key(
                    subject_kind, subject_id, metric, window, now=now
                )
                pipe.incrby(key, increments[metric])
                if ttl is not None:
                    pipe.expire(key, ttl)
    await pipe.execute()


async def record_usage(
    session: AsyncSession,
    event: UsageEvent,
    *,
    redis: Redis | None = None,
    now: datetime | None = None,
) -> UsageRecord:
    """Record one completed request and advance its consumption counters.

    Persists a single :class:`~gozar.usage.models.UsageRecord` row capturing the
    Client_Token, the Upstream_Credential, the Provider, the provider-reported token
    counts, and the request timestamp (Requirement 13.1). When the Provider reported
    no token counts the row is stored with zero counts and
    ``provider_metering_missing=True`` (Requirement 13.2). After the row is flushed,
    the Redis consumption counters for both the token and the credential are
    incremented atomically (Requirement 13.3). No secret material is stored, logged,
    or placed in a counter key (Requirement 16.4).

    Args:
        session: The async DB session to persist the row on (committed by the caller
            / request dependency).
        event: The completed request to meter.
        redis: Optional Redis client; defaults to the process-wide client. Injected
            in tests.
        now: Optional timestamp used for the counter window buckets; defaults to the
            current UTC time. The row's ``created_at`` is set from it too so the
            durable record and the counters share one instant.

    Returns:
        The persisted :class:`~gozar.usage.models.UsageRecord` (flushed; id assigned).
    """
    moment = (now or _utcnow()).astimezone(timezone.utc)
    counts = _resolve_counts(event)

    record = UsageRecord(
        correlation_id=event.correlation_id,
        client_token_id=event.client_token_id,
        account_id=event.account_id,
        provider=event.provider,
        prompt_tokens=counts.prompt_tokens,
        completion_tokens=counts.completion_tokens,
        total_tokens=counts.total_tokens,
        provider_metering_missing=counts.metering_missing,
        created_at=moment,
    )
    session.add(record)
    # Flush so the row (and its generated id) is persisted before we touch Redis;
    # Postgres is the durable source of truth from which counters are rebuildable.
    await session.flush()

    client = redis or get_redis()
    await _increment_counters(
        client,
        account_id=event.account_id,
        client_token_id=event.client_token_id,
        total_tokens=counts.total_tokens,
        now=moment,
    )

    return record


async def read_counter(
    redis: Redis,
    subject_kind: str,
    subject_id: uuid.UUID,
    metric: LimitMetric,
    window: LimitWindow,
    *,
    now: datetime | None = None,
) -> float:
    """Return a subject's accumulated consumption for ``metric`` in ``window``.

    The counterpart of the increment performed by :func:`record_usage`, used by limit
    evaluation and the token/account usage listings. For fixed windows the current
    bucket's value is returned; for ``ROLLING_24H`` the trailing 24 hourly buckets are
    summed. Absent counters read as ``0.0``.
    """
    moment = (now or _utcnow()).astimezone(timezone.utc)

    if window is LimitWindow.ROLLING_24H:
        total = 0.0
        for hours_ago in range(_ROLLING_24H_HOURS):
            key = counter_key(
                subject_kind,
                subject_id,
                metric,
                window,
                now=moment - timedelta(hours=hours_ago),
            )
            raw = await redis.get(key)
            if raw is not None:
                total += float(raw)
        return total

    raw = await redis.get(
        counter_key(subject_kind, subject_id, metric, window, now=moment)
    )
    return float(raw) if raw is not None else 0.0


def _consumption_metric(spec: UsageLimitSpec) -> LimitMetric:
    """Pick which metered counter backs a limit's consumption read.

    Mirrors the gateway's limit-evaluation mapping
    (:func:`gozar.gateway.pipeline._counter_metric`): a ``request_count`` limit reads
    the request counter; every other metric (``token_count``, ``cost_estimate``,
    ``percentage``) is evaluated against accumulated token consumption, the only
    token-volume signal the Usage_Recorder meters.
    """
    if spec.metric is LimitMetric.REQUEST_COUNT:
        return LimitMetric.REQUEST_COUNT
    return LimitMetric.TOKEN_COUNT


async def read_subject_consumption(
    redis: Redis,
    subject_kind: str,
    subject_id: uuid.UUID,
    spec: UsageLimitSpec | None,
    *,
    now: datetime | None = None,
) -> float:
    """Return a subject's recorded consumption for the account/token usage views.

    The single consumption-reader shared by the Account_Manager and Token_Authority
    listings so both report the same figure the gateway enforces against:

    * When ``spec`` is set, read the counter for the metric the limit measures --
      ``request_count`` for a ``request_count`` limit, otherwise ``token_count`` (see
      :func:`_consumption_metric`) -- scoped to the limit's own ``spec.window``. This
      is the same value :func:`gozar.gateway.pipeline._limit_reached_for` evaluates,
      so the console shows usage measured exactly as the limit is enforced.
    * When ``spec`` is ``None`` (no limit configured), report cumulative total token
      usage: ``token_count`` in :attr:`~gozar.usage.limits.LimitWindow.NONE` (the
      never-resetting cumulative bucket). This gives the console a meaningful
      "recorded usage" figure even when no limit is set.

    Absent counters read as ``0.0`` (see :func:`read_counter`).
    """
    if spec is None:
        return await read_counter(
            redis,
            subject_kind,
            subject_id,
            LimitMetric.TOKEN_COUNT,
            LimitWindow.NONE,
            now=now,
        )
    return await read_counter(
        redis,
        subject_kind,
        subject_id,
        _consumption_metric(spec),
        spec.window,
        now=now,
    )


# Public surface of the Usage_Recorder metering service. Tracing functions
# (open_trace / finalize_trace) are defined in the section below.
__all__ = [
    "SUBJECT_ACCOUNT",
    "SUBJECT_TOKEN",
    "UsageEvent",
    "window_bucket",
    "counter_key",
    "record_usage",
    "read_counter",
    "read_subject_consumption",
    # Request tracing (task 10.3)
    "TRACE_OUTCOMES",
    "InboundMeta",
    "OutboundMeta",
    "open_trace",
    "set_trace_chain",
    "finalize_trace",
    "trace_elapsed",
    "list_traces",
    "get_trace",
]


# ===========================================================================
# Request tracing (open_trace / finalize_trace) -- task 10.3
# ===========================================================================
#
# The tracing half of the Usage_Recorder. It writes the lightweight
# :class:`~gozar.usage.models.TraceLog` row the Web_Console later presents
# (Requirement 14.3). A trace is opened with the inbound request metadata when a
# request arrives (Requirement 14.1) and finalized with the selected credential,
# the outcome, the final status, and the outbound metadata when the response is
# returned (Requirement 14.2). The elapsed duration is not stored as its own field;
# it is derived from ``ended_at - started_at`` (Requirement 14.3, see
# :func:`trace_elapsed`).
#
# As with metering, no secret material (subscription tokens, API keys, client-token
# secrets, or authorization headers) is ever placed into the stored metadata. The
# metadata value objects below expose only an explicit, non-secret set of fields, so
# there is no field through which a secret could be carried (Requirement 16.4).

# Recognised Trace_Log outcomes (mirrors the ``TraceLog.outcome`` documentation).
TRACE_OUTCOMES: tuple[str, ...] = (
    "success",
    "client_error",
    "all_fallbacks_failed",
    "no_account",
)


@dataclass(frozen=True)
class InboundMeta:
    """Non-secret inbound request metadata for a Trace_Log entry (Requirement 14.1).

    Carries only the request-shape fields the console needs to show what came in:
    the HTTP method, the requested model, whether streaming was requested, the
    session-affinity id, and the inbound payload size. There is deliberately no field
    for authorization material, so a secret cannot be traced (Requirement 16.4).

    Attributes
    ----------
    method:
        The inbound HTTP method (e.g. ``"POST"``).
    model:
        The requested model name, if any.
    stream:
        Whether the client requested a streaming (SSE) response.
    session_id:
        The session-affinity identifier, if the client supplied one.
    request_bytes:
        Size of the inbound request body in bytes, if known.
    """

    method: str
    model: str | None = None
    stream: bool = False
    session_id: str | None = None
    chain_id: uuid.UUID | None = None
    request_bytes: int | None = None

    def as_meta(self) -> dict:
        """Render the non-secret metadata blob stored in ``inbound_meta``."""
        meta: dict = {"method": self.method, "stream": self.stream}
        if self.model is not None:
            meta["model"] = self.model
        if self.session_id is not None:
            meta["session_id"] = self.session_id
        if self.chain_id is not None:
            meta["chain_id"] = str(self.chain_id)
        if self.request_bytes is not None:
            meta["request_bytes"] = self.request_bytes
        return meta


@dataclass(frozen=True)
class CredentialTraceSnapshot:
    """Non-secret credential facts captured before account lifecycle changes.

    Trace rows deliberately avoid hard foreign keys to accounts so request history
    survives account deletion. This snapshot lets the console keep showing the
    provider, kind, label, and status that served a request without storing any
    credential secret material.
    """

    account_id: uuid.UUID
    label: str
    provider: str
    kind: str
    status: str

    def as_meta(self) -> dict[str, str]:
        """Render the credential snapshot as JSON-safe metadata."""
        return {
            "account_id": str(self.account_id),
            "label": self.label,
            "provider": self.provider,
            "kind": self.kind,
            "status": self.status,
        }


@dataclass(frozen=True)
class OutboundMeta:
    """Non-secret outbound response metadata for finalizing a trace (Req 14.2).

    Carries the request's terminal :data:`outcome <TRACE_OUTCOMES>`, the selected
    Upstream_Credential (``account_id``; ``None`` when none was chosen, e.g. the
    ``no_account`` outcome), the final HTTP status code, and response-shape fields
    (finish reason, payload size). As with :class:`InboundMeta` there is no field for
    secret material (Requirement 16.4).

    Attributes
    ----------
    outcome:
        One of :data:`TRACE_OUTCOMES`.
    status_code:
        The final HTTP status code returned to the client, if any.
    account_id:
        The Upstream_Credential selected to serve the request, or ``None`` when no
        credential was used.
    credential:
        Optional non-secret snapshot of the selected credential for UI display after
        account deletion, renaming, or status changes.
    finish_reason:
        The provider/response finish reason, if any.
    response_bytes:
        Size of the outbound response body in bytes, if known.
    """

    outcome: str
    status_code: int | None = None
    account_id: uuid.UUID | None = None
    credential: CredentialTraceSnapshot | None = None
    finish_reason: str | None = None
    model: str | None = None
    response_bytes: int | None = None
    routing: dict | None = None

    def as_meta(self) -> dict:
        """Render the non-secret metadata blob stored in ``outbound_meta``."""
        meta: dict = {}
        if self.status_code is not None:
            meta["status_code"] = self.status_code
        if self.credential is not None:
            meta["selected_credential"] = self.credential.as_meta()
        if self.finish_reason is not None:
            meta["finish_reason"] = self.finish_reason
        if self.model is not None:
            meta["model"] = self.model
        if self.response_bytes is not None:
            meta["response_bytes"] = self.response_bytes
        if self.routing is not None:
            meta["routing"] = self.routing
        return meta


async def set_trace_chain(
    session: AsyncSession,
    correlation_id: uuid.UUID,
    chain_id: uuid.UUID | None,
) -> TraceLog:
    """Persist the effective chain after authentication and route resolution."""

    trace = await session.get(TraceLog, correlation_id)
    if trace is None:
        raise NotFound("trace not found for correlation id")
    inbound_meta = dict(trace.inbound_meta or {})
    if chain_id is None:
        inbound_meta.pop("chain_id", None)
    else:
        inbound_meta["chain_id"] = str(chain_id)
    trace.inbound_meta = inbound_meta
    await session.flush()
    return trace


async def open_trace(
    session: AsyncSession,
    correlation_id: uuid.UUID,
    inbound: InboundMeta,
    *,
    now: datetime | None = None,
) -> TraceLog:
    """Create a Trace_Log entry for an arriving request (Requirement 14.1).

    Persists a :class:`~gozar.usage.models.TraceLog` row keyed by ``correlation_id``
    with the inbound request metadata and the ``started_at`` instant; the outbound
    columns stay null until :func:`finalize_trace` runs. Only non-secret metadata is
    stored (Requirement 16.4).

    Args:
        session: The async DB session to persist the row on (committed by the caller
            / request dependency).
        correlation_id: The request correlation id; also links the eventual
            :class:`~gozar.usage.models.UsageRecord`.
        inbound: The non-secret inbound request metadata.
        now: Optional ``started_at`` instant; defaults to the current UTC time.

    Returns:
        The persisted :class:`~gozar.usage.models.TraceLog` (flushed).
    """
    moment = (now or _utcnow()).astimezone(timezone.utc)
    trace = TraceLog(
        correlation_id=correlation_id,
        inbound_meta=inbound.as_meta(),
        started_at=moment,
    )
    session.add(trace)
    await session.flush()
    return trace


async def finalize_trace(
    session: AsyncSession,
    correlation_id: uuid.UUID,
    outcome: OutboundMeta,
    *,
    now: datetime | None = None,
) -> TraceLog:
    """Finalize the Trace_Log entry when the response is returned (Req 14.2).

    Updates the row identified by ``correlation_id`` with the selected
    Upstream_Credential, the terminal outcome, the final status code, the
    ``ended_at`` instant, and the outbound metadata. The elapsed duration is not
    stored; it is derived from ``ended_at - started_at`` (Requirement 14.3, see
    :func:`trace_elapsed`). Only non-secret metadata is stored (Requirement 16.4).

    Args:
        session: The async DB session holding the open trace row.
        correlation_id: The correlation id used to open the trace.
        outcome: The non-secret outbound response metadata; ``outcome.outcome`` must
            be one of :data:`TRACE_OUTCOMES`.
        now: Optional ``ended_at`` instant; defaults to the current UTC time.

    Returns:
        The updated :class:`~gozar.usage.models.TraceLog` (flushed).

    Raises:
        ValidationError: If ``outcome.outcome`` is not a recognised outcome.
        NotFound: If no open trace exists for ``correlation_id``.
    """
    if outcome.outcome not in TRACE_OUTCOMES:
        raise ValidationError(f"unknown trace outcome: {outcome.outcome!r}")

    moment = (now or _utcnow()).astimezone(timezone.utc)
    trace = await session.get(TraceLog, correlation_id)
    if trace is None:
        raise NotFound("trace not found for correlation id")

    trace.account_id = outcome.account_id
    trace.outcome = outcome.outcome
    trace.status_code = outcome.status_code
    trace.ended_at = moment
    trace.outbound_meta = outcome.as_meta()
    await session.flush()
    return trace


async def list_traces(
    session: AsyncSession,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[TraceLog]:
    """Return Trace_Log entries, most recent first, for the console list (Req 14.3).

    A read-only listing of the lightweight per-request traces the Web_Console
    presents. Rows are ordered by ``started_at`` descending and paginated with
    ``limit``/``offset`` so the listing stays bounded. No secret material is stored on
    a trace, so nothing secret can be returned here (Requirement 16.4).
    """
    bounded_limit = max(1, min(limit, 500))
    bounded_offset = max(0, offset)
    rows = await session.scalars(
        select(TraceLog)
        .order_by(TraceLog.started_at.desc())
        .limit(bounded_limit)
        .offset(bounded_offset)
    )
    return list(rows.all())


async def get_trace(session: AsyncSession, correlation_id: uuid.UUID) -> TraceLog:
    """Return a single Trace_Log entry by correlation id (Requirement 14.3).

    Raises :class:`~gozar.core.errors.NotFound` when no trace exists for
    ``correlation_id`` so the route can render a 404.
    """
    trace = await session.get(TraceLog, correlation_id)
    if trace is None:
        raise NotFound("trace not found for correlation id")
    return trace


def trace_elapsed(trace: TraceLog) -> timedelta | None:
    """Return a finalized trace's elapsed duration, or ``None`` if still open.

    The elapsed duration is derived from the stored timestamps rather than persisted
    on its own (Requirement 14.3). Returns ``None`` until :func:`finalize_trace` has
    set ``ended_at``. Both instants are written in UTC; any naive value read back is
    treated as UTC so the subtraction is always well defined.
    """
    if trace.ended_at is None or trace.started_at is None:
        return None
    return _as_utc(trace.ended_at) - _as_utc(trace.started_at)
