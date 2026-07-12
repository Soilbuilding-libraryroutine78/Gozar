"""store encrypted client token secrets for reveal

Adds nullable envelope-encryption columns to tok_client_token. New API keys store
their full presentable value encrypted at rest, allowing password-confirmed reveal
without rotating/revoking the token. Existing rows stay valid but unrecoverable.

Revision ID: 0009_token_secret_reveal
Revises: 0008_provider_model_catalog
Create Date: 2026-07-11 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0009_token_secret_reveal"
down_revision: str | None = "0008_provider_model_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tok_client_token",
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "tok_client_token",
        sa.Column("secret_nonce", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "tok_client_token",
        sa.Column("secret_wrapped_dek", sa.LargeBinary(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tok_client_token", "secret_wrapped_dek")
    op.drop_column("tok_client_token", "secret_nonce")
    op.drop_column("tok_client_token", "secret_ciphertext")
