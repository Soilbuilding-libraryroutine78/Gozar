"""Unit tests for the Usage_Recorder metering service (task 10.2).

Covers :func:`gozar.usage.service.record_usage` and its supporting counter helpers:
persisting one ``usage_record`` row per request (Requirement 13.1), the
missing-provider-metering flag (Requirement 13.2), atomic increment of the Redis
consumption counters for both the credential and the token (Requirement 13.3), the
window-bucket keying, and the no-secrets-in-keys guarantee (Requirement 16.4).

The durable persistence path runs against a fresh in-memory SQLite database (the
project's convention for service-layer tests), and the counters run against a small
in-memory Redis stand-in that implements exactly the ``pipeline``/``incrby``/
``expire``/``get`` surface the service uses.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from gozar.accounts.models import UpstreamCredential
from gozar.core.db import Base
from gozar.core.errors import ValidationError
from gozar.tokens.models import ClientToken
from gozar.usage.limits import LimitMetric, LimitWindow
from gozar.usage.models import UsageRecord
from gozar.usage.service import (
    SUBJECT_ACCOUNT,
    SUBJECT_TOKEN,
    UsageEvent,
    counter_key,
    read_counter,
    record_usage,
    window_bucket,
)

# The metering table plus the two tables its foreign keys reference, so the schema
# resolves cleanly. SQLite does not enforce the FKs, but they must exist in metadata.
_TEST_TABLES = [
    UpstreamCredential.__table__,
    ClientToken.__table__,
    UsageRecord.__table__,
]

# A fixed instant so window buckets and keys are deterministic in assertions.
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


class _FakePipeline:
    """Collects increments/expirations and applies them on :meth:`execute`."""

    def __init__(self, redis: "FakeRedis") -> None:
        self._redis = redis
        self._ops: list[tuple] = []

    def incrby(self, key: str, amount: int) -> "_FakePipeline":
        self._ops.append(("incrby", key, amount))
        return self

    def expire(self, key: str, ttl: int) -> "_FakePipeline":
        self._ops.append(("expire", key, ttl))
        return self

    async def execute(self) -> list:
        results: list = []
        for op in self._ops:
            if op[0] == "incrby":
                _, key, amount = op
                self._redis.store[key] = self._redis.store.get(key, 0) + amount
                results.append(self._redis.store[key])
            else:  # expire
                _, key, ttl = op
                self._redis.expires[key] = ttl
                results.append(True)
        self._ops.clear()
        return results


class FakeRedis:
    """Minimal in-memory async stand-in for the redis.asyncio subset used here.

    Stores counter values as ints and returns them as strings on ``get`` to mirror a
    ``decode_responses=True`` client. Supports a transactional pipeline of
    ``incrby``/``expire`` so the atomic-increment path is exercised for real.
    """

    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.expires: dict[str, int] = {}

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self)

    async def get(self, key: str) -> str | None:
        value = self.store.get(key)
        return None if value is None else str(value)


def _event(**overrides) -> UsageEvent:
    """Build a UsageEvent with sensible defaults, overridable per test."""
    defaults = dict(
        correlation_id=uuid.uuid4(),
        client_token_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        provider="openai",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
    )
    defaults.update(overrides)
    return UsageEvent(**defaults)


# --- persistence (Requirement 13.1) ------------------------------------------


async def test_record_usage_persists_one_row_with_reported_counts(
    session: AsyncSession,
) -> None:
    redis = FakeRedis()
    event = _event()

    record = await record_usage(session, event, redis=redis, now=_FIXED_NOW)

    rows = (await session.scalars(select(UsageRecord))).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.id == record.id
    assert row.correlation_id == event.correlation_id
    assert row.client_token_id == event.client_token_id
    assert row.account_id == event.account_id
    assert row.provider == "openai"
    assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (10, 5, 15)
    assert row.provider_metering_missing is False
    assert row.created_at == _FIXED_NOW


# --- missing metering (Requirement 13.2) -------------------------------------


async def test_missing_metering_stores_zero_counts_and_flag(
    session: AsyncSession,
) -> None:
    redis = FakeRedis()
    event = _event(prompt_tokens=None, completion_tokens=None, total_tokens=None)

    record = await record_usage(session, event, redis=redis, now=_FIXED_NOW)

    assert record.provider_metering_missing is True
    assert (record.prompt_tokens, record.completion_tokens, record.total_tokens) == (
        0,
        0,
        0,
    )


async def test_partial_counts_are_not_flagged_and_total_is_derived(
    session: AsyncSession,
) -> None:
    redis = FakeRedis()
    event = _event(prompt_tokens=7, completion_tokens=3, total_tokens=None)

    record = await record_usage(session, event, redis=redis, now=_FIXED_NOW)

    assert record.provider_metering_missing is False
    assert record.total_tokens == 10


async def test_negative_counts_are_rejected(session: AsyncSession) -> None:
    redis = FakeRedis()
    event = _event(prompt_tokens=-1, completion_tokens=0, total_tokens=None)

    with pytest.raises(ValidationError):
        await record_usage(session, event, redis=redis, now=_FIXED_NOW)


# --- counters (Requirement 13.3) ---------------------------------------------


async def test_counters_incremented_for_both_subjects(
    session: AsyncSession,
) -> None:
    redis = FakeRedis()
    event = _event(total_tokens=15)

    await record_usage(session, event, redis=redis, now=_FIXED_NOW)

    for subject_kind, subject_id in (
        (SUBJECT_ACCOUNT, event.account_id),
        (SUBJECT_TOKEN, event.client_token_id),
    ):
        requests = await read_counter(
            redis,
            subject_kind,
            subject_id,
            LimitMetric.REQUEST_COUNT,
            LimitWindow.DAILY,
            now=_FIXED_NOW,
        )
        tokens = await read_counter(
            redis,
            subject_kind,
            subject_id,
            LimitMetric.TOKEN_COUNT,
            LimitWindow.DAILY,
            now=_FIXED_NOW,
        )
        assert requests == 1.0
        assert tokens == 15.0


async def test_counters_accumulate_across_records(session: AsyncSession) -> None:
    redis = FakeRedis()
    token_id = uuid.uuid4()
    account_id = uuid.uuid4()

    for total in (15, 25):
        await record_usage(
            session,
            _event(
                client_token_id=token_id, account_id=account_id, total_tokens=total
            ),
            redis=redis,
            now=_FIXED_NOW,
        )

    requests = await read_counter(
        redis, SUBJECT_TOKEN, token_id, LimitMetric.REQUEST_COUNT, LimitWindow.NONE,
        now=_FIXED_NOW,
    )
    tokens = await read_counter(
        redis, SUBJECT_TOKEN, token_id, LimitMetric.TOKEN_COUNT, LimitWindow.NONE,
        now=_FIXED_NOW,
    )
    assert requests == 2.0
    assert tokens == 40.0


async def test_counter_incremented_in_every_window(session: AsyncSession) -> None:
    redis = FakeRedis()
    event = _event(total_tokens=8)

    await record_usage(session, event, redis=redis, now=_FIXED_NOW)

    for window in LimitWindow:
        value = await read_counter(
            redis,
            SUBJECT_TOKEN,
            event.client_token_id,
            LimitMetric.TOKEN_COUNT,
            window,
            now=_FIXED_NOW,
        )
        assert value == 8.0


async def test_windowed_counters_receive_cleanup_ttl(session: AsyncSession) -> None:
    redis = FakeRedis()
    event = _event()

    await record_usage(session, event, redis=redis, now=_FIXED_NOW)

    # The cumulative NONE window never expires; windowed buckets get a TTL.
    none_key = counter_key(
        SUBJECT_TOKEN,
        event.client_token_id,
        LimitMetric.REQUEST_COUNT,
        LimitWindow.NONE,
        now=_FIXED_NOW,
    )
    daily_key = counter_key(
        SUBJECT_TOKEN,
        event.client_token_id,
        LimitMetric.REQUEST_COUNT,
        LimitWindow.DAILY,
        now=_FIXED_NOW,
    )
    assert none_key not in redis.expires
    assert redis.expires[daily_key] > 0


# --- rolling 24h read ---------------------------------------------------------


async def test_read_counter_rolling_24h_sums_trailing_hourly_buckets(
    session: AsyncSession,
) -> None:
    redis = FakeRedis()
    token_id = uuid.uuid4()
    account_id = uuid.uuid4()

    # Two requests an hour apart fall in different hourly buckets.
    later = _FIXED_NOW
    earlier = _FIXED_NOW.replace(hour=_FIXED_NOW.hour - 1)
    for moment, total in ((earlier, 4), (later, 6)):
        await record_usage(
            session,
            _event(
                client_token_id=token_id, account_id=account_id, total_tokens=total
            ),
            redis=redis,
            now=moment,
        )

    rolling = await read_counter(
        redis,
        SUBJECT_TOKEN,
        token_id,
        LimitMetric.TOKEN_COUNT,
        LimitWindow.ROLLING_24H,
        now=later,
    )
    # Both buckets are within the trailing 24h of ``later``.
    assert rolling == 10.0


# --- keying & secret-safety ---------------------------------------------------


def test_window_bucket_labels_are_period_specific() -> None:
    assert window_bucket(LimitWindow.NONE, _FIXED_NOW) == "all"
    assert window_bucket(LimitWindow.DAILY, _FIXED_NOW) == "20240628"
    assert window_bucket(LimitWindow.MONTHLY, _FIXED_NOW) == "202406"
    assert window_bucket(LimitWindow.ROLLING_24H, _FIXED_NOW) == "2024062821"


def test_counter_key_format_contains_only_identifiers() -> None:
    token_id = uuid.uuid4()
    key = counter_key(
        SUBJECT_TOKEN,
        token_id,
        LimitMetric.TOKEN_COUNT,
        LimitWindow.DAILY,
        now=_FIXED_NOW,
    )
    assert key == f"usage:token:{token_id}:token_count:20240628"


async def test_no_secret_material_in_counter_keys(session: AsyncSession) -> None:
    redis = FakeRedis()
    event = _event(provider="anthropic")

    await record_usage(session, event, redis=redis, now=_FIXED_NOW)

    # Every key is composed solely of the namespace, subject kind/id, metric, and
    # bucket -- no provider name, token secret, or other sensitive value.
    allowed_ids = {str(event.account_id), str(event.client_token_id)}
    for key in redis.store:
        namespace, kind, subject_id, metric, _bucket = key.split(":")
        assert namespace == "usage"
        assert kind in {SUBJECT_ACCOUNT, SUBJECT_TOKEN}
        assert subject_id in allowed_ids
        assert metric in {"request_count", "token_count"}
