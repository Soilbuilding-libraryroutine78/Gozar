"""acct_* tables (Account_Manager upstream credentials, secrets, usage limits)

Adds the credential lifecycle and secret-storage tables owned by the
``gozar.accounts`` module, building on ``0002_auth_operator``:

* ``acct_upstream_credential`` -- one row per connected Subscription_Account or
  API_Key_Account, with a ``deleted_at`` soft-delete marker and an index on
  ``provider`` (Requirements 1.4, 3.3, 5.1, 5.3).
* ``acct_subscription_secret`` / ``acct_api_key_secret`` -- one-to-one
  envelope-encrypted secret rows (ciphertext, nonce, wrapped_dek) keyed by the
  credential id; deleted with the credential via ``ON DELETE CASCADE``
  (Requirements 1.2, 2.2, 3.2, 16.2).
* ``acct_usage_limit`` -- per-account Usage_Limit, column-shaped to map to/from
  ``gozar.usage.limits.UsageLimitSpec`` (Requirement 4.1).

Enum-typed columns (``kind``, ``status``, ``metric``, ``window``) are stored as
VARCHAR with a CHECK constraint (non-native enum) carrying the lowercase enum values.
Constraint/index names follow the project naming convention configured in
:mod:`gozar.core.db`.

Revision ID: 0003_acct_credentials
Revises: 0002_auth_operator
Create Date: 2024-01-01 00:00:02.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0003_acct_credentials"
down_revision: str | None = "0002_auth_operator"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Non-native enum types (VARCHAR + CHECK) storing lowercase enum values, matching the
# ORM models in gozar.accounts.models.
_kind_enum = sa.Enum(
    "subscription", "api_key", name="credentialkind", native_enum=False
)
_status_enum = sa.Enum(
    "active", "disabled", "requires_reauth", name="credentialstatus", native_enum=False
)
_metric_enum = sa.Enum(
    "request_count",
    "token_count",
    "cost_estimate",
    "percentage",
    name="limitmetric",
    native_enum=False,
)
_window_enum = sa.Enum(
    "none", "daily", "monthly", "rolling_24h", name="limitwindow", native_enum=False
)


def upgrade() -> None:
    op.create_table(
        "acct_upstream_credential",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("kind", _kind_enum, nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("status", _status_enum, nullable=False),
        sa.Column("provider_account_ref", sa.Text(), nullable=True),
        sa.Column(
            "connected_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("requires_reauth_reason", sa.Text(), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_acct_upstream_credential")),
    )
    op.create_index(
        op.f("ix_acct_upstream_credential_provider"),
        "acct_upstream_credential",
        ["provider"],
        unique=False,
    )

    op.create_table(
        "acct_subscription_secret",
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["acct_upstream_credential.id"],
            name=op.f(
                "fk_acct_subscription_secret_account_id_acct_upstream_credential"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "account_id", name=op.f("pk_acct_subscription_secret")
        ),
    )

    op.create_table(
        "acct_api_key_secret",
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["acct_upstream_credential.id"],
            name=op.f("fk_acct_api_key_secret_account_id_acct_upstream_credential"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("account_id", name=op.f("pk_acct_api_key_secret")),
    )

    op.create_table(
        "acct_usage_limit",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("subject_kind", sa.String(length=16), nullable=False),
        sa.Column("subject_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("metric", _metric_enum, nullable=False),
        sa.Column("limit_value", sa.Numeric(), nullable=False),
        sa.Column("capacity", sa.Numeric(), nullable=True),
        sa.Column("window", _window_enum, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["subject_id"],
            ["acct_upstream_credential.id"],
            name=op.f("fk_acct_usage_limit_subject_id_acct_upstream_credential"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_acct_usage_limit")),
    )
    op.create_index(
        op.f("ix_acct_usage_limit_subject_id"),
        "acct_usage_limit",
        ["subject_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_acct_usage_limit_subject_id"), table_name="acct_usage_limit"
    )
    op.drop_table("acct_usage_limit")
    op.drop_table("acct_api_key_secret")
    op.drop_table("acct_subscription_secret")
    op.drop_index(
        op.f("ix_acct_upstream_credential_provider"),
        table_name="acct_upstream_credential",
    )
    op.drop_table("acct_upstream_credential")
