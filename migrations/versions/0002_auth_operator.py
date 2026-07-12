"""auth_operator table (Auth_Service operators)

Adds the ``auth_operator`` table owned by the ``gozar.auth`` module. Builds on the
empty baseline (``0001_baseline``). Constraint/index names follow the project naming
convention configured in :mod:`gozar.core.db`.

Revision ID: 0002_auth_operator
Revises: 0001_baseline
Create Date: 2024-01-01 00:00:01.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0002_auth_operator"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_operator",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_operator")),
        sa.UniqueConstraint("username", name=op.f("uq_auth_operator_username")),
    )


def downgrade() -> None:
    op.drop_table("auth_operator")
