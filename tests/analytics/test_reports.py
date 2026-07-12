"""Unit tests for the Analytics_Service reports (task 13.1).

Exercises :mod:`gozar.analytics.service` against a real in-memory async SQLite
database (the project's convention for service-layer tests, mirroring
``tests/usage/test_usage_recording.py``). The reports are driven through the real ORM
queries over the durable ``usage_record`` and ``trace_log`` tables plus the
``tok_usage_limit`` / ``acct_usage_limit`` configuration rows:

* per-token request counts, token sums, and consumption vs the token limit (Req 15.1);
* per-account request counts, token sums, error counts, and consumption (Req 15.2);
* system request counts and error rate across all credentials (Req 15.3);
* the half-open ``[start, end)`` range semantics and the consumption-vs-limit math.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
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
from gozar.core.errors import ValidationError
from gozar.tokens.models import ClientToken, TokenUsageLimit
from gozar.usage.limits import LimitMetric, LimitWindow
from gozar.usage.models import TraceLog, UsageRecord

# The two aggregated tables plus the limit-config tables and the credential/token
# tables their foreign keys reference, so the schema resolves cleanly under SQLite.
_TEST_TABLES = [
    UpstreamCredential.__table__,
    ClientToken.__table__,
    UsageRecord.__table__,
    TraceLog.__table__,
    AccountUsageLimit.__table__,
    TokenUsageLimit.__table__,
]

# A fixed reference window so range membership is deterministic in assertions.
_T0 = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
_RANGE = TimeRange(start=_T0, end=_T0 + timedelta(days=1))


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    """Yield a session backed by a fresh in-memory SQLite database."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TEST_TABLES)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _add_usage(
    session: AsyncSession,
    *,
    token_id: uuid.UUID,
    account_id: uuid.UUID,
    created_at: datetime,
    prompt: int = 4,
    completion: int = 6,
    total: int = 10,
    provider: str = "openai",
) -> None:
    session.add(
        UsageRecord(
            correlation_id=uuid.uuid4(),
            client_token_id=token_id,
            account_id=account_id,
            provider=provider,
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            created_at=created_at,
        )
    )
    await session.flush()


async def _add_trace(
    session: AsyncSession,
    *,
    started_at: datetime,
    outcome: str | None,
    account_id: uuid.UUID | None = None,
    status_code: int | None = None,
) -> None:
    session.add(
        TraceLog(
            correlation_id=uuid.uuid4(),
            inbound_meta={"method": "POST"},
            account_id=account_id,
            outcome=outcome,
            status_code=status_code,
            started_at=started_at,
        )
    )
    await session.flush()


# --- TimeRange ---------------------------------------------------------------


def test_timerange_rejects_start_after_end() -> None:
    with pytest.raises(ValidationError):
        TimeRange(start=_T0 + timedelta(hours=1), end=_T0)


def test_timerange_normalises_naive_to_utc() -> None:
    rng = TimeRange(start=datetime(2024, 6, 1), end=datetime(2024, 6, 2))
    assert rng.start.tzinfo == timezone.utc
    assert rng.end.tzinfo == timezone.utc


# --- token_report (Requirement 15.1) -----------------------------------------


async def test_token_report_aggregates_requests_and_tokens(
    session: AsyncSession,
) -> None:
    token_id = uuid.uuid4()
    account_id = uuid.uuid4()
    other_token = uuid.uuid4()

    await _add_usage(session, token_id=token_id, account_id=account_id, created_at=_T0)
    await _add_usage(
        session,
        token_id=token_id,
        account_id=account_id,
        created_at=_T0 + timedelta(hours=2),
        prompt=1,
        completion=2,
        total=3,
    )
    # A different token must not bleed into this token's totals.
    await _add_usage(
        session, token_id=other_token, account_id=account_id, created_at=_T0
    )

    report = await token_report(session, token_id, _RANGE)

    assert report.counts.request_count == 2
    assert report.counts.prompt_tokens == 5
    assert report.counts.completion_tokens == 8
    assert report.counts.total_tokens == 13
    # No limit configured -> consumption fields are all None.
    assert report.consumption.spec is None
    assert report.consumption.consumed is None


async def test_token_report_excludes_records_outside_range(
    session: AsyncSession,
) -> None:
    token_id = uuid.uuid4()
    account_id = uuid.uuid4()

    await _add_usage(session, token_id=token_id, account_id=account_id, created_at=_T0)
    # End is exclusive: a record exactly at ``end`` is not counted.
    await _add_usage(
        session, token_id=token_id, account_id=account_id, created_at=_RANGE.end
    )
    # Well before the range.
    await _add_usage(
        session,
        token_id=token_id,
        account_id=account_id,
        created_at=_T0 - timedelta(days=5),
    )

    report = await token_report(session, token_id, _RANGE)

    assert report.counts.request_count == 1
    assert report.counts.total_tokens == 10


async def test_token_report_consumption_against_token_count_limit(
    session: AsyncSession,
) -> None:
    token_id = uuid.uuid4()
    account_id = uuid.uuid4()
    # Three requests of 10 tokens each = 30 tokens consumed.
    for _ in range(3):
        await _add_usage(
            session, token_id=token_id, account_id=account_id, created_at=_T0
        )
    session.add(
        TokenUsageLimit(
            subject_id=token_id,
            metric=LimitMetric.TOKEN_COUNT.value,
            limit_value=60,
            window=LimitWindow.NONE.value,
        )
    )
    await session.flush()

    report = await token_report(session, token_id, _RANGE)

    assert report.consumption.consumed == 30.0
    assert report.consumption.percent_of_limit == pytest.approx(50.0)
    assert report.consumption.reached is False


# --- account_report (Requirement 15.2) ---------------------------------------


async def test_account_report_counts_errors_for_the_credential(
    session: AsyncSession,
) -> None:
    account_id = uuid.uuid4()
    other_account = uuid.uuid4()
    token_id = uuid.uuid4()

    await _add_usage(session, token_id=token_id, account_id=account_id, created_at=_T0)
    await _add_usage(session, token_id=token_id, account_id=account_id, created_at=_T0)

    # Two error traces for this account, one success, plus one error for another.
    await _add_trace(
        session, started_at=_T0, outcome="all_fallbacks_failed", account_id=account_id
    )
    await _add_trace(
        session, started_at=_T0, outcome="client_error", account_id=account_id
    )
    await _add_trace(session, started_at=_T0, outcome="success", account_id=account_id)
    await _add_trace(
        session, started_at=_T0, outcome="client_error", account_id=other_account
    )

    report = await account_report(session, account_id, _RANGE)

    assert report.counts.request_count == 2
    assert report.counts.total_tokens == 20
    assert report.error_count == 2


async def test_account_report_consumption_against_request_count_limit(
    session: AsyncSession,
) -> None:
    account_id = uuid.uuid4()
    token_id = uuid.uuid4()
    for _ in range(4):
        await _add_usage(
            session, token_id=token_id, account_id=account_id, created_at=_T0
        )
    session.add(
        AccountUsageLimit(
            subject_id=account_id,
            metric=LimitMetric.REQUEST_COUNT,
            limit_value=4,
            window=LimitWindow.NONE,
        )
    )
    await session.flush()

    report = await account_report(session, account_id, _RANGE)

    assert report.consumption.consumed == 4.0
    assert report.consumption.percent_of_limit == pytest.approx(100.0)
    # consumption >= limit_value -> limit reached.
    assert report.consumption.reached is True


async def test_account_report_percentage_limit_uses_token_throughput(
    session: AsyncSession,
) -> None:
    account_id = uuid.uuid4()
    token_id = uuid.uuid4()
    # 80 tokens consumed against a capacity of 100 -> 80%.
    await _add_usage(
        session,
        token_id=token_id,
        account_id=account_id,
        created_at=_T0,
        total=80,
    )
    session.add(
        AccountUsageLimit(
            subject_id=account_id,
            metric=LimitMetric.PERCENTAGE,
            limit_value=90,
            capacity=100,
            window=LimitWindow.NONE,
        )
    )
    await session.flush()

    report = await account_report(session, account_id, _RANGE)

    assert report.consumption.consumed == 80.0
    assert report.consumption.percent_of_limit == pytest.approx(80.0)
    assert report.consumption.reached is False


# --- system_report (Requirement 15.3) ----------------------------------------


async def test_system_report_request_count_and_error_rate(
    session: AsyncSession,
) -> None:
    account_id = uuid.uuid4()
    token_id = uuid.uuid4()

    # Four traces in range: 1 error, 3 success -> error_rate 0.25.
    await _add_trace(session, started_at=_T0, outcome="success", account_id=account_id)
    await _add_trace(session, started_at=_T0, outcome="success", account_id=account_id)
    await _add_trace(session, started_at=_T0, outcome="success")
    await _add_trace(session, started_at=_T0, outcome="no_account")
    # A trace outside the range is ignored.
    await _add_trace(
        session, started_at=_RANGE.end, outcome="client_error", account_id=account_id
    )

    # Token sums come from usage_record.
    await _add_usage(
        session, token_id=token_id, account_id=account_id, created_at=_T0, total=10
    )

    report = await system_report(session, _RANGE)

    assert report.request_count == 4
    assert report.error_count == 1
    assert report.error_rate == pytest.approx(0.25)
    assert report.total_tokens == 10


async def test_system_report_empty_range_has_zero_error_rate(
    session: AsyncSession,
) -> None:
    report = await system_report(session, _RANGE)

    assert report.request_count == 0
    assert report.error_count == 0
    assert report.error_rate == 0.0
    assert report.total_tokens == 0
