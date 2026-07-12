"""runtime provider model catalog

Adds an editable provider fallback model catalog. This lets operators update
provider model lists from the admin API/UI without restarting the backend.

Revision ID: 0008_provider_model_catalog
Revises: 0007_token_assigned_chain
Create Date: 2026-07-10 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0008_provider_model_catalog"
down_revision: str | None = "0007_token_assigned_chain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "core_provider_model_catalog",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model_ids", sa.JSON(), nullable=False),
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
        sa.PrimaryKeyConstraint("provider", name=op.f("pk_core_provider_model_catalog")),
    )


def downgrade() -> None:
    op.drop_table("core_provider_model_catalog")
