"""Property-based tests for counter/record consistency (Property 17).

Validates Property 17 from the Gozar design: for any sequence of recorded requests,
the Redis consumption counters that limit evaluation reads equal the aggregate of the
persisted ``usage_record`` rows for the corresponding subject and window
(Requirement 13.3).

Each generated request is metered through :func:`gozar.usage.service.record_usage`,
which both persists one durable row (the source of truth) and atomically advances the
consumption counters. The properties then assert the two halves agree:

* ``request_count`` counter == number of recorded rows for the subject/window, and
* ``token_count`` counter == sum of ``total_tokens`` over those rows,

for both subjects a Usage_Limit can attach to (the Upstream_Credential and the
Client_Token) and for every measurement window.

The persistence path runs against a fresh in-memory SQLite database and the counters
against a small in-memory Redis stand-in (the project's service-layer test
convention). A fresh engine + Redis stand-in is built per Hypothesis example so no
state bleeds between examples; the async body is driven with ``asyncio.run`` because
Hypothesis drives a synchronous test function.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from gozar.accounts.models import UpstreamCredential
from gozar.core.db import Base
from gozar.tokens.models import ClientToken
from gozar.usage.limits import LimitMetric, LimitWindow
from gozar.usage.models import UsageRecord
from gozar.usage.service import (
    SUBJECT_ACCOUNT,
    SUBJECT_TOKEN,
    UsageEvent,
    read_counter,
    record_usage,
)

# The metering table plus the two tables its foreign keys reference, so the schema
# resolves cleanly (mirrors tests/usage/test_usage_recording.py).
_TEST_TABLES = [
    UpstreamCredential.__table__,
    ClientToken.__table__,
    UsageRecord.__table__,
]

# A fixed instant so a single-instant sequence lands in one bucket per window.
_FIXED_NOW = datetime(2024, 6, 28, 21, 30, 15, tzinfo=timezone.utc)

# Small fixed pools so generated requests share subjects, exercising accumulation
# across many records for the same Client_Token / Upstream_Credential.
_POOL_SIZE = 3


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


async def _new_session(engine) -> AsyncSession:
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return maker()


async def _create_schema(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TEST_TABLES)


def _pool(n: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(n)]


# A single generated request: which token/credential it used, its token total, and
# whether the provider reported metering at all. Token/credential are referenced by
# index into per-example pools so requests share subjects.
_request = st.fixed_dictionaries(
    {
        "token_idx": st.integers(min_value=0, max_value=_POOL_SIZE - 1),
        "account_idx": st.integers(min_value=0, max_value=_POOL_SIZE - 1),
        "total_tokens": st.integers(min_value=0, max_value=10_000),
        "metering_missing": st.booleans(),
    }
)

_request_sequences = st.lists(_request, min_size=0, max_size=25)


def _make_event(
    req: dict, tokens: list[uuid.UUID], accounts: list[uuid.UUID]
) -> tuple[UsageEvent, int]:
    """Build a UsageEvent from a generated request; return it with its expected total.

    A request flagged ``metering_missing`` is recorded with all counts ``None`` (the
    provider reported nothing), which the service stores as zero total tokens.
    """
    token_id = tokens[req["token_idx"]]
    account_id = accounts[req["account_idx"]]
    if req["metering_missing"]:
        event = UsageEvent(
            correlation_id=uuid.uuid4(),
            client_token_id=token_id,
            account_id=account_id,
            provider="openai",
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
        )
        return event, 0
    total = req["total_tokens"]
    event = UsageEvent(
        correlation_id=uuid.uuid4(),
        client_token_id=token_id,
        account_id=account_id,
        provider="openai",
        total_tokens=total,
    )
    return event, total


# Feature: gozar, Property 17: Counter and record consistency
@hyp_settings(max_examples=150, deadline=None)
@given(requests=_request_sequences)
def test_counters_equal_recorded_row_aggregate_for_every_subject_and_window(
    requests: list[dict],
) -> None:
    """Validates: Requirements 13.3.

    Recording an arbitrary sequence of requests at a single instant leaves every
    consumption counter equal to the aggregate of the persisted usage rows for the
    corresponding subject and window: the ``request_count`` counter equals the number
    of rows for that subject and the ``token_count`` counter equals the sum of
    ``total_tokens`` over those rows. Because the sequence is recorded at one instant,
    every window (cumulative and each fixed/rolling period) holds the full aggregate.
    """

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        await _create_schema(engine)
        tokens = _pool(_POOL_SIZE)
        accounts = _pool(_POOL_SIZE)
        redis = FakeRedis()

        session = await _new_session(engine)
        try:
            for req in requests:
                event, _ = _make_event(req, tokens, accounts)
                await record_usage(session, event, redis=redis, now=_FIXED_NOW)
            await session.commit()

            # Aggregate the persisted rows (the durable source of truth) per subject.
            rows = (await session.scalars(select(UsageRecord))).all()
            token_counts: dict[uuid.UUID, list[int]] = defaultdict(lambda: [0, 0])
            account_counts: dict[uuid.UUID, list[int]] = defaultdict(lambda: [0, 0])
            for row in rows:
                tc = token_counts[row.client_token_id]
                tc[0] += 1
                tc[1] += row.total_tokens
                ac = account_counts[row.account_id]
                ac[0] += 1
                ac[1] += row.total_tokens

            subjects = (
                (SUBJECT_TOKEN, token_counts),
                (SUBJECT_ACCOUNT, account_counts),
            )
            for subject_kind, aggregate in subjects:
                for subject_id in set(tokens) | set(accounts):
                    expected_requests, expected_tokens = aggregate.get(
                        subject_id, [0, 0]
                    )
                    for window in LimitWindow:
                        requests_counter = await read_counter(
                            redis,
                            subject_kind,
                            subject_id,
                            LimitMetric.REQUEST_COUNT,
                            window,
                            now=_FIXED_NOW,
                        )
                        tokens_counter = await read_counter(
                            redis,
                            subject_kind,
                            subject_id,
                            LimitMetric.TOKEN_COUNT,
                            window,
                            now=_FIXED_NOW,
                        )
                        assert requests_counter == float(expected_requests)
                        assert tokens_counter == float(expected_tokens)
        finally:
            await session.close()
            await engine.dispose()

    asyncio.run(scenario())


# Feature: gozar, Property 17: Counter and record consistency
@hyp_settings(max_examples=100, deadline=None)
@given(
    requests=st.lists(
        st.fixed_dictionaries(
            {
                "day_offset": st.integers(min_value=0, max_value=4),
                "total_tokens": st.integers(min_value=0, max_value=10_000),
            }
        ),
        min_size=0,
        max_size=25,
    )
)
def test_windowed_counters_partition_records_by_period(
    requests: list[dict],
) -> None:
    """Validates: Requirements 13.3.

    When requests are spread across distinct days for a single subject, each DAILY
    counter equals the aggregate of only the rows recorded on that day, while the
    cumulative (NONE) counter equals the aggregate of every row. This shows the
    counters partition consumption by measurement window in step with the recorded
    rows.
    """

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        await _create_schema(engine)
        token_id = uuid.uuid4()
        account_id = uuid.uuid4()
        redis = FakeRedis()

        # Expected per-day aggregate (request count, token sum) and the grand total.
        per_day: dict[int, list[int]] = defaultdict(lambda: [0, 0])
        grand = [0, 0]

        session = await _new_session(engine)
        try:
            for req in requests:
                offset = req["day_offset"]
                total = req["total_tokens"]
                moment = _FIXED_NOW + timedelta(days=offset)
                event = UsageEvent(
                    correlation_id=uuid.uuid4(),
                    client_token_id=token_id,
                    account_id=account_id,
                    provider="openai",
                    total_tokens=total,
                )
                await record_usage(session, event, redis=redis, now=moment)
                per_day[offset][0] += 1
                per_day[offset][1] += total
                grand[0] += 1
                grand[1] += total
            await session.commit()

            # Each day's DAILY counter matches only that day's recorded rows.
            for subject_kind, subject_id in (
                (SUBJECT_TOKEN, token_id),
                (SUBJECT_ACCOUNT, account_id),
            ):
                for offset, (exp_requests, exp_tokens) in per_day.items():
                    moment = _FIXED_NOW + timedelta(days=offset)
                    daily_requests = await read_counter(
                        redis,
                        subject_kind,
                        subject_id,
                        LimitMetric.REQUEST_COUNT,
                        LimitWindow.DAILY,
                        now=moment,
                    )
                    daily_tokens = await read_counter(
                        redis,
                        subject_kind,
                        subject_id,
                        LimitMetric.TOKEN_COUNT,
                        LimitWindow.DAILY,
                        now=moment,
                    )
                    assert daily_requests == float(exp_requests)
                    assert daily_tokens == float(exp_tokens)

                # The cumulative window holds every recorded row.
                none_requests = await read_counter(
                    redis,
                    subject_kind,
                    subject_id,
                    LimitMetric.REQUEST_COUNT,
                    LimitWindow.NONE,
                    now=_FIXED_NOW,
                )
                none_tokens = await read_counter(
                    redis,
                    subject_kind,
                    subject_id,
                    LimitMetric.TOKEN_COUNT,
                    LimitWindow.NONE,
                    now=_FIXED_NOW,
                )
                assert none_requests == float(grand[0])
                assert none_tokens == float(grand[1])
        finally:
            await session.close()
            await engine.dispose()

    asyncio.run(scenario())
