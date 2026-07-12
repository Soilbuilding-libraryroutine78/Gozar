"""Flow_Controller persistence: Fallback_Chain CRUD and chain mapping.

This module owns the database side of the Flow_Controller: creating, editing,
listing, and deleting persisted Fallback_Chains (the ``route_fallback_chain`` and
``route_fallback_chain_entry`` tables), plus mapping a persisted chain into the pure
:class:`gozar.routing.chains.RoutingChain` value object the Proxy_Gateway feeds to
:func:`gozar.routing.chains.evaluate_chain`.

Routing *decisions* stay pure and live in :mod:`gozar.routing.chains` /
:mod:`gozar.routing.state`; this module only persists and reads chains. All functions
operate on a caller-supplied :class:`~sqlalchemy.ext.asyncio.AsyncSession`.

A chain is stored as a parent row plus one entry row per credential, ordered by a
contiguous ``position`` starting at ``0`` (Requirement 10.1). Editing the entry order
replaces the chain's entries atomically (old rows deleted, new rows inserted in the
requested order) so the ``(chain_id, position)`` uniqueness constraint never sees a
transient collision and the updated chain applies to subsequent requests
(Requirement 10.4). Entries may reference soft-deleted credentials; persistence keeps
them and the pure evaluation layer skips them (Requirements 11.2, 11.4).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from gozar.core.errors import NotFound
from gozar.routing.chains import FallbackPolicy, RoutingChain, RoutingTarget
from gozar.routing.models import RouteFallbackChain, RouteFallbackChainEntry


class _Unset:
    """Sentinel type for an omitted optional argument (distinct from ``None``)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset>"


# Sentinel distinguishing an omitted ``model_selector`` from an explicit ``None`` in
# :func:`edit_chain` (``None`` clears the selector; omission leaves it unchanged).
_UNSET = _Unset()


@dataclass(frozen=True)
class ChainEntryInput:
    """One ordered chain node accepted by create/edit operations."""

    account_id: uuid.UUID
    model_id: str | None = None
    fallback_policy: FallbackPolicy = FallbackPolicy.ANY_ERROR


@dataclass(frozen=True)
class ChainEntryView:
    """A single ordered entry of a chain (no live credential state)."""

    account_id: uuid.UUID
    position: int
    model_id: str | None = None
    fallback_policy: FallbackPolicy = FallbackPolicy.ANY_ERROR


@dataclass(frozen=True)
class ChainView:
    """A read view of a persisted Fallback_Chain.

    ``entries`` is ordered by ascending ``position``. This view carries persisted
    structure only; whether each entry is currently *available* is decided by the
    pure evaluation layer against a live credential-state snapshot.
    """

    chain_id: uuid.UUID
    name: str
    client_key: str | None
    model_selector: str | None
    entries: tuple[ChainEntryView, ...]


def _normalize_entries(
    entries: Sequence[uuid.UUID | ChainEntryInput],
) -> tuple[ChainEntryInput, ...]:
    """Coerce legacy UUID-only entries into provider-aware chain nodes."""

    normalized: list[ChainEntryInput] = []
    for entry in entries:
        if isinstance(entry, ChainEntryInput):
            model_id = entry.model_id.strip() if entry.model_id else None
            normalized.append(
                ChainEntryInput(
                    entry.account_id,
                    model_id or None,
                    entry.fallback_policy,
                )
            )
        else:
            normalized.append(ChainEntryInput(entry))
    return tuple(normalized)


async def _load_chain(session: AsyncSession, chain_id: uuid.UUID) -> RouteFallbackChain:
    """Load a chain row by id or raise :class:`~gozar.core.errors.NotFound`."""
    chain = await session.get(RouteFallbackChain, chain_id)
    if chain is None:
        raise NotFound("fallback chain not found")
    return chain


async def _ordered_entries(
    session: AsyncSession, chain_id: uuid.UUID
) -> list[RouteFallbackChainEntry]:
    """Return a chain's entries ordered by ascending ``position`` (Requirement 10.1)."""
    result = await session.scalars(
        select(RouteFallbackChainEntry)
        .where(RouteFallbackChainEntry.chain_id == chain_id)
        .order_by(RouteFallbackChainEntry.position)
    )
    return list(result.all())


async def _replace_entries(
    session: AsyncSession,
    chain_id: uuid.UUID,
    entries: Sequence[uuid.UUID | ChainEntryInput],
) -> None:
    """Replace a chain's entries with ``account_ids`` at contiguous positions.

    Existing entries are deleted and flushed before the new rows are inserted so the
    ``(chain_id, position)`` unique constraint never observes a transient duplicate
    during a reorder.
    """
    await session.execute(
        delete(RouteFallbackChainEntry).where(
            RouteFallbackChainEntry.chain_id == chain_id
        )
    )
    # Flush the deletes before inserting so reused positions don't collide.
    await session.flush()
    for position, entry in enumerate(_normalize_entries(entries)):
        session.add(
            RouteFallbackChainEntry(
                chain_id=chain_id,
                position=position,
                account_id=entry.account_id,
                model_id=entry.model_id,
                fallback_policy=entry.fallback_policy.value,
            )
        )
    await session.flush()


def _to_view(
    chain: RouteFallbackChain, entries: Sequence[RouteFallbackChainEntry]
) -> ChainView:
    """Build a :class:`ChainView` from a chain row and its ordered entries."""
    return ChainView(
        chain_id=chain.id,
        name=chain.name,
        client_key=chain.client_key,
        model_selector=chain.model_selector,
        entries=tuple(
            ChainEntryView(
                account_id=entry.account_id,
                position=entry.position,
                model_id=entry.model_id,
                fallback_policy=FallbackPolicy(
                    entry.fallback_policy or FallbackPolicy.ANY_ERROR.value
                ),
            )
            for entry in entries
        ),
    )


async def create_chain(
    session: AsyncSession,
    name: str,
    account_ids: Sequence[uuid.UUID | ChainEntryInput],
    *,
    model_selector: str | None = None,
    client_key: str | None = None,
) -> ChainView:
    """Create a Fallback_Chain with ``account_ids`` as its ordered entries.

    Entries are assigned contiguous positions starting at ``0`` in the given order
    (Requirement 10.1). Returns the persisted chain as a :class:`ChainView`.
    """
    chain = RouteFallbackChain(
        name=name,
        model_selector=model_selector,
        client_key=client_key,
    )
    session.add(chain)
    # Flush so the chain id is assigned before entries reference it.
    await session.flush()
    await _replace_entries(session, chain.id, account_ids)
    entries = await _ordered_entries(session, chain.id)
    return _to_view(chain, entries)


async def upsert_chain_by_key(
    session: AsyncSession,
    client_key: str,
    name: str,
    account_ids: Sequence[uuid.UUID | ChainEntryInput],
    *,
    model_selector: str | None = None,
) -> ChainView:
    """Create or replace the stable chain resource identified by ``client_key``.

    Repeating the request with the same key returns the same chain id. Supplying a
    changed definition updates that resource atomically, which gives code integrations
    deterministic chain identity without content-based de-duplication surprises.
    """

    existing = await session.scalar(
        select(RouteFallbackChain).where(RouteFallbackChain.client_key == client_key)
    )
    if existing is None:
        return await create_chain(
            session,
            name,
            account_ids,
            model_selector=model_selector,
            client_key=client_key,
        )

    existing.name = name
    existing.model_selector = model_selector
    await _replace_entries(session, existing.id, account_ids)
    await session.flush()
    entries = await _ordered_entries(session, existing.id)
    return _to_view(existing, entries)


async def edit_chain(
    session: AsyncSession,
    chain_id: uuid.UUID,
    *,
    name: str | None = None,
    model_selector: str | None | _Unset = _UNSET,
    account_ids: Sequence[uuid.UUID | ChainEntryInput] | None = None,
) -> ChainView:
    """Edit a chain's name, model selector, and/or ordered entries.

    Only the provided fields change. When ``account_ids`` is supplied the chain's
    entries are replaced with the new order (reordering, adding, or removing entries);
    the updated chain applies to subsequent routing decisions (Requirement 10.4).
    Passing an empty ``account_ids`` clears the chain's entries.

    ``model_selector`` is only updated when it is passed, so an explicit ``None``
    clears it while omitting it leaves it unchanged.
    """
    chain = await _load_chain(session, chain_id)
    if name is not None:
        chain.name = name
    if not isinstance(model_selector, _Unset):
        chain.model_selector = model_selector
    if account_ids is not None:
        await _replace_entries(session, chain_id, account_ids)
    await session.flush()
    entries = await _ordered_entries(session, chain_id)
    return _to_view(chain, entries)


async def get_chain(session: AsyncSession, chain_id: uuid.UUID) -> ChainView:
    """Return a single chain as a :class:`ChainView` or raise ``NotFound``."""
    chain = await _load_chain(session, chain_id)
    entries = await _ordered_entries(session, chain_id)
    return _to_view(chain, entries)


async def list_chains(session: AsyncSession) -> list[ChainView]:
    """Return all chains (ordered by creation time) as :class:`ChainView`\\ s."""
    chains = (
        await session.scalars(
            select(RouteFallbackChain).order_by(RouteFallbackChain.created_at)
        )
    ).all()
    views: list[ChainView] = []
    for chain in chains:
        entries = await _ordered_entries(session, chain.id)
        views.append(_to_view(chain, entries))
    return views


async def delete_chain(session: AsyncSession, chain_id: uuid.UUID) -> None:
    """Delete a chain and its entries (entries cascade via ``chain_id``)."""
    chain = await _load_chain(session, chain_id)
    await session.delete(chain)
    await session.flush()


async def load_routing_chain(
    session: AsyncSession, chain_id: uuid.UUID
) -> RoutingChain:
    """Map a persisted chain into the pure :class:`RoutingChain` value object.

    Reads the chain's entries in ascending ``position`` and builds the ordered tuple
    of credential ids the Proxy_Gateway feeds to
    :func:`gozar.routing.chains.evaluate_chain`. This is the single seam between
    persistence and the pure routing logic (the evaluation layer never touches the
    database).
    """
    chain = await _load_chain(session, chain_id)
    entries = await _ordered_entries(session, chain_id)
    return RoutingChain.from_entries(
        [
            RoutingTarget(
                account_id=entry.account_id,
                model_id=entry.model_id,
                fallback_policy=FallbackPolicy(
                    entry.fallback_policy or FallbackPolicy.ANY_ERROR.value
                ),
                node_id=entry.id,
                position=entry.position,
            )
            for entry in entries
        ],
        chain_id=chain.id,
        model_selector=chain.model_selector,
    )


__all__ = [
    "ChainEntryInput",
    "ChainEntryView",
    "ChainView",
    "create_chain",
    "edit_chain",
    "get_chain",
    "list_chains",
    "delete_chain",
    "load_routing_chain",
    "upsert_chain_by_key",
]
