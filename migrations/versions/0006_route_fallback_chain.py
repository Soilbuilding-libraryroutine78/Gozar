"""route_fallback_chain and route_fallback_chain_entry tables (Flow_Controller)

Adds the Fallback_Chain persistence tables owned by the ``gozar.routing`` module,
chaining after ``0005_usage_trace`` to keep a single linear history:

* ``route_fallback_chain`` -- one row per Fallback_Chain: an operator-facing ``name``
  and an optional ``model_selector`` used to resolve which chain serves a requested
  model (Requirements 10.1, 10.4).
* ``route_fallback_chain_entry`` -- one row per credential position within a chain.
  ``(chain_id, position)`` is unique and entries are read in ascending ``position``
  (Requirement 10.1). ``chain_id`` cascade-deletes with its chain. ``account_id``
  references ``acct_upstream_credential`` with the default (RESTRICT) action and is
  deliberately **not** cascade-deleted: credentials are soft-deleted (their row is
  retained), so an entry may reference a deleted credential, which the Flow_Controller
  skips at evaluation time and the Web_Console marks unavailable (Requirements 11.2,
  11.4).

Constraint/index names follow the project naming convention configured in
:mod:`gozar.core.db`.

Revision ID: 0006_route_fallback_chain
Revises: 0005_usage_trace
Create Date: 2024-01-01 00:00:05.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0006_route_fallback_chain"
down_revision: str | None = "0005_usage_trace"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "route_fallback_chain",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("model_selector", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_route_fallback_chain")),
    )

    op.create_table(
        "route_fallback_chain_entry",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("chain_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["chain_id"],
            ["route_fallback_chain.id"],
            name=op.f(
                "fk_route_fallback_chain_entry_chain_id_route_fallback_chain"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["acct_upstream_credential.id"],
            name=op.f(
                "fk_route_fallback_chain_entry_account_id_acct_upstream_credential"
            ),
        ),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_route_fallback_chain_entry")
        ),
        sa.UniqueConstraint(
            "chain_id",
            "position",
            name=op.f("uq_route_fallback_chain_entry_chain_id"),
        ),
    )
    op.create_index(
        op.f("ix_route_fallback_chain_entry_chain_id"),
        "route_fallback_chain_entry",
        ["chain_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_route_fallback_chain_entry_account_id"),
        "route_fallback_chain_entry",
        ["account_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_route_fallback_chain_entry_account_id"),
        table_name="route_fallback_chain_entry",
    )
    op.drop_index(
        op.f("ix_route_fallback_chain_entry_chain_id"),
        table_name="route_fallback_chain_entry",
    )
    op.drop_table("route_fallback_chain_entry")
    op.drop_table("route_fallback_chain")
