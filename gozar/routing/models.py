"""Flow_Controller ORM models.

Owns the ``route_`` tables (module prefix per the convention in
:mod:`gozar.core.db`): the persisted Fallback_Chain and its ordered entries.

Tables
------
* :class:`RouteFallbackChain` -- ``route_fallback_chain``: one row per
  Fallback_Chain. Carries an operator-facing ``name`` and an optional
  ``model_selector`` used by :func:`gozar.routing.session.get_attempt_order` to pick
  the chain for a requested model (Requirements 10.1, 10.4).
* :class:`RouteFallbackChainEntry` -- ``route_fallback_chain_entry``: one row per
  credential position within a request lane. ``(chain_id, route_kind, position)`` is
  unique and entries are read in lane order to produce each attempt order
  (Requirement 10.1). ``account_id`` references an Upstream_Credential but is **not**
  cascade-deleted with it: a credential is soft-deleted (its row is retained with
  ``deleted_at`` set), so an entry may legitimately reference a deleted credential;
  the Flow_Controller skips such entries at evaluation time and the Web_Console marks
  them unavailable (Requirements 11.2, 11.4).

The pure, in-memory view of a persisted chain is
:class:`gozar.routing.chains.RoutingChain`; the mapping from these rows to that value
object lives in :mod:`gozar.routing.service` so this module stays free of logic.

This module is imported by the Alembic ``env.py`` so the tables register on
``Base.metadata`` for autogenerate/migrations.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from gozar.core.db import Base


class RouteFallbackChain(Base):
    """A persisted Fallback_Chain (``route_fallback_chain``).

    ``model_selector`` is an optional match used to resolve which chain serves a
    requested model; it does not affect ordering. The chain's ordered credentials
    live in :class:`RouteFallbackChainEntry` rows linked by ``chain_id``.
    """

    __tablename__ = "route_fallback_chain"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # Optional caller-owned idempotency key. UI-created chains leave it NULL; code
    # integrations use it to upsert a stable resource without remembering a UUID.
    client_key: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        unique=True,
        index=True,
    )
    model_selector: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"RouteFallbackChain(id={self.id!r}, name={self.name!r}, "
            f"model_selector={self.model_selector!r})"
        )


class RouteFallbackChainEntry(Base):
    """An ordered credential position within a chain (``route_fallback_chain_entry``).

    ``(chain_id, route_kind, position)`` is unique so each request lane has an
    independent fallback order. ``chain_id`` cascade-deletes with its chain, but
    ``account_id`` does **not** cascade with the referenced
    Upstream_Credential: credentials are soft-deleted (the row is retained), so an
    entry may point at a deleted credential, which the Flow_Controller skips at
    evaluation time (Requirements 11.2, 11.4).
    """

    __tablename__ = "route_fallback_chain_entry"
    __table_args__ = (
        UniqueConstraint("chain_id", "route_kind", "position"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    chain_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("route_fallback_chain.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Ordering within one request lane; unique with chain_id + route_kind.
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    # ``chat`` serves Chat Completions; ``embeddings`` serves Embeddings. Existing
    # rows migrate to ``chat`` so deployed LLM routes retain their behavior.
    route_kind: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="chat",
        server_default="chat",
    )
    # References an Upstream_Credential; intentionally no ON DELETE CASCADE so a
    # soft-deleted credential leaves the entry in place to be skipped (Req 11.2/11.4).
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("acct_upstream_credential.id"),
        nullable=False,
        index=True,
    )
    # Provider-specific model for this attempt. NULL means "use the inbound request
    # model", preserving every chain created before per-node model routing existed.
    model_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Policy for the edge to the next node. NULL keeps the historical behavior where
    # every typed provider failure advances to the next available credential.
    fallback_policy: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"RouteFallbackChainEntry(chain_id={self.chain_id!r}, "
            f"position={self.position!r}, account_id={self.account_id!r})"
        )
