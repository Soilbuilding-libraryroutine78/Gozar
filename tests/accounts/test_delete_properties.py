"""Property-based tests for account deletion (Property 20).

These tests validate Property 20 from the Gozar design: for any Upstream_Credential
that has recorded usage, deleting it removes all stored secret material for that
credential (the subscription token bundle and/or API-key rows) while leaving its
credential row -- soft-deleted via ``deleted_at`` -- and its previously recorded
usage history intact for reporting (Requirement 5.3).

The function under test is :func:`gozar.accounts.service.delete`, exercised against
an in-memory fake session in the spirit of ``tests/accounts/test_connect.py``. The
fake interprets the two operations ``delete`` actually performs:

* ``session.get(UpstreamCredential, account_id)`` -- primary-key lookup, and
* ``session.execute(sa_delete(Model).where(Model.account_id == account_id))`` --
  a scoped hard-delete of secret rows,

against a small in-memory row store, so no real database is involved.

"Usage history" is modelled by an opaque :class:`_UsageRecord` stand-in row, since
the durable usage counters are owned by the Usage_Recorder (a later task) and have
no table in this module yet. The point of the property is that ``delete`` is scoped
to the secret tables only: anything that is not a subscription/API-key secret row
(the credential itself, the per-account usage limit, and recorded usage history)
must survive, while every secret row for the deleted account must be gone.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st
from sqlalchemy.sql.dml import Delete

from gozar.accounts import service
from gozar.accounts.models import (
    AccountUsageLimit,
    ApiKeySecret,
    CredentialKind,
    CredentialStatus,
    SubscriptionSecret,
    UpstreamCredential,
)
from gozar.core.errors import NotFound
from gozar.usage.limits import LimitMetric, LimitWindow


@dataclass
class _UsageRecord:
    """Opaque stand-in for a recorded usage-history row tied to an account.

    Not an ORM-mapped secret table, so :func:`service.delete` -- which only targets
    the subscription/API-key secret tables -- must never remove it. It models the
    "previously recorded usage history" that the design requires be retained.
    """

    account_id: uuid.UUID
    tokens: int


class _InMemorySession:
    """Minimal async session backed by an in-memory list of rows.

    Supports exactly the two operations :func:`service.delete` performs: a
    primary-key ``get`` for :class:`UpstreamCredential` and ``execute`` of a scoped
    SQLAlchemy ``DELETE`` statement. ``flush`` is a no-op (nothing to persist).
    """

    def __init__(self, rows: list[object]) -> None:
        self.rows: list[object] = list(rows)

    async def get(self, model: type, pk: uuid.UUID) -> object | None:
        for row in self.rows:
            if isinstance(row, model) and getattr(row, "id", None) == pk:
                return row
        return None

    async def execute(self, statement: object) -> None:
        if not isinstance(statement, Delete):
            raise NotImplementedError(
                f"fake session only handles DELETE, got {type(statement)!r}"
            )
        where = statement.whereclause
        # delete() always scopes by ``<Model>.account_id == <value>``.
        column_key = where.left.key
        target_value = where.right.value
        table_name = statement.table.name

        self.rows = [
            row
            for row in self.rows
            if not (
                getattr(getattr(row, "__table__", None), "name", None) == table_name
                and getattr(row, column_key, None) == target_value
            )
        ]
        return None

    async def flush(self) -> None:  # noqa: D401 - test stub
        return None

    # --- test helpers --------------------------------------------------------
    def secret_rows_for(self, account_id: uuid.UUID) -> list[object]:
        return [
            row
            for row in self.rows
            if isinstance(row, (SubscriptionSecret, ApiKeySecret))
            and row.account_id == account_id
        ]

    def usage_records_for(self, account_id: uuid.UUID) -> list[_UsageRecord]:
        return [
            row
            for row in self.rows
            if isinstance(row, _UsageRecord) and row.account_id == account_id
        ]


def _dummy_secret_columns() -> dict[str, bytes]:
    """Non-empty placeholder ciphertext columns (content is irrelevant here)."""
    return {"ciphertext": b"ct", "nonce": b"nz", "wrapped_dek": b"dek"}


def _build_account(
    *,
    kind: CredentialKind,
    usage_count: int,
    has_limit: bool,
) -> tuple[uuid.UUID, list[object]]:
    """Build one credential plus its secret row, usage history, and optional limit."""
    account_id = uuid.uuid4()
    rows: list[object] = [
        UpstreamCredential(
            id=account_id,
            provider="openai",
            kind=kind,
            label="acct",
            status=CredentialStatus.ACTIVE,
            provider_account_ref=None,
        )
    ]
    if kind is CredentialKind.SUBSCRIPTION:
        rows.append(SubscriptionSecret(account_id=account_id, **_dummy_secret_columns()))
    else:
        rows.append(ApiKeySecret(account_id=account_id, **_dummy_secret_columns()))

    rows.extend(_UsageRecord(account_id=account_id, tokens=i) for i in range(usage_count))

    if has_limit:
        rows.append(
            AccountUsageLimit(
                id=uuid.uuid4(),
                subject_kind="account",
                subject_id=account_id,
                metric=LimitMetric.TOKEN_COUNT,
                limit_value=1000,
                capacity=None,
                window=LimitWindow.NONE,
            )
        )
    return account_id, rows


# A handful of distinct accounts coexisting in the store, so we can also assert that
# deleting one account never touches another account's secrets or history.
_accounts = st.lists(
    st.builds(
        lambda kind, usage, has_limit: {
            "kind": kind,
            "usage_count": usage,
            "has_limit": has_limit,
        },
        kind=st.sampled_from(list(CredentialKind)),
        usage=st.integers(min_value=0, max_value=8),
        has_limit=st.booleans(),
    ),
    min_size=1,
    max_size=5,
)


# Feature: gozar, Property 20: Delete removes secrets but retains usage history
@hyp_settings(max_examples=200)
@given(specs=_accounts, target_index=st.integers(min_value=0))
def test_delete_removes_secrets_but_retains_history(
    specs: list[dict], target_index: int
) -> None:
    """Validates: Requirements 5.3.

    Deleting one Upstream_Credential hard-deletes that account's secret material
    (subscription bundle and/or API-key rows) while the credential row is retained
    and soft-deleted (``deleted_at`` set), and its recorded usage history is left
    intact. Other accounts are entirely unaffected.
    """
    account_ids: list[uuid.UUID] = []
    all_rows: list[object] = []
    usage_before: dict[uuid.UUID, int] = {}
    for spec in specs:
        account_id, rows = _build_account(
            kind=spec["kind"],
            usage_count=spec["usage_count"],
            has_limit=spec["has_limit"],
        )
        account_ids.append(account_id)
        all_rows.extend(rows)
        usage_before[account_id] = spec["usage_count"]

    session = _InMemorySession(all_rows)
    target_id = account_ids[target_index % len(account_ids)]

    # Precondition: the target has exactly one secret row before deletion.
    assert len(session.secret_rows_for(target_id)) == 1

    asyncio.run(service.delete(session, target_id))

    # Secret material for the deleted account is gone.
    assert session.secret_rows_for(target_id) == []

    # The credential row is retained and soft-deleted (deleted_at stamped).
    credential = asyncio.run(session.get(UpstreamCredential, target_id))
    assert credential is not None
    assert credential.deleted_at is not None

    # The deleted account's usage history is retained in full.
    assert len(session.usage_records_for(target_id)) == usage_before[target_id]

    # Every other account is untouched: secrets still present, history intact, and
    # the credential row not soft-deleted.
    for other_id in account_ids:
        if other_id == target_id:
            continue
        assert len(session.secret_rows_for(other_id)) == 1
        assert len(session.usage_records_for(other_id)) == usage_before[other_id]
        other = asyncio.run(session.get(UpstreamCredential, other_id))
        assert other is not None
        assert other.deleted_at is None


# Feature: gozar, Property 20: Delete removes secrets but retains usage history
@hyp_settings(max_examples=200)
@given(
    kind=st.sampled_from(list(CredentialKind)),
    usage_count=st.integers(min_value=0, max_value=8),
)
def test_delete_is_idempotent_then_refuses_second_delete(
    kind: CredentialKind, usage_count: int
) -> None:
    """Validates: Requirements 5.3.

    After a credential is deleted (secrets removed, row soft-deleted), a second
    delete of the same account is refused with :class:`NotFound` -- the soft-deleted
    row is treated as absent -- and the retained usage history is left untouched by
    the rejected attempt.
    """
    account_id, rows = _build_account(
        kind=kind, usage_count=usage_count, has_limit=False
    )
    session = _InMemorySession(rows)

    asyncio.run(service.delete(session, account_id))
    assert session.secret_rows_for(account_id) == []
    assert len(session.usage_records_for(account_id)) == usage_count

    # A deleted account cannot be deleted again; it is hidden as not found.
    try:
        asyncio.run(service.delete(session, account_id))
        raised = False
    except NotFound:
        raised = True
    assert raised is True

    # The rejected second delete changed nothing about the retained history.
    assert len(session.usage_records_for(account_id)) == usage_count
