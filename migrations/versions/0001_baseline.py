"""baseline (empty initial revision)

This is the baseline revision for Gozar's schema. It is intentionally empty: the
schema is built incrementally, with each domain module contributing its own
migration that adds module-prefixed tables (``core_``, ``acct_``, ``tok_``,
``route_``, ``usage_``, ``trace_``, ``auth_``) on top of this baseline.

Revision ID: 0001_baseline
Revises:
Create Date: 2024-01-01 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op  # noqa: F401  (available to future edits)
import sqlalchemy as sa  # noqa: F401  (available to future edits)

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op baseline; module migrations build on this revision."""
    pass


def downgrade() -> None:
    """No-op baseline."""
    pass
