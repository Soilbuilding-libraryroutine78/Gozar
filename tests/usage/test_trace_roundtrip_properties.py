"""Property-based tests for the Trace_Log open/finalize round-trip (Property 18).

Validates Property 18 from the Gozar design: for any inbound request metadata and
response outcome, opening a trace and then finalizing it yields a single Trace_Log
row whose stored inbound metadata round-trips intact, whose finalized fields (the
selected Upstream_Credential, the outcome, the final status, and the outbound
metadata) reflect exactly what was supplied, and whose elapsed duration equals
``ended_at - started_at`` (and is therefore non-negative whenever the response is
returned at or after the request arrived).

The persistence path runs against a fresh in-memory SQLite database, mirroring the
convention in ``test_trace.py``. Because :func:`open_trace` / :func:`finalize_trace`
are async, each generated example drives them inside a short-lived event loop with
its own engine, and the row is re-read from the database (after expiring the session
identity map) so the assertions exercise a genuine store/round-trip rather than the
in-memory object.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from gozar.core.db import Base
from gozar.usage.models import TraceLog
from gozar.usage.service import (
    TRACE_OUTCOMES,
    InboundMeta,
    OutboundMeta,
    finalize_trace,
    open_trace,
    trace_elapsed,
)

# Only the trace table is needed; it has no hard foreign keys.
_TEST_TABLES = [TraceLog.__table__]

# JSON- and SQLite-safe text: exclude surrogates (Cs) and control chars (Cc) so the
# generated metadata strings always round-trip through the JSON column intact.
_safe_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc")),
    max_size=40,
)

# Inbound metadata fields (mirrors InboundMeta; no secret-bearing field exists).
_inbound_strategy = st.builds(
    InboundMeta,
    method=st.sampled_from(["GET", "POST", "PUT", "PATCH", "DELETE"]),
    model=st.none() | _safe_text,
    stream=st.booleans(),
    session_id=st.none() | _safe_text,
    request_bytes=st.none() | st.integers(min_value=0, max_value=10_000_000),
)

# Outbound metadata fields (mirrors OutboundMeta).
_outbound_strategy = st.builds(
    OutboundMeta,
    outcome=st.sampled_from(TRACE_OUTCOMES),
    status_code=st.none() | st.integers(min_value=100, max_value=599),
    account_id=st.none() | st.uuids(),
    finish_reason=st.none() | _safe_text,
    response_bytes=st.none() | st.integers(min_value=0, max_value=10_000_000),
)

# Aware UTC start instant within a sane range, plus a non-negative elapsed delta so
# the response is returned at or after the request arrived (Requirement 14.2).
_started_strategy = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2100, 1, 1),
    timezones=st.just(timezone.utc),
)
_elapsed_strategy = st.timedeltas(
    min_value=timedelta(0), max_value=timedelta(hours=24)
)


async def _run_roundtrip(
    correlation_id: uuid.UUID,
    inbound: InboundMeta,
    outbound: OutboundMeta,
    started: datetime,
    ended: datetime,
) -> tuple[TraceLog, int, timedelta | None]:
    """Open + finalize a trace on a fresh in-memory DB and re-read the row.

    Returns the re-fetched row, the total trace-row count, and the derived elapsed
    duration. The session identity map is expired before the re-read so the returned
    row reflects what was actually persisted.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=_TEST_TABLES)
        maker = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        async with maker() as session:
            await open_trace(session, correlation_id, inbound, now=started)
            await finalize_trace(session, correlation_id, outbound, now=ended)

            # Force a genuine round-trip from the store rather than the cached object.
            session.expire_all()
            row = await session.get(TraceLog, correlation_id)
            assert row is not None
            count = len((await session.scalars(select(TraceLog))).all())
            elapsed = trace_elapsed(row)
            return row, count, elapsed
    finally:
        await engine.dispose()


# Feature: gozar, Property 18: Trace open/finalize round-trip
@hyp_settings(max_examples=150, deadline=None)
@given(
    correlation_id=st.uuids(),
    inbound=_inbound_strategy,
    outbound=_outbound_strategy,
    started=_started_strategy,
    elapsed=_elapsed_strategy,
)
def test_trace_open_finalize_roundtrip(
    correlation_id: uuid.UUID,
    inbound: InboundMeta,
    outbound: OutboundMeta,
    started: datetime,
    elapsed: timedelta,
) -> None:
    """Validates: Requirements 14.1, 14.2.

    For any inbound metadata and outcome, opening a trace then finalizing it yields a
    single trace row whose inbound metadata round-trips intact and whose finalized
    fields (selected credential, outcome, status, outbound metadata) reflect exactly
    what was supplied; the derived elapsed duration equals ``ended_at - started_at``
    and is non-negative.
    """
    ended = started + elapsed

    row, count, derived_elapsed = asyncio.run(
        _run_roundtrip(correlation_id, inbound, outbound, started, ended)
    )

    # Exactly one trace row exists for the correlation id (open + finalize touch one).
    assert count == 1
    assert row.correlation_id == correlation_id

    # Inbound metadata round-trips intact (Requirement 14.1).
    assert row.inbound_meta == inbound.as_meta()

    # Finalized fields reflect exactly what was supplied (Requirement 14.2).
    assert row.account_id == outbound.account_id
    assert row.outcome == outbound.outcome
    assert row.status_code == outbound.status_code
    assert row.outbound_meta == outbound.as_meta()

    # Elapsed = ended_at - started_at, and non-negative (Requirement 14.2/14.3).
    assert derived_elapsed == elapsed
    assert derived_elapsed >= timedelta(0)
