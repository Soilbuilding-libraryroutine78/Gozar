"""tok_client_token and tok_usage_limit tables (Token_Authority)

Adds the ``tok_client_token`` and ``tok_usage_limit`` tables owned by the
``gozar.tokens`` module. Chains after ``0003_acct_credentials`` to keep a single
linear history (the accounts module added its migration on the same parent in
parallel work). Constraint/index names follow the project naming convention
configured in :mod:`gozar.core.db`.

``tok_client_token`` stores only a non-reversible representation of each token: a
non-secret ``id_prefix`` lookup key and an HMAC-SHA256 ``token_hash`` (Requirement
8.2). ``tok_usage_limit`` carries the uniform Usage_Limit shape attached to a token
(Requirement 9.1), with a unique ``subject_id`` so a token has at most one limit.

Revision ID: 0004_tok_client_token
Revises: 0003_acct_credentials
Create Date: 2024-01-01 00:00:03.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0004_tok_client_token"
down_revision: str | None = "0003_acct_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tok_client_token",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("id_prefix", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tok_client_token")),
        sa.UniqueConstraint(
            "id_prefix", name=op.f("uq_tok_client_token_id_prefix")
        ),
    )

    op.create_table(
        "tok_usage_limit",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("subject_kind", sa.String(length=16), nullable=False),
        sa.Column("subject_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("metric", sa.String(length=32), nullable=False),
        sa.Column("limit_value", sa.Numeric(), nullable=False),
        sa.Column("capacity", sa.Numeric(), nullable=True),
        sa.Column("window", sa.String(length=16), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tok_usage_limit")),
        sa.UniqueConstraint(
            "subject_id", name=op.f("uq_tok_usage_limit_subject_id")
        ),
    )


def downgrade() -> None:
    op.drop_table("tok_usage_limit")
    op.drop_table("tok_client_token")
