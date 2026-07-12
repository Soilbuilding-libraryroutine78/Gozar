"""Property-based test for usage-record completeness (Property 16).

Validates Property 16 from the Gozar design: for any completed proxied request,
:func:`gozar.usage.service.record_usage` persists exactly one ``usage_record`` row
capturing the Client_Token used, the Upstream_Credential used, the Provider, the
provider-reported token counts, and the request timestamp (Requirement 13.1); and
when the Provider reports no token counts (every count field unset) the row stores
zero counts and is flagged ``provider_metering_missing`` (Requirement 13.2).

The persistence path runs against a fresh in-memory SQLite database and a small
in-memory Redis stand-in -- the same pattern used by ``test_usage_recording.py`` --
exercised across many generated events by Hypothesis. Each generated example uses
its own engine so examples never share state.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

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
from gozar.usage.models import UsageRecord
from gozar.usage.service import UsageEvent, record_usage

# The metering table plus the two tables its foreign keys reference, so the schema
# resolves cleanly (SQLite does not enforce the FKs, but they must exist).
_TEST_TABLES = [
    UpstreamCredential.__table__,
    ClientToken.__table__,
    UsageRecord.__table__,
]


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
    """Minimal in-memory async stand-in for the redis.asyncio subset used here."""

    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.expires: dict[str, int] = {}

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self)

    async def get(self, key: str) -> str | None:
        value = self.store.get(key)
        return None if value is None else str(value)


async def _record_once(event: UsageEvent, now: datetime):
    """Record one event against a fresh in-memory DB; return (record, rows)."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=_TEST_TABLES)
        maker = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        async with maker() as s:
            record = await record_usage(s, event, redis=FakeRedis(), now=now)
            rows = (await s.scalars(select(UsageRecord))).all()
            return record, rows
    finally:
        await engine.dispose()


# Each token-count field is either unset (provider reported nothing for it) or a
# non-negative reported value. Negative counts are an explicit invalid-input case
# handled by a separate unit test, so they are excluded here.
_count = st.one_of(st.none(), st.integers(min_value=0, max_value=10**9))

# Realistic provider identifiers; the exact string is opaque to the recorder.
_providers = st.sampled_from(["openai", "anthropic", "openrouter", "codex"])

# Timezone-aware UTC instants; record_usage normalises to UTC, so created_at equals
# the instant we pass in.
_timestamps = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2035, 1, 1),
    timezones=st.just(timezone.utc),
)


# Feature: gozar, Property 16: Usage record completeness and missing-metering flag
@hyp_settings(max_examples=200, deadline=None)
@given(
    provider=_providers,
    prompt=_count,
    completion=_count,
    total=_count,
    now=_timestamps,
)
def test_usage_record_is_complete_and_flags_missing_metering(
    provider: str,
    prompt: int | None,
    completion: int | None,
    total: int | None,
    now: datetime,
) -> None:
    """Validates: Requirements 13.1, 13.2.

    For any completed request, exactly one usage row is persisted capturing the
    client token, credential, provider, token counts and timestamp. When the
    provider reports no token counts the row stores zero counts and is flagged
    ``provider_metering_missing``; otherwise the reported counts are stored
    (with a missing total derived from prompt + completion) and the flag is unset.
    """
    correlation_id = uuid.uuid4()
    client_token_id = uuid.uuid4()
    account_id = uuid.uuid4()
    event = UsageEvent(
        correlation_id=correlation_id,
        client_token_id=client_token_id,
        account_id=account_id,
        provider=provider,
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
    )

    record, rows = asyncio.run(_record_once(event, now))

    # Exactly one usage row is produced (Requirement 13.1).
    assert len(rows) == 1
    row = rows[0]
    assert row.id == record.id

    # The row captures the client token, credential, provider and timestamp.
    assert row.correlation_id == correlation_id
    assert row.client_token_id == client_token_id
    assert row.account_id == account_id
    assert row.provider == provider
    assert row.created_at == now

    # Missing metering: provider reported no counts at all (Requirement 13.2).
    metering_missing = prompt is None and completion is None and total is None
    assert row.provider_metering_missing is metering_missing

    if metering_missing:
        # Zero counts are stored and the row is flagged.
        assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (
            0,
            0,
            0,
        )
    else:
        # Reported counts are stored; an unset total is derived, unset parts zero.
        expected_prompt = prompt or 0
        expected_completion = completion or 0
        expected_total = (
            total if total is not None else expected_prompt + expected_completion
        )
        assert row.prompt_tokens == expected_prompt
        assert row.completion_tokens == expected_completion
        assert row.total_tokens == expected_total
