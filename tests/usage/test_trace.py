"""Unit tests for the Usage_Recorder request-tracing service (task 10.3).

Covers :func:`gozar.usage.service.open_trace` and
:func:`gozar.usage.service.finalize_trace` together with their
:class:`~gozar.usage.service.InboundMeta` / :class:`~gozar.usage.service.OutboundMeta`
value objects:

* opening a Trace_Log with inbound metadata and ``started_at`` (Requirement 14.1);
* finalizing it with the selected credential, outcome, status, ``ended_at``, and
  outbound metadata (Requirement 14.2);
* deriving the elapsed duration from ``ended_at - started_at`` (Requirement 14.3);
* never storing secret material in the metadata blobs (Requirement 16.4).

The persistence path runs against a fresh in-memory SQLite database, mirroring the
convention in ``test_usage_recording.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from gozar.core.db import Base
from gozar.core.errors import NotFound, ValidationError
from gozar.usage.models import TraceLog
from gozar.usage.service import (
    TRACE_OUTCOMES,
    CredentialTraceSnapshot,
    InboundMeta,
    OutboundMeta,
    finalize_trace,
    open_trace,
    trace_elapsed,
)

# Only the trace table is needed; it has no hard foreign keys.
_TEST_TABLES = [TraceLog.__table__]

_STARTED = datetime(2024, 6, 28, 21, 30, 15, tzinfo=timezone.utc)
_ENDED = datetime(2024, 6, 28, 21, 30, 17, 500000, tzinfo=timezone.utc)


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


def _inbound(**overrides) -> InboundMeta:
    defaults = dict(
        method="POST",
        model="gpt-4o",
        stream=False,
        session_id="sess-123",
        request_bytes=512,
    )
    defaults.update(overrides)
    return InboundMeta(**defaults)


# --- open_trace (Requirement 14.1) -------------------------------------------


async def test_open_trace_persists_inbound_meta_and_started_at(
    session: AsyncSession,
) -> None:
    correlation_id = uuid.uuid4()

    trace = await open_trace(session, correlation_id, _inbound(), now=_STARTED)

    rows = (await session.scalars(select(TraceLog))).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.correlation_id == correlation_id
    assert row.started_at == _STARTED
    assert row.inbound_meta == {
        "method": "POST",
        "stream": False,
        "model": "gpt-4o",
        "session_id": "sess-123",
        "request_bytes": 512,
    }
    # Outbound fields stay null until finalize.
    assert row.account_id is None
    assert row.outcome is None
    assert row.status_code is None
    assert row.ended_at is None
    assert row.outbound_meta is None
    assert trace.correlation_id == correlation_id


async def test_open_trace_omits_unset_optional_inbound_fields(
    session: AsyncSession,
) -> None:
    correlation_id = uuid.uuid4()

    await open_trace(
        session,
        correlation_id,
        InboundMeta(method="POST", stream=True),
        now=_STARTED,
    )

    row = await session.get(TraceLog, correlation_id)
    assert row is not None
    assert row.inbound_meta == {"method": "POST", "stream": True}


# --- finalize_trace (Requirement 14.2) ---------------------------------------


async def test_finalize_trace_updates_outcome_and_outbound_meta(
    session: AsyncSession,
) -> None:
    correlation_id = uuid.uuid4()
    account_id = uuid.uuid4()
    await open_trace(session, correlation_id, _inbound(), now=_STARTED)

    trace = await finalize_trace(
        session,
        correlation_id,
        OutboundMeta(
            outcome="success",
            status_code=200,
            account_id=account_id,
            finish_reason="stop",
            response_bytes=1024,
        ),
        now=_ENDED,
    )

    assert trace.account_id == account_id
    assert trace.outcome == "success"
    assert trace.status_code == 200
    assert trace.ended_at == _ENDED
    assert trace.outbound_meta == {
        "status_code": 200,
        "finish_reason": "stop",
        "response_bytes": 1024,
    }
    # Open + finalize touch a single row.
    rows = (await session.scalars(select(TraceLog))).all()
    assert len(rows) == 1


async def test_finalize_trace_allows_no_account_outcome(
    session: AsyncSession,
) -> None:
    correlation_id = uuid.uuid4()
    await open_trace(session, correlation_id, _inbound(), now=_STARTED)

    trace = await finalize_trace(
        session,
        correlation_id,
        OutboundMeta(outcome="no_account", status_code=503),
        now=_ENDED,
    )

    assert trace.outcome == "no_account"
    assert trace.account_id is None
    assert trace.outbound_meta == {"status_code": 503}


async def test_finalize_trace_stores_non_secret_credential_snapshot(
    session: AsyncSession,
) -> None:
    correlation_id = uuid.uuid4()
    account_id = uuid.uuid4()
    await open_trace(session, correlation_id, _inbound(), now=_STARTED)

    trace = await finalize_trace(
        session,
        correlation_id,
        OutboundMeta(
            outcome="success",
            status_code=200,
            account_id=account_id,
            credential=CredentialTraceSnapshot(
                account_id=account_id,
                label="Primary OpenAI",
                provider="openai",
                kind="api_key",
                status="active",
            ),
        ),
        now=_ENDED,
    )

    assert trace.outbound_meta == {
        "status_code": 200,
        "selected_credential": {
            "account_id": str(account_id),
            "label": "Primary OpenAI",
            "provider": "openai",
            "kind": "api_key",
            "status": "active",
        },
    }


async def test_finalize_trace_rejects_unknown_outcome(session: AsyncSession) -> None:
    correlation_id = uuid.uuid4()
    await open_trace(session, correlation_id, _inbound(), now=_STARTED)

    with pytest.raises(ValidationError):
        await finalize_trace(
            session, correlation_id, OutboundMeta(outcome="bogus"), now=_ENDED
        )


async def test_finalize_trace_missing_correlation_raises_not_found(
    session: AsyncSession,
) -> None:
    with pytest.raises(NotFound):
        await finalize_trace(
            session, uuid.uuid4(), OutboundMeta(outcome="success"), now=_ENDED
        )


@pytest.mark.parametrize("outcome", TRACE_OUTCOMES)
async def test_finalize_trace_accepts_every_recognised_outcome(
    session: AsyncSession, outcome: str
) -> None:
    correlation_id = uuid.uuid4()
    await open_trace(session, correlation_id, _inbound(), now=_STARTED)

    trace = await finalize_trace(
        session, correlation_id, OutboundMeta(outcome=outcome), now=_ENDED
    )

    assert trace.outcome == outcome


# --- elapsed duration (Requirement 14.3) -------------------------------------


async def test_trace_elapsed_is_none_until_finalized(session: AsyncSession) -> None:
    correlation_id = uuid.uuid4()
    trace = await open_trace(session, correlation_id, _inbound(), now=_STARTED)

    assert trace_elapsed(trace) is None


async def test_trace_elapsed_derived_from_timestamps(session: AsyncSession) -> None:
    correlation_id = uuid.uuid4()
    await open_trace(session, correlation_id, _inbound(), now=_STARTED)
    trace = await finalize_trace(
        session, correlation_id, OutboundMeta(outcome="success"), now=_ENDED
    )

    assert trace_elapsed(trace) == timedelta(seconds=2, milliseconds=500)


# --- secret safety (Requirement 16.4) ----------------------------------------


def test_inbound_meta_carries_only_non_secret_keys() -> None:
    # Construct with every field populated; the rendered blob must only contain the
    # explicit, non-secret keys -- there is no field for auth material.
    meta = _inbound().as_meta()
    assert set(meta) <= {
        "method",
        "model",
        "stream",
        "session_id",
        "request_bytes",
        "chain_id",
    }


def test_outbound_meta_carries_only_non_secret_keys() -> None:
    meta = OutboundMeta(
        outcome="success",
        status_code=200,
        account_id=uuid.uuid4(),
        finish_reason="stop",
        response_bytes=1024,
    ).as_meta()
    # The credential is stored in its own column, not the blob, and the blob holds
    # only response-shape fields.
    assert set(meta) <= {
        "status_code",
        "finish_reason",
        "response_bytes",
        "routing",
    }


async def test_stored_metadata_contains_no_secret_values(
    session: AsyncSession,
) -> None:
    correlation_id = uuid.uuid4()
    await open_trace(session, correlation_id, _inbound(), now=_STARTED)
    await finalize_trace(
        session,
        correlation_id,
        OutboundMeta(outcome="success", status_code=200, finish_reason="stop"),
        now=_ENDED,
    )

    row = await session.get(TraceLog, correlation_id)
    assert row is not None
    serialised = f"{row.inbound_meta}{row.outbound_meta}".lower()
    for forbidden in (
        "authorization",
        "bearer",
        "secret",
        "api_key",
        "access_token",
        "refresh_token",
    ):
        assert forbidden not in serialised
