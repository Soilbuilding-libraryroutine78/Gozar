"""provider-specific chain node models and stable client keys

Adds two backwards-compatible routing fields:

* ``route_fallback_chain.client_key`` is an optional, unique caller-defined key used
  by the idempotent ``PUT /api/chains/by-key/{client_key}`` control-path endpoint.
* ``route_fallback_chain_entry.model_id`` is an optional provider model override for
  that attempt. ``NULL`` preserves the historical behavior and forwards the inbound
  request model unchanged.
* ``route_fallback_chain_entry.fallback_policy`` controls whether a failed attempt
  may continue to the next node. ``NULL`` preserves historical ``any_error`` behavior.

Revision ID: 0010_chain_node_models
Revises: 0009_token_secret_reveal
Create Date: 2026-07-11 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0010_chain_node_models"
down_revision: str | None = "0009_token_secret_reveal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "route_fallback_chain",
        sa.Column("client_key", sa.Text(), nullable=True),
    )
    op.create_index(
        op.f("ix_route_fallback_chain_client_key"),
        "route_fallback_chain",
        ["client_key"],
        unique=True,
    )
    op.add_column(
        "route_fallback_chain_entry",
        sa.Column("model_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "route_fallback_chain_entry",
        sa.Column("fallback_policy", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("route_fallback_chain_entry", "fallback_policy")
    op.drop_column("route_fallback_chain_entry", "model_id")
    op.drop_index(
        op.f("ix_route_fallback_chain_client_key"),
        table_name="route_fallback_chain",
    )
    op.drop_column("route_fallback_chain", "client_key")
