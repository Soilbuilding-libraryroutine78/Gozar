"""usage_record and trace_log tables (Usage_Recorder)

Adds the durable metering and lightweight tracing tables owned by the
``gozar.usage`` module, chaining after ``0004_tok_client_token`` to keep a single
linear history:

* ``usage_record`` -- one row per completed proxied request: the Client_Token used,
  the Upstream_Credential used, the Provider, provider-reported token counts (zero
  and flagged via ``provider_metering_missing`` when the Provider reports none), the
  correlation id linking the trace, and the request timestamp. ``created_at`` is
  indexed for the time-range queries used by analytics and counter rebuilds
  (Requirements 13.1, 13.2). The foreign keys to ``tok_client_token`` and
  ``acct_upstream_credential`` use the default (RESTRICT) action so usage history is
  retained when a token is revoked or a credential is soft-deleted.
* ``trace_log`` -- one row per request keyed by ``correlation_id``: inbound metadata
  and ``started_at`` recorded on arrival, then ``account_id``, ``outcome``,
  ``status_code``, ``ended_at`` and outbound metadata filled in on response. The
  outbound columns are nullable because they are populated only once the request
  finishes (Requirements 14.1, 14.2, 14.3). Metadata blobs are JSONB.

Constraint/index names follow the project naming convention configured in
:mod:`gozar.core.db`.

Revision ID: 0005_usage_trace
Revises: 0004_tok_client_token
Create Date: 2024-01-01 00:00:04.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005_usage_trace"
down_revision: str | None = "0004_tok_client_token"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "usage_record",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("correlation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("client_token_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column(
            "prompt_tokens", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "completion_tokens", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "total_tokens", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "provider_metering_missing",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["client_token_id"],
            ["tok_client_token.id"],
            name=op.f("fk_usage_record_client_token_id_tok_client_token"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["acct_upstream_credential.id"],
            name=op.f("fk_usage_record_account_id_acct_upstream_credential"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_record")),
    )
    op.create_index(
        op.f("ix_usage_record_correlation_id"),
        "usage_record",
        ["correlation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_usage_record_client_token_id"),
        "usage_record",
        ["client_token_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_usage_record_account_id"),
        "usage_record",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_usage_record_created_at"),
        "usage_record",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "trace_log",
        sa.Column("correlation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "inbound_meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "outbound_meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.PrimaryKeyConstraint("correlation_id", name=op.f("pk_trace_log")),
    )


def downgrade() -> None:
    op.drop_table("trace_log")
    op.drop_index(
        op.f("ix_usage_record_created_at"), table_name="usage_record"
    )
    op.drop_index(
        op.f("ix_usage_record_account_id"), table_name="usage_record"
    )
    op.drop_index(
        op.f("ix_usage_record_client_token_id"), table_name="usage_record"
    )
    op.drop_index(
        op.f("ix_usage_record_correlation_id"), table_name="usage_record"
    )
    op.drop_table("usage_record")
