"""Unit tests for Fallback_Chain CRUD persistence (task 9.1).

Exercises :mod:`gozar.routing.service` against a real in-memory async SQLite database
(no mocks): chains and their ordered entries are created, read, reordered, and
deleted through the real ORM, and a persisted chain is mapped into the pure
:class:`~gozar.routing.chains.RoutingChain` value object.

Only the three tables the service touches are created (the chain, its entries, and
the referenced ``acct_upstream_credential``); the rest of the schema is irrelevant
here. SQLite stores UUIDs as CHAR(32) via SQLAlchemy's ``Uuid`` type, which is enough
to drive the real query/insert/delete paths.

Covered behaviour:

* create persists entries in order with contiguous positions (Requirement 10.1);
* list/get return chains with entries in ascending position;
* edit reorders / replaces entries and updates name + model selector, applying to
  subsequent reads (Requirement 10.4);
* an entry may reference a soft-deleted credential and is retained (Requirements 11.2,
  11.4);
* ``(chain_id, position)`` is unique;
* ``load_routing_chain`` maps rows to the pure ``RoutingChain``;
* missing chains raise ``NotFound``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from gozar.accounts.models import CredentialKind, CredentialStatus, UpstreamCredential
from gozar.core.db import Base
from gozar.core.errors import NotFound
from gozar.routing.chains import FallbackPolicy, RoutingChain
from gozar.routing.models import RouteFallbackChain, RouteFallbackChainEntry
from gozar.routing.service import (
    ChainEntryInput,
    create_chain,
    delete_chain,
    edit_chain,
    get_chain,
    list_chains,
    load_routing_chain,
    upsert_chain_by_key,
)

# Only the tables the chain service reads/writes (plus the credential the entry FK
# points at). Keeps the fixture independent of Postgres-only types elsewhere.
_TEST_TABLES = [
    UpstreamCredential.__table__,
    RouteFallbackChain.__table__,
    RouteFallbackChainEntry.__table__,
]


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    """Yield a session backed by a fresh in-memory SQLite database."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TEST_TABLES)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _make_credential(
    session: AsyncSession,
    *,
    deleted: bool = False,
) -> uuid.UUID:
    """Insert an Upstream_Credential and return its id (optionally soft-deleted)."""
    credential = UpstreamCredential(
        provider="openai",
        kind=CredentialKind.API_KEY,
        label="test-account",
        status=CredentialStatus.ACTIVE,
        deleted_at=datetime.now(timezone.utc) if deleted else None,
    )
    session.add(credential)
    await session.flush()
    return credential.id


# --- create -------------------------------------------------------------------

async def test_create_chain_persists_entries_in_order(session: AsyncSession) -> None:
    a = await _make_credential(session)
    b = await _make_credential(session)
    c = await _make_credential(session)

    view = await create_chain(
        session,
        "primary",
        [
            ChainEntryInput(a, "gpt-5.4-mini", FallbackPolicy.AUTH_OR_RETRYABLE),
            ChainEntryInput(b, "google/gemini-2.5-flash"),
            ChainEntryInput(c),
        ],
        model_selector="gpt-4o",
    )

    assert view.name == "primary"
    assert view.model_selector == "gpt-4o"
    assert [e.account_id for e in view.entries] == [a, b, c]
    assert [e.position for e in view.entries] == [0, 1, 2]
    assert [e.model_id for e in view.entries] == [
        "gpt-5.4-mini",
        "google/gemini-2.5-flash",
        None,
    ]
    assert view.entries[0].fallback_policy is FallbackPolicy.AUTH_OR_RETRYABLE


async def test_create_empty_chain_has_no_entries(session: AsyncSession) -> None:
    view = await create_chain(session, "empty", [])
    assert view.entries == ()


# --- read ---------------------------------------------------------------------

async def test_get_chain_returns_entries_ascending(session: AsyncSession) -> None:
    a = await _make_credential(session)
    b = await _make_credential(session)
    created = await create_chain(session, "chain", [a, b])

    fetched = await get_chain(session, created.chain_id)

    assert fetched.chain_id == created.chain_id
    assert [e.account_id for e in fetched.entries] == [a, b]


async def test_list_chains_returns_all(session: AsyncSession) -> None:
    a = await _make_credential(session)
    await create_chain(session, "first", [a])
    await create_chain(session, "second", [a])

    chains = await list_chains(session)

    assert {c.name for c in chains} == {"first", "second"}


async def test_upsert_by_client_key_reuses_chain_identity(session: AsyncSession) -> None:
    a = await _make_credential(session)
    b = await _make_credential(session)

    created = await upsert_chain_by_key(
        session,
        "support-production",
        "Support",
        [ChainEntryInput(a, "gpt-5.4-mini")],
    )
    updated = await upsert_chain_by_key(
        session,
        "support-production",
        "Support v2",
        [ChainEntryInput(b, "google/gemini-2.5-flash")],
    )

    assert updated.chain_id == created.chain_id
    assert updated.client_key == "support-production"
    assert updated.name == "Support v2"
    assert [(entry.account_id, entry.model_id) for entry in updated.entries] == [
        (b, "google/gemini-2.5-flash")
    ]
    assert len(await list_chains(session)) == 1


async def test_get_missing_chain_raises_not_found(session: AsyncSession) -> None:
    with pytest.raises(NotFound):
        await get_chain(session, uuid.uuid4())


# --- edit / reorder -----------------------------------------------------------

async def test_edit_reorders_entries(session: AsyncSession) -> None:
    a = await _make_credential(session)
    b = await _make_credential(session)
    c = await _make_credential(session)
    created = await create_chain(session, "chain", [a, b, c])

    edited = await edit_chain(session, created.chain_id, account_ids=[c, a, b])

    assert [e.account_id for e in edited.entries] == [c, a, b]
    assert [e.position for e in edited.entries] == [0, 1, 2]
    # Re-reading reflects the new order (applies to subsequent requests).
    refetched = await get_chain(session, created.chain_id)
    assert [e.account_id for e in refetched.entries] == [c, a, b]


async def test_edit_can_add_and_remove_entries(session: AsyncSession) -> None:
    a = await _make_credential(session)
    b = await _make_credential(session)
    created = await create_chain(session, "chain", [a, b])

    edited = await edit_chain(session, created.chain_id, account_ids=[b])
    assert [e.account_id for e in edited.entries] == [b]

    cleared = await edit_chain(session, created.chain_id, account_ids=[])
    assert cleared.entries == ()


async def test_edit_updates_name_and_selector(session: AsyncSession) -> None:
    a = await _make_credential(session)
    created = await create_chain(session, "old", [a], model_selector="gpt-4o")

    edited = await edit_chain(
        session, created.chain_id, name="new", model_selector=None
    )
    assert edited.name == "new"
    assert edited.model_selector is None


async def test_edit_without_selector_keyword_leaves_it_unchanged(
    session: AsyncSession,
) -> None:
    a = await _make_credential(session)
    created = await create_chain(session, "chain", [a], model_selector="gpt-4o")

    edited = await edit_chain(session, created.chain_id, name="renamed")
    assert edited.model_selector == "gpt-4o"


async def test_edit_missing_chain_raises_not_found(session: AsyncSession) -> None:
    with pytest.raises(NotFound):
        await edit_chain(session, uuid.uuid4(), name="x")


# --- deleted-credential reference ---------------------------------------------

async def test_entry_may_reference_soft_deleted_credential(
    session: AsyncSession,
) -> None:
    live = await _make_credential(session)
    gone = await _make_credential(session, deleted=True)

    view = await create_chain(session, "chain", [live, gone])

    # The entry for the soft-deleted credential is retained (Req 11.2/11.4); the pure
    # evaluation layer is what skips it later.
    assert [e.account_id for e in view.entries] == [live, gone]


# --- uniqueness ---------------------------------------------------------------

async def test_chain_id_position_is_unique(session: AsyncSession) -> None:
    a = await _make_credential(session)
    created = await create_chain(session, "chain", [a])

    # Force a duplicate (chain_id, position) and expect the constraint to reject it.
    session.add(
        RouteFallbackChainEntry(
            chain_id=created.chain_id, position=0, account_id=a
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


# --- mapping to the pure value object -----------------------------------------

async def test_load_routing_chain_maps_to_value_object(
    session: AsyncSession,
) -> None:
    a = await _make_credential(session)
    b = await _make_credential(session)
    created = await create_chain(
        session, "chain", [a, b], model_selector="claude-3-5"
    )

    routing = await load_routing_chain(session, created.chain_id)

    assert isinstance(routing, RoutingChain)
    assert [target.account_id for target in routing.entries] == [a, b]
    assert routing.chain_id == created.chain_id
    assert routing.model_selector == "claude-3-5"


# --- delete -------------------------------------------------------------------

async def test_delete_chain_removes_it(session: AsyncSession) -> None:
    a = await _make_credential(session)
    created = await create_chain(session, "chain", [a])

    await delete_chain(session, created.chain_id)

    with pytest.raises(NotFound):
        await get_chain(session, created.chain_id)
