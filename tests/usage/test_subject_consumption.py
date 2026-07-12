"""Unit tests for :func:`gozar.usage.service.read_subject_consumption`.

The account/token admin views read recorded consumption through this single shared
helper so both report the same figure the Proxy_Gateway enforces against. These tests
pin its metric/window mapping (Requirements 5.4, 8.3, 13.3):

* a ``request_count`` limit reads the request counter in the limit's window;
* every other metric reads the token counter in the limit's window;
* no limit (``spec is None``) reports cumulative ``token_count`` in the ``NONE``
  bucket so the console still shows a meaningful "recorded usage" figure.

The counters are exercised against the same in-memory Redis stand-in the metering
tests use, fed by the real :func:`record_usage` path, so the mapping is verified
end-to-end rather than against hand-placed keys.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from gozar.accounts.models import UpstreamCredential
from gozar.core.db import Base
from gozar.tokens.models import ClientToken
from gozar.usage.limits import LimitMetric, LimitWindow, UsageLimitSpec
from gozar.usage.models import UsageRecord
from gozar.usage.service import (
    SUBJECT_ACCOUNT,
    SUBJECT_TOKEN,
    UsageEvent,
    read_subject_consumption,
    record_usage,
)

# Reuse the metering test's in-memory Redis stand-in (same pipeline/get surface).
from tests.usage.test_usage_recording import FakeRedis

_TEST_TABLES = [
    UpstreamCredential.__table__,
    ClientToken.__table__,
    UsageRecord.__table__,
]

_FIXED_NOW = datetime(2024, 6, 28, 21, 30, 15, tzinfo=timezone.utc)


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


async def _record(
    session: AsyncSession,
    redis: FakeRedis,
    *,
    account_id: uuid.UUID,
    token_id: uuid.UUID,
    total_tokens: int,
) -> None:
    """Meter one completed request into the counters via the real recorder."""
    await record_usage(
        session,
        UsageEvent(
            correlation_id=uuid.uuid4(),
            client_token_id=token_id,
            account_id=account_id,
            provider="openai",
            prompt_tokens=total_tokens,
            completion_tokens=0,
            total_tokens=total_tokens,
        ),
        redis=redis,
        now=_FIXED_NOW,
    )


async def test_request_count_limit_reads_request_counter(session: AsyncSession) -> None:
    redis = FakeRedis()
    account_id, token_id = uuid.uuid4(), uuid.uuid4()
    # Three requests, 15 tokens each.
    for _ in range(3):
        await _record(
            session, redis, account_id=account_id, token_id=token_id, total_tokens=15
        )

    spec = UsageLimitSpec(
        metric=LimitMetric.REQUEST_COUNT,
        limit_value=100,
        window=LimitWindow.DAILY,
    )
    consumption = await read_subject_consumption(
        redis, SUBJECT_ACCOUNT, account_id, spec, now=_FIXED_NOW
    )
    # Request counter, not the token counter.
    assert consumption == 3.0


async def test_token_count_limit_reads_token_counter(session: AsyncSession) -> None:
    redis = FakeRedis()
    account_id, token_id = uuid.uuid4(), uuid.uuid4()
    for _ in range(3):
        await _record(
            session, redis, account_id=account_id, token_id=token_id, total_tokens=15
        )

    spec = UsageLimitSpec(
        metric=LimitMetric.TOKEN_COUNT,
        limit_value=1000,
        window=LimitWindow.DAILY,
    )
    consumption = await read_subject_consumption(
        redis, SUBJECT_TOKEN, token_id, spec, now=_FIXED_NOW
    )
    assert consumption == 45.0


async def test_non_request_metrics_read_token_counter(session: AsyncSession) -> None:
    """cost_estimate and percentage both fall back to the token counter."""
    redis = FakeRedis()
    account_id, token_id = uuid.uuid4(), uuid.uuid4()
    await _record(
        session, redis, account_id=account_id, token_id=token_id, total_tokens=20
    )

    cost_spec = UsageLimitSpec(
        metric=LimitMetric.COST_ESTIMATE,
        limit_value=999,
        window=LimitWindow.DAILY,
    )
    pct_spec = UsageLimitSpec(
        metric=LimitMetric.PERCENTAGE,
        limit_value=80,
        capacity=100,
        window=LimitWindow.DAILY,
    )
    for spec in (cost_spec, pct_spec):
        consumption = await read_subject_consumption(
            redis, SUBJECT_ACCOUNT, account_id, spec, now=_FIXED_NOW
        )
        assert consumption == 20.0


async def test_no_limit_reports_cumulative_token_usage(session: AsyncSession) -> None:
    """spec=None reports cumulative token_count in the NONE bucket."""
    redis = FakeRedis()
    account_id, token_id = uuid.uuid4(), uuid.uuid4()
    for total in (15, 25):
        await _record(
            session,
            redis,
            account_id=account_id,
            token_id=token_id,
            total_tokens=total,
        )

    consumption = await read_subject_consumption(
        redis, SUBJECT_TOKEN, token_id, None, now=_FIXED_NOW
    )
    assert consumption == 40.0


async def test_absent_counters_read_as_zero(session: AsyncSession) -> None:
    """A subject with no recorded usage reports 0.0 for both the limit and no-limit
    paths."""
    redis = FakeRedis()
    unknown = uuid.uuid4()

    spec = UsageLimitSpec(
        metric=LimitMetric.TOKEN_COUNT, limit_value=10, window=LimitWindow.MONTHLY
    )
    assert (
        await read_subject_consumption(
            redis, SUBJECT_TOKEN, unknown, spec, now=_FIXED_NOW
        )
        == 0.0
    )
    assert (
        await read_subject_consumption(
            redis, SUBJECT_TOKEN, unknown, None, now=_FIXED_NOW
        )
        == 0.0
    )


async def test_window_is_respected(session: AsyncSession) -> None:
    """A daily-window limit reads only the current day's bucket, not the cumulative
    one."""
    redis = FakeRedis()
    account_id, token_id = uuid.uuid4(), uuid.uuid4()

    # Record on a previous day; it lands in a different daily bucket.
    prev_day = _FIXED_NOW.replace(day=_FIXED_NOW.day - 1)
    await record_usage(
        session,
        UsageEvent(
            correlation_id=uuid.uuid4(),
            client_token_id=token_id,
            account_id=account_id,
            provider="openai",
            prompt_tokens=50,
            completion_tokens=0,
            total_tokens=50,
        ),
        redis=redis,
        now=prev_day,
    )
    # Record today.
    await _record(
        session, redis, account_id=account_id, token_id=token_id, total_tokens=7
    )

    daily_spec = UsageLimitSpec(
        metric=LimitMetric.TOKEN_COUNT, limit_value=1000, window=LimitWindow.DAILY
    )
    today = await read_subject_consumption(
        redis, SUBJECT_TOKEN, token_id, daily_spec, now=_FIXED_NOW
    )
    assert today == 7.0

    # The cumulative (no-limit) figure sees both days.
    cumulative = await read_subject_consumption(
        redis, SUBJECT_TOKEN, token_id, None, now=_FIXED_NOW
    )
    assert cumulative == 57.0
