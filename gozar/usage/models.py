"""Usage_Recorder ORM models.

Owns the ``usage_`` and ``trace_`` tables (module prefixes per the convention in
:mod:`gozar.core.db`): the durable per-request metering record and the lightweight
request trace.

Tables
------
* :class:`UsageRecord` -- ``usage_record``: one row per completed proxied request,
  recording the Client_Token used, the Upstream_Credential used, the Provider, the
  provider-reported token counts (zero and flagged when the Provider reports none),
  and the request timestamp. ``created_at`` is indexed for the time-range queries
  used by analytics and counter rebuilds (Requirements 13.1, 13.2). This Postgres
  table is the durable source of truth from which the Redis consumption counters
  (Requirement 13.3) are rebuildable.
* :class:`TraceLog` -- ``trace_log``: one row per request keyed by its correlation
  identifier. Created with the inbound request metadata when the request arrives
  (Requirement 14.1) and updated with the selected credential, outcome, final status
  code, end timestamp, and outbound metadata when the response is returned
  (Requirements 14.2, 14.3). Metadata blobs are JSONB and never contain secrets.

This module is imported by the Alembic ``env.py`` so the tables register on
``Base.metadata`` for autogenerate/migrations.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    Uuid,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from gozar.core.db import Base

# Trace metadata is JSONB on PostgreSQL (production; see migration 0005) and falls
# back to the generic JSON type on SQLite so the service-layer tests can run against
# an in-memory database (the project's test convention). Production behaviour is
# unchanged -- PostgreSQL still uses JSONB.
_TraceMeta = JSONB().with_variant(JSON(), "sqlite")


class UsageRecord(Base):
    """A durable per-request metering record (``usage_record``).

    One row is written when the Proxy_Gateway completes a request (Requirement
    13.1). ``correlation_id`` links the row to its :class:`TraceLog`
    (Requirement 14.1). ``client_token_id`` and ``account_id`` reference the
    Client_Token and the Upstream_Credential actually used; both rows are retained
    even after revocation/soft-delete so usage history survives (no cascade).

    Token counts default to ``0``; when the Provider reports no token counts the row
    is stored with zero counts and ``provider_metering_missing`` set ``True``
    (Requirement 13.2). ``created_at`` is indexed because analytics and counter
    rebuilds query it by time range (Requirement 13.1).
    """

    __tablename__ = "usage_record"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    # Links this metering row to its trace_log entry (Requirement 14.1).
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )
    # The Client_Token used; row retained after revocation so history survives.
    client_token_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tok_client_token.id"),
        nullable=False,
        index=True,
    )
    # The Upstream_Credential actually used; row retained after soft-delete.
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("acct_upstream_credential.id"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    total_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # True when the Provider reported no token counts (Requirement 13.2).
    provider_metering_missing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    # Request timestamp; indexed for range queries (Requirement 13.1).
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"UsageRecord(id={self.id!r}, correlation_id={self.correlation_id!r}, "
            f"provider={self.provider!r}, total_tokens={self.total_tokens!r})"
        )


class TraceLog(Base):
    """A lightweight per-request trace (``trace_log``).

    Keyed by the request ``correlation_id`` (Requirement 14.1). The row is created
    with ``inbound_meta`` and ``started_at`` when the request arrives, then completed
    on response with ``account_id`` (the selected Upstream_Credential), ``outcome``,
    ``status_code``, ``ended_at``, and ``outbound_meta`` (Requirements 14.2, 14.3).
    The outbound columns are nullable because they are populated only once the
    request finishes. Metadata blobs carry no secrets.

    Recognised ``outcome`` values: ``success``, ``client_error``,
    ``all_fallbacks_failed``, ``no_account``.
    """

    __tablename__ = "trace_log"

    correlation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
    )
    # Inbound request metadata: method, model, stream flag, session id, sizes
    # (no secrets) (Requirement 14.1).
    inbound_meta: Mapped[dict] = mapped_column(_TraceMeta, nullable=False)
    # Selected Upstream_Credential; null until one is chosen / if none is
    # (Requirement 14.3). Not a hard FK so a trace is never lost to credential
    # lifecycle changes.
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    # Final outcome, set on response: success / client_error /
    # all_fallbacks_failed / no_account.
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    # Elapsed duration = ended_at - started_at (Requirement 14.3).
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Outbound response metadata: status, finish reason, sizes (no secrets)
    # (Requirement 14.2).
    outbound_meta: Mapped[dict | None] = mapped_column(_TraceMeta, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"TraceLog(correlation_id={self.correlation_id!r}, "
            f"outcome={self.outcome!r}, status_code={self.status_code!r})"
        )
