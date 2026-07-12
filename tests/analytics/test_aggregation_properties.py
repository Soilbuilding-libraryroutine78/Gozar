"""Property-based tests for Analytics_Service aggregation (Property 19).

Validates Property 19 from the Gozar design: for any set of usage records and trace
logs over a time range, the analytics reports produced by
:mod:`gozar.analytics.service` (``token_report``, ``account_report``,
``system_report``) equal a straightforward, independent reference aggregation
computed directly from the generated rows -- request counts, token sums, error
counts, error rates, and consumption-vs-configured-limit.

The reference model here is deliberately *independent* of the implementation: it does
not call the analytics service's private aggregation helpers. It re-derives every
reported number with a plain Python pass over the generated data, honouring the
half-open ``[start, end)`` UTC range semantics (a row is in range iff
``start <= timestamp < end``).

Each example builds a fresh in-memory async SQLite database (the project's
service-layer test convention, mirroring ``tests/analytics/test_reports.py``),
inserts the generated ``UsageRecord`` / ``TraceLog`` / limit-config rows, runs the
real reports through the ORM, and compares against the reference.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from gozar.accounts.models import AccountUsageLimit, UpstreamCredential
from gozar.analytics.service import (
    TimeRange,
    account_report,
    system_report,
    token_report,
)
from gozar.core.db import Base
from gozar.tokens.models import ClientToken, TokenUsageLimit
from gozar.usage.limits import LimitMetric, LimitWindow
from gozar.usage.models import TraceLog, UsageRecord

# The aggregated tables plus the limit-config tables and the credential/token tables
# their (non-enforced under SQLite) foreign keys reference, so the schema resolves.
_TEST_TABLES = [
    UpstreamCredential.__table__,
    ClientToken.__table__,
    UsageRecord.__table__,
    TraceLog.__table__,
    AccountUsageLimit.__table__,
    TokenUsageLimit.__table__,
]

# Fixed subject pools so generated rows group onto a small, deterministic set of
# tokens/credentials -- this makes per-subject aggregation meaningful rather than
# every row landing on a unique id.
_TOKEN_IDS = [uuid.UUID(int=i + 1) for i in range(3)]
_ACCOUNT_IDS = [uuid.UUID(int=100 + i) for i in range(3)]

# Reference window: the half-open range [_BASE, _BASE + 1 day) in UTC. The window is
# exactly 86_400 seconds wide, so an offset is in range iff 0 <= offset < 86_400.
_BASE = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
_RANGE_SECONDS = 86_400
_RANGE = TimeRange(start=_BASE, end=_BASE + timedelta(seconds=_RANGE_SECONDS))

# Outcomes the Usage_Recorder can finalize a trace to; the error set mirrors the
# documented analytics definition (everything other than a clean success). ``None``
# models a trace that was opened but never finalized.
_ERROR_OUTCOMES = frozenset({"client_error", "all_fallbacks_failed", "no_account"})
_TRACE_OUTCOMES = ["success", "client_error", "all_fallbacks_failed", "no_account", None]

# Offsets span well before, inside, exactly on, and after the range so the boundary
# semantics (start inclusive at 0, end exclusive at 86_400) are exercised.
_offsets = st.integers(min_value=-90_000, max_value=180_000)


def _in_range(offset: int) -> bool:
    """Reference membership test for the half-open ``[start, end)`` range."""
    return 0 <= offset < _RANGE_SECONDS


# --- generators --------------------------------------------------------------

_usage_entry = st.fixed_dictionaries(
    {
        "token": st.integers(min_value=0, max_value=len(_TOKEN_IDS) - 1),
        "account": st.integers(min_value=0, max_value=len(_ACCOUNT_IDS) - 1),
        "offset": _offsets,
        "prompt": st.integers(min_value=0, max_value=5_000),
        "completion": st.integers(min_value=0, max_value=5_000),
    }
)

_trace_entry = st.fixed_dictionaries(
    {
        "account": st.one_of(
            st.none(), st.integers(min_value=0, max_value=len(_ACCOUNT_IDS) - 1)
        ),
        "offset": _offsets,
        "outcome": st.sampled_from(_TRACE_OUTCOMES),
    }
)

# A per-subject limit configuration, or ``None`` for "no limit configured". Absolute
# metrics use ``limit_value`` directly; the percentage metric needs a positive
# capacity (enforced by UsageLimitSpec) used as the denominator.
_limit_params = st.one_of(
    st.none(),
    st.fixed_dictionaries(
        {
            "metric": st.sampled_from(
                ["request_count", "token_count", "cost_estimate"]
            ),
            "limit_value": st.integers(min_value=0, max_value=100_000),
            "capacity": st.none(),
        }
    ),
    st.fixed_dictionaries(
        {
            "metric": st.just("percentage"),
            "limit_value": st.integers(min_value=0, max_value=100),
            "capacity": st.integers(min_value=1, max_value=100_000),
        }
    ),
)

_limit_list = st.lists(_limit_params, min_size=3, max_size=3)


# --- reference model ---------------------------------------------------------


def _ref_consumption(
    limit: dict | None, request_count: int, total_tokens: int
) -> tuple[float | None, float | None, bool | None]:
    """Independently derive (consumed, percent_of_limit, reached) for a subject.

    Mirrors the documented analytics semantics without calling the implementation:
    request-count limits measure the request count, token-count and percentage limits
    measure the total-token sum, and a cost-estimate limit is not derivable from the
    metering rows (``consumed`` is ``None``).
    """
    if limit is None:
        return (None, None, None)

    metric = limit["metric"]
    if metric == "request_count":
        consumed: float | None = float(request_count)
    elif metric in ("token_count", "percentage"):
        consumed = float(total_tokens)
    else:  # cost_estimate -- no source quantity in usage_record
        consumed = None

    if consumed is None:
        return (None, None, None)

    limit_value = float(limit["limit_value"])
    if metric == "percentage":
        capacity = float(limit["capacity"])
        percent: float | None = consumed / capacity * 100.0
        reached: bool | None = percent >= limit_value
    else:
        percent = (consumed / limit_value * 100.0) if limit_value > 0 else None
        reached = consumed >= limit_value

    return (consumed, percent, reached)


# --- DB helpers --------------------------------------------------------------


async def _run_example(
    usage: list[dict], traces: list[dict], token_limits: list, account_limits: list
) -> None:
    """Build a fresh DB from the generated data, run the reports, and compare."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=_TEST_TABLES)
        maker = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        async with maker() as session:
            await _insert_rows(session, usage, traces, token_limits, account_limits)
            await _check_reports(session, usage, traces, token_limits, account_limits)
    finally:
        await engine.dispose()


async def _insert_rows(
    session: AsyncSession,
    usage: list[dict],
    traces: list[dict],
    token_limits: list,
    account_limits: list,
) -> None:
    for entry in usage:
        total = entry["prompt"] + entry["completion"]
        session.add(
            UsageRecord(
                correlation_id=uuid.uuid4(),
                client_token_id=_TOKEN_IDS[entry["token"]],
                account_id=_ACCOUNT_IDS[entry["account"]],
                provider="openai",
                prompt_tokens=entry["prompt"],
                completion_tokens=entry["completion"],
                total_tokens=total,
                created_at=_BASE + timedelta(seconds=entry["offset"]),
            )
        )
    for entry in traces:
        account_id = (
            None if entry["account"] is None else _ACCOUNT_IDS[entry["account"]]
        )
        session.add(
            TraceLog(
                correlation_id=uuid.uuid4(),
                inbound_meta={"method": "POST"},
                account_id=account_id,
                outcome=entry["outcome"],
                started_at=_BASE + timedelta(seconds=entry["offset"]),
            )
        )
    for idx, limit in enumerate(token_limits):
        if limit is None:
            continue
        session.add(
            TokenUsageLimit(
                subject_id=_TOKEN_IDS[idx],
                metric=limit["metric"],
                limit_value=limit["limit_value"],
                capacity=limit["capacity"],
                window=LimitWindow.NONE.value,
            )
        )
    for idx, limit in enumerate(account_limits):
        if limit is None:
            continue
        session.add(
            AccountUsageLimit(
                subject_id=_ACCOUNT_IDS[idx],
                metric=LimitMetric(limit["metric"]),
                limit_value=limit["limit_value"],
                capacity=limit["capacity"],
                window=LimitWindow.NONE,
            )
        )
    await session.flush()


def _ref_counts_for(usage: list[dict], *, key: str, idx: int) -> tuple[int, int, int, int]:
    """Reference (request_count, prompt, completion, total) for one subject in range."""
    requests = prompt = completion = total = 0
    for entry in usage:
        if not _in_range(entry["offset"]):
            continue
        if entry[key] != idx:
            continue
        requests += 1
        prompt += entry["prompt"]
        completion += entry["completion"]
        total += entry["prompt"] + entry["completion"]
    return requests, prompt, completion, total


def _assert_consumption(actual, limit: dict | None, request_count: int, total_tokens: int) -> None:
    consumed, percent, reached = _ref_consumption(limit, request_count, total_tokens)
    if limit is None:
        assert actual.spec is None
    else:
        assert actual.spec is not None
    assert actual.consumed == consumed
    assert actual.reached == reached
    if percent is None:
        assert actual.percent_of_limit is None
    else:
        assert actual.percent_of_limit == pytest.approx(percent)


async def _check_reports(
    session: AsyncSession,
    usage: list[dict],
    traces: list[dict],
    token_limits: list,
    account_limits: list,
) -> None:
    # --- per-token (Requirement 15.1) ---
    for idx, token_id in enumerate(_TOKEN_IDS):
        requests, prompt, completion, total = _ref_counts_for(
            usage, key="token", idx=idx
        )
        report = await token_report(session, token_id, _RANGE)
        assert report.counts.request_count == requests
        assert report.counts.prompt_tokens == prompt
        assert report.counts.completion_tokens == completion
        assert report.counts.total_tokens == total
        _assert_consumption(report.consumption, token_limits[idx], requests, total)

    # --- per-account (Requirement 15.2) ---
    for idx, account_id in enumerate(_ACCOUNT_IDS):
        requests, prompt, completion, total = _ref_counts_for(
            usage, key="account", idx=idx
        )
        ref_errors = sum(
            1
            for t in traces
            if _in_range(t["offset"])
            and t["account"] == idx
            and t["outcome"] in _ERROR_OUTCOMES
        )
        report = await account_report(session, account_id, _RANGE)
        assert report.counts.request_count == requests
        assert report.counts.prompt_tokens == prompt
        assert report.counts.completion_tokens == completion
        assert report.counts.total_tokens == total
        assert report.error_count == ref_errors
        _assert_consumption(report.consumption, account_limits[idx], requests, total)

    # --- system-wide (Requirement 15.3) ---
    ref_requests = sum(1 for t in traces if _in_range(t["offset"]))
    ref_errors = sum(
        1
        for t in traces
        if _in_range(t["offset"]) and t["outcome"] in _ERROR_OUTCOMES
    )
    ref_total_tokens = sum(
        e["prompt"] + e["completion"] for e in usage if _in_range(e["offset"])
    )
    ref_rate = (ref_errors / ref_requests) if ref_requests > 0 else 0.0

    system = await system_report(session, _RANGE)
    assert system.request_count == ref_requests
    assert system.error_count == ref_errors
    assert system.total_tokens == ref_total_tokens
    assert system.error_rate == pytest.approx(ref_rate)


# --- property test -----------------------------------------------------------


# Feature: gozar, Property 19: Analytics aggregation matches the reference model
@hyp_settings(max_examples=120, deadline=None)
@given(
    usage=st.lists(_usage_entry, max_size=14),
    traces=st.lists(_trace_entry, max_size=14),
    token_limits=_limit_list,
    account_limits=_limit_list,
)
def test_analytics_reports_match_reference_model(
    usage: list[dict],
    traces: list[dict],
    token_limits: list,
    account_limits: list,
) -> None:
    """Validates: Requirements 15.1, 15.2, 15.3.

    For any generated set of usage records and trace logs over the reference range,
    the per-token, per-account, and system reports equal an independent Python
    aggregation of the in-range rows: request counts, token sums, error counts, error
    rate, and consumption against the configured limits all match.
    """
    asyncio.run(_run_example(usage, traces, token_limits, account_limits))
