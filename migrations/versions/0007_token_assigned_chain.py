"""assign fallback chains to client tokens

Adds an optional ``assigned_chain_id`` column to ``tok_client_token``. When set,
the Proxy_Gateway routes requests authenticated by that Client_Token through the
selected Fallback_Chain before falling back to model-selector auto routing.

Revision ID: 0007_token_assigned_chain
Revises: 0006_route_fallback_chain
Create Date: 2026-07-09 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0007_token_assigned_chain"
down_revision: str | None = "0006_route_fallback_chain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tok_client_token",
        sa.Column("assigned_chain_id", sa.Uuid(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_tok_client_token_assigned_chain_id_route_fallback_chain"),
        "tok_client_token",
        "route_fallback_chain",
        ["assigned_chain_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_tok_client_token_assigned_chain_id"),
        "tok_client_token",
        ["assigned_chain_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_tok_client_token_assigned_chain_id"),
        table_name="tok_client_token",
    )
    op.drop_constraint(
        op.f("fk_tok_client_token_assigned_chain_id_route_fallback_chain"),
        "tok_client_token",
        type_="foreignkey",
    )
    op.drop_column("tok_client_token", "assigned_chain_id")
