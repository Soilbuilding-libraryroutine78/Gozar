"""Analytics_Service: per-token, per-account, and system reporting.

This module implements the Analytics_Service described in the design: it aggregates
the durable Usage_Recorder data into per-token, per-account, and system reports over
a selected time range (Requirement 15). It reads two durable Postgres tables and is
otherwise pure aggregation -- no Redis, no provider calls, no clock dependency beyond
the caller-supplied :class:`TimeRange`.

Data sources
------------
* :class:`gozar.usage.models.UsageRecord` (``usage_record``) -- one row per completed
  proxied request. This is the source of truth for **request counts** and
  **token sums** for a Client_Token (``client_token_id``) or an Upstream_Credential
  (``account_id``). Its ``created_at`` column is indexed for range queries.
* :class:`gozar.usage.models.TraceLog` (``trace_log``) -- one row per request keyed by
  correlation id, carrying the request ``outcome`` and final ``status_code``. The
  metering row carries no error flag, so **error counts and error rates** are derived
  here from the trace log's ``outcome`` (Requirements 15.2, 15.3). A trace is counted
  as an error when its ``outcome`` is finalized to anything other than ``success``
  (i.e. ``client_error``, ``all_fallbacks_failed`` or ``no_account``).

Per-token error counts are not reported because the trace log is keyed/attributed by
credential (``account_id``), not by Client_Token -- Requirement 15.1 asks only for
request counts, token counts, and consumption for a token, which this honours.

Consumption vs configured limit
--------------------------------
Each report computes the subject's consumption over the range against its configured
:class:`~gozar.usage.limits.UsageLimitSpec` (the per-token ``tok_usage_limit`` or the
per-account ``acct_usage_limit`` row), reusing the pure
:func:`~gozar.usage.limits.consumed_percentage` /
:func:`~gozar.usage.limits.limit_reached` evaluation. The measured quantity matched to
the limit's metric is:

* ``request_count`` -- the aggregated request count over the range;
* ``token_count`` -- the aggregated total-token sum over the range;
* ``percentage`` -- the aggregated total-token sum, evaluated as a percentage of the
  limit's configured ``capacity`` (token throughput is the metered quantity the
  percentage limit caps);
* ``cost_estimate`` -- not derivable from ``usage_record`` (which stores token counts,
  not cost), so consumption is reported as ``None``.

Range semantics
---------------
A :class:`TimeRange` is half-open ``[start, end)`` in UTC: a record is included when
``start <= timestamp < end``. This avoids double-counting a record that sits exactly on
the boundary between two adjacent ranges.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gozar.accounts.models import AccountUsageLimit
from gozar.core.errors import ValidationError
from gozar.tokens.models import TokenUsageLimit
from gozar.usage.limits import (
    LimitMetric,
    LimitWindow,
    UsageLimitSpec,
    consumed_percentage,
    limit_reached,
)
from gozar.usage.models import TraceLog, UsageRecord

# Trace outcomes that count as an error for analytics (everything the Usage_Recorder
# can finalize a trace to other than a clean success). Kept explicit so the set is
# obvious at the call site and stays aligned with ``TraceLog.outcome``.
ERROR_OUTCOMES: tuple[str, ...] = (
    "client_error",
    "all_fallbacks_failed",
    "no_account",
)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimeRange:
    """A half-open ``[start, end)`` UTC time range for a report.

    Both bounds are normalised to UTC (a naive value is treated as already-UTC). A
    record is included when ``start <= timestamp < end``. ``start`` must not be after
    ``end``; an empty range (``start == end``) is permitted and yields zero rows.
    """

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", _as_utc(self.start))
        object.__setattr__(self, "end", _as_utc(self.end))
        if self.start > self.end:
            raise ValidationError("TimeRange start must not be after end")


@dataclass(frozen=True)
class TokenCounts:
    """Aggregated request and token counts over a range."""

    request_count: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class LimitConsumption:
    """A subject's consumption over the range against its configured limit.

    ``spec`` is the configured :class:`~gozar.usage.limits.UsageLimitSpec`, or ``None``
    when the subject has no limit configured (in which case the remaining fields are
    ``None``). ``consumed`` is the measured quantity matched to the limit's metric, or
    ``None`` for a ``cost_estimate`` limit (not derivable from ``usage_record``).
    ``percent_of_limit`` is ``consumed`` as a percentage of the configured threshold
    (capacity for a percentage limit, ``limit_value`` otherwise), or ``None`` when the
    threshold is zero / undefined. ``reached`` mirrors
    :func:`~gozar.usage.limits.limit_reached`.
    """

    spec: UsageLimitSpec | None
    consumed: float | None
    percent_of_limit: float | None
    reached: bool | None


@dataclass(frozen=True)
class TokenAnalytics:
    """Per-token report (Requirement 15.1)."""

    token_id: uuid.UUID
    range: TimeRange
    counts: TokenCounts
    consumption: LimitConsumption


@dataclass(frozen=True)
class AccountAnalytics:
    """Per-account report (Requirement 15.2)."""

    account_id: uuid.UUID
    range: TimeRange
    counts: TokenCounts
    error_count: int
    consumption: LimitConsumption


@dataclass(frozen=True)
class SystemAnalytics:
    """System-wide report across all credentials (Requirement 15.3)."""

    range: TimeRange
    request_count: int
    error_count: int
    error_rate: float
    total_tokens: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_utc(moment: datetime) -> datetime:
    """Normalise a datetime to UTC, treating a naive value as already-UTC."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _spec_from_columns(
    metric: object,
    limit_value: object,
    capacity: object,
    window: object,
) -> UsageLimitSpec:
    """Build a :class:`UsageLimitSpec` from a limit row's columns.

    Handles both the account table (enum-typed ``metric``/``window`` columns) and the
    token table (plain string columns), and ``Numeric`` columns that come back as
    ``Decimal``.
    """
    metric_enum = metric if isinstance(metric, LimitMetric) else LimitMetric(str(metric))
    window_enum = window if isinstance(window, LimitWindow) else LimitWindow(str(window))
    return UsageLimitSpec(
        metric=metric_enum,
        limit_value=float(limit_value),  # type: ignore[arg-type]
        capacity=None if capacity is None else float(capacity),  # type: ignore[arg-type]
        window=window_enum,
    )


def _consumption(
    spec: UsageLimitSpec | None, counts: TokenCounts
) -> LimitConsumption:
    """Compute consumption against ``spec`` from aggregated ``counts``.

    The measured quantity is matched to the limit's metric (see the module docstring).
    For a ``cost_estimate`` limit there is no source quantity in ``usage_record`` so
    ``consumed`` is ``None``.
    """
    if spec is None:
        return LimitConsumption(spec=None, consumed=None, percent_of_limit=None, reached=None)

    if spec.metric is LimitMetric.REQUEST_COUNT:
        consumed: float | None = float(counts.request_count)
    elif spec.metric in (LimitMetric.TOKEN_COUNT, LimitMetric.PERCENTAGE):
        consumed = float(counts.total_tokens)
    else:  # COST_ESTIMATE -- not derivable from usage_record
        consumed = None

    if consumed is None:
        return LimitConsumption(spec=spec, consumed=None, percent_of_limit=None, reached=None)

    if spec.metric is LimitMetric.PERCENTAGE:
        # capacity is guaranteed positive by UsageLimitSpec validation.
        assert spec.capacity is not None
        percent: float | None = consumed_percentage(consumed, spec.capacity)
    elif spec.limit_value > 0:
        percent = consumed / spec.limit_value * 100.0
    else:
        percent = None

    return LimitConsumption(
        spec=spec,
        consumed=consumed,
        percent_of_limit=percent,
        reached=limit_reached(consumed, spec),
    )


async def _aggregate_counts(
    session: AsyncSession,
    range: TimeRange,
    *,
    column: object | None = None,
    value: uuid.UUID | None = None,
) -> TokenCounts:
    """Aggregate request count and token sums from ``usage_record`` over the range.

    When ``column``/``value`` are supplied the aggregation is filtered to that subject
    (e.g. ``UsageRecord.client_token_id == token_id``); otherwise it spans all rows.
    """
    stmt = select(
        func.count(UsageRecord.id),
        func.coalesce(func.sum(UsageRecord.prompt_tokens), 0),
        func.coalesce(func.sum(UsageRecord.completion_tokens), 0),
        func.coalesce(func.sum(UsageRecord.total_tokens), 0),
    ).where(
        UsageRecord.created_at >= range.start,
        UsageRecord.created_at < range.end,
    )
    if column is not None:
        stmt = stmt.where(column == value)

    row = (await session.execute(stmt)).one()
    return TokenCounts(
        request_count=int(row[0]),
        prompt_tokens=int(row[1]),
        completion_tokens=int(row[2]),
        total_tokens=int(row[3]),
    )


async def _count_errors(
    session: AsyncSession,
    range: TimeRange,
    *,
    account_id: uuid.UUID | None = None,
) -> int:
    """Count error traces over the range, optionally scoped to one credential.

    A trace is an error when its ``outcome`` is one of :data:`ERROR_OUTCOMES`. Traces
    are filtered by their ``started_at`` instant.
    """
    stmt = (
        select(func.count())
        .select_from(TraceLog)
        .where(
            TraceLog.started_at >= range.start,
            TraceLog.started_at < range.end,
            TraceLog.outcome.in_(ERROR_OUTCOMES),
        )
    )
    if account_id is not None:
        stmt = stmt.where(TraceLog.account_id == account_id)
    return int(await session.scalar(stmt) or 0)


async def _token_limit_spec(
    session: AsyncSession, token_id: uuid.UUID
) -> UsageLimitSpec | None:
    """Load the Client_Token's configured limit, or ``None`` if it has none."""
    row = (
        await session.execute(
            select(
                TokenUsageLimit.metric,
                TokenUsageLimit.limit_value,
                TokenUsageLimit.capacity,
                TokenUsageLimit.window,
            ).where(TokenUsageLimit.subject_id == token_id)
        )
    ).first()
    if row is None:
        return None
    return _spec_from_columns(*row)


async def _account_limit_spec(
    session: AsyncSession, account_id: uuid.UUID
) -> UsageLimitSpec | None:
    """Load the Upstream_Credential's configured limit, or ``None`` if it has none."""
    row = (
        await session.execute(
            select(
                AccountUsageLimit.metric,
                AccountUsageLimit.limit_value,
                AccountUsageLimit.capacity,
                AccountUsageLimit.window,
            ).where(AccountUsageLimit.subject_id == account_id)
        )
    ).first()
    if row is None:
        return None
    return _spec_from_columns(*row)


# ---------------------------------------------------------------------------
# Public reports
# ---------------------------------------------------------------------------


async def token_report(
    session: AsyncSession, token_id: uuid.UUID, range: TimeRange
) -> TokenAnalytics:
    """Aggregate a Client_Token's usage over ``range`` (Requirement 15.1).

    Reports aggregated request counts, token-count sums, and consumption against the
    token's configured Usage_Limit. Counts come from ``usage_record`` rows whose
    ``client_token_id`` matches ``token_id``.
    """
    counts = await _aggregate_counts(
        session, range, column=UsageRecord.client_token_id, value=token_id
    )
    spec = await _token_limit_spec(session, token_id)
    return TokenAnalytics(
        token_id=token_id,
        range=range,
        counts=counts,
        consumption=_consumption(spec, counts),
    )


async def account_report(
    session: AsyncSession, account_id: uuid.UUID, range: TimeRange
) -> AccountAnalytics:
    """Aggregate an Upstream_Credential's usage over ``range`` (Requirement 15.2).

    Reports aggregated request counts, token-count sums, error counts, and consumption
    against the credential's configured Usage_Limit. Counts come from ``usage_record``
    rows whose ``account_id`` matches; error counts come from ``trace_log`` rows for
    the credential with an error ``outcome``.
    """
    counts = await _aggregate_counts(
        session, range, column=UsageRecord.account_id, value=account_id
    )
    error_count = await _count_errors(session, range, account_id=account_id)
    spec = await _account_limit_spec(session, account_id)
    return AccountAnalytics(
        account_id=account_id,
        range=range,
        counts=counts,
        error_count=error_count,
        consumption=_consumption(spec, counts),
    )


async def system_report(session: AsyncSession, range: TimeRange) -> SystemAnalytics:
    """Aggregate system-wide usage over ``range`` (Requirement 15.3).

    Reports the aggregated request count and the error rate across all
    Upstream_Credentials. The request count and error count both come from
    ``trace_log`` (the per-request log that records every request and its outcome,
    including failures that never produced a metering row), so the error rate
    (``error_count / request_count``) is internally consistent. The total token sum
    from ``usage_record`` is included as a supplementary figure.
    """
    request_count = int(
        await session.scalar(
            select(func.count())
            .select_from(TraceLog)
            .where(
                TraceLog.started_at >= range.start,
                TraceLog.started_at < range.end,
            )
        )
        or 0
    )
    error_count = await _count_errors(session, range)
    error_rate = (error_count / request_count) if request_count > 0 else 0.0
    counts = await _aggregate_counts(session, range)
    return SystemAnalytics(
        range=range,
        request_count=request_count,
        error_count=error_count,
        error_rate=error_rate,
        total_tokens=counts.total_tokens,
    )


__all__ = [
    "ERROR_OUTCOMES",
    "TimeRange",
    "TokenCounts",
    "LimitConsumption",
    "TokenAnalytics",
    "AccountAnalytics",
    "SystemAnalytics",
    "token_report",
    "account_report",
    "system_report",
]
