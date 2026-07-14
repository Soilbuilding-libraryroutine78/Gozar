"""separate chat and embeddings lanes inside each routing chain

Every existing node remains in the ``chat`` lane. Existing OpenAI and OpenRouter
nodes are also copied into an explicit ``embeddings`` lane to preserve the old
endpoint behavior. Both lanes have independent zero-based fallback positions while
the parent chain id remains stable.

Revision ID: 0011_chain_request_lanes
Revises: 0010_chain_node_models
Create Date: 2026-07-14 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence
import uuid

from alembic import op
import sqlalchemy as sa


revision: str = "0011_chain_request_lanes"
down_revision: str | None = "0010_chain_node_models"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "route_fallback_chain_entry",
        sa.Column(
            "route_kind",
            sa.Text(),
            nullable=False,
            server_default="chat",
        ),
    )
    op.drop_constraint(
        op.f("uq_route_fallback_chain_entry_chain_id"),
        "route_fallback_chain_entry",
        type_="unique",
    )
    op.create_unique_constraint(
        op.f("uq_route_fallback_chain_entry_chain_id"),
        "route_fallback_chain_entry",
        ["chain_id", "route_kind", "position"],
    )

    # Preserve the pre-lane embeddings behavior for existing installations. Before
    # this migration, Embeddings reused every OpenAI/OpenRouter node in the Chat
    # order and forwarded the inbound embedding model. Persist equivalent explicit
    # embedding nodes so the new routing rule is backwards-compatible.
    connection = op.get_bind()
    capable_rows = connection.execute(
        sa.text(
            """
            SELECT
                entry.chain_id,
                ROW_NUMBER() OVER (
                    PARTITION BY entry.chain_id
                    ORDER BY entry.position
                ) - 1 AS position,
                entry.account_id,
                entry.fallback_policy
            FROM route_fallback_chain_entry AS entry
            JOIN acct_upstream_credential AS account
                ON account.id = entry.account_id
            WHERE account.provider IN ('openai', 'openrouter')
            ORDER BY entry.chain_id, entry.position
            """
        )
    ).mappings().all()
    for row in capable_rows:
        connection.execute(
            sa.text(
                """
                INSERT INTO route_fallback_chain_entry (
                    id,
                    chain_id,
                    position,
                    account_id,
                    model_id,
                    fallback_policy,
                    route_kind
                ) VALUES (
                    :id,
                    :chain_id,
                    :position,
                    :account_id,
                    NULL,
                    :fallback_policy,
                    'embeddings'
                )
                """
            ),
            {
                "id": uuid.uuid4(),
                "chain_id": row["chain_id"],
                "position": row["position"],
                "account_id": row["account_id"],
                "fallback_policy": row["fallback_policy"],
            },
        )


def downgrade() -> None:
    # Preserve every node by flattening Chat first and Embeddings second before the
    # lane discriminator is removed. No chain or credential reference is deleted.
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY chain_id
                    ORDER BY
                        CASE WHEN route_kind = 'chat' THEN 0 ELSE 1 END,
                        position,
                        id
                ) - 1 AS new_position
            FROM route_fallback_chain_entry
        )
        UPDATE route_fallback_chain_entry AS entry
        SET position = ranked.new_position
        FROM ranked
        WHERE entry.id = ranked.id
        """
    )
    op.drop_constraint(
        op.f("uq_route_fallback_chain_entry_chain_id"),
        "route_fallback_chain_entry",
        type_="unique",
    )
    op.create_unique_constraint(
        op.f("uq_route_fallback_chain_entry_chain_id"),
        "route_fallback_chain_entry",
        ["chain_id", "position"],
    )
    op.drop_column("route_fallback_chain_entry", "route_kind")
