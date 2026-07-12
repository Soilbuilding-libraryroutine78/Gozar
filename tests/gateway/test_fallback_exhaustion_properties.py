"""Property-based test for fallback exhaustion producing a terminal error (Property 6).

Validates Property 6 from the Gozar design: for any fallback chain whose entries are
all unavailable, or any request whose every available attempt fails, the evaluated
attempt order is empty (or fully consumed) and the gateway returns a descriptive
terminal error (no available account, or all fallbacks failed) and makes no further
upstream attempts.

The pipeline (:func:`gozar.gateway.pipeline.complete_chat_completion`) is exercised
against a fresh in-memory SQLite database per example (the convention in this
package's ``conftest.py`` and in ``tests/usage/test_trace_roundtrip_properties.py``).
Because the pipeline is async and Hypothesis drives a synchronous test body, each
generated example runs inside its own short-lived event loop with its own engine.

Two seams are injected so no network is touched and call counts can be asserted
exactly:

* ``acquire_material`` -- returns in-memory credential material (reusing
  ``conftest.material_for``); it counts how many credentials the pipeline tried to
  acquire.
* ``upstream`` -- always raises :class:`UpstreamError`, simulating every upstream
  attempt failing; it counts how many upstream calls the pipeline made.

Together the two counters prove the two halves of the property: when no credential is
available the gateway raises ``NoAvailableAccount`` and makes zero upstream attempts;
when at least one is available it tries *exactly* the available subset (the attempt
order is fully consumed) and then raises ``UpstreamError`` ("all fallbacks failed").
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

# Register every ORM model on Base.metadata before create_all (the pipeline touches
# accounts, tokens, routing, and usage tables).
from gozar.accounts import models as _accounts_models  # noqa: F401
from gozar.routing import models as _routing_models  # noqa: F401
from gozar.tokens import models as _tokens_models  # noqa: F401
from gozar.usage import models as _usage_models  # noqa: F401

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from gozar.accounts.models import (
    CredentialKind,
    CredentialStatus,
    UpstreamCredential,
)
from gozar.core.config import Settings
from gozar.core.db import Base
from gozar.core.errors import NoAvailableAccount, UpstreamError
from gozar.gateway.pipeline import complete_chat_completion
from gozar.routing.service import create_chain
from gozar.tokens.service import create_token
from gozar.translation.types import OpenAIChatRequest

from conftest import FakeRedis, material_for

# A deterministic, well-formed 32-byte master key (base64) for envelope encryption,
# mirroring the package conftest's test settings.
import base64

_TEST_MASTER_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()

_MODEL = "gpt-4o"

# The ways a chain entry can be present-but-unavailable, plus "missing" (no row at
# all, which the snapshot treats as deleted) and "available". These cover every
# availability-gating condition the routing layer evaluates except limit-reached,
# which requires live Redis counters and is exercised by the routing/usage property
# suites; here unavailability is established structurally.
_AVAILABLE = "available"
_UNAVAILABLE_KINDS = ("missing", "deleted", "disabled", "reauth")
_ENTRY_KINDS = (_AVAILABLE, *_UNAVAILABLE_KINDS)


def _build_settings() -> Settings:
    """Test settings carrying the secret material and provider URLs the path needs."""
    return Settings(
        master_key=_TEST_MASTER_KEY,
        token_pepper="test-pepper",
        jwt_secret="test-jwt-secret",
        redis_url="redis://localhost:6379/0",
        provider_base_urls={"openai": "https://api.openai.com/v1"},
    )


def _make_credential(kind: str) -> UpstreamCredential | None:
    """Build an UpstreamCredential row for an entry kind, or ``None`` for 'missing'.

    'missing' returns ``None`` so the caller references an id that has no live row
    (the snapshot treats it as deleted/unavailable). Every other kind produces a
    persisted row whose status/soft-delete marker matches its availability.
    """
    if kind == "missing":
        return None
    status = CredentialStatus.ACTIVE
    deleted_at: datetime | None = None
    if kind == "disabled":
        status = CredentialStatus.DISABLED
    elif kind == "reauth":
        status = CredentialStatus.REQUIRES_REAUTH
    elif kind == "deleted":
        deleted_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return UpstreamCredential(
        id=uuid.uuid4(),
        provider="openai",
        kind=CredentialKind.API_KEY,
        label=f"cred-{kind}",
        status=status,
        deleted_at=deleted_at,
    )


async def _run_exhaustion(entry_kinds: list[str]) -> tuple[type | None, int, int, int]:
    """Seed a chain of the given entry kinds and run the pipeline to exhaustion.

    Returns ``(error_type, upstream_calls, acquire_calls, available_count)`` where
    ``error_type`` is the class of the raised terminal error (or ``None`` if the call
    unexpectedly returned), and the call counts record how many times the pipeline
    invoked the injected acquire/upstream seams. ``available_count`` is the number of
    entries that are genuinely available (present, active, not deleted).
    """
    settings = _build_settings()
    redis = FakeRedis()
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    upstream_calls = 0
    acquire_calls = 0
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        async with maker() as session:
            issued = await create_token(session, "exhaust-token", settings=settings)

            account_ids: list[uuid.UUID] = []
            available_count = 0
            for kind in entry_kinds:
                cred = _make_credential(kind)
                if cred is None:
                    # 'missing': reference an id with no live row.
                    account_ids.append(uuid.uuid4())
                    continue
                session.add(cred)
                account_ids.append(cred.id)
                if kind == _AVAILABLE:
                    available_count += 1
            await session.flush()

            await create_chain(
                session, "exhaust-chain", account_ids, model_selector=_MODEL
            )
            await session.commit()

            async def _fake_acquire(sess, account_id):
                nonlocal acquire_calls
                acquire_calls += 1
                return material_for(account_id, provider="openai")

            async def _fake_upstream(entry, material, adapter, body):
                nonlocal upstream_calls
                upstream_calls += 1
                # Every available credential fails -> drive the chain to exhaustion.
                raise UpstreamError("simulated upstream failure")

            request = OpenAIChatRequest(
                model=_MODEL, messages=[{"role": "user", "content": "hi"}]
            )

            error_type: type | None = None
            try:
                await complete_chat_completion(
                    session,
                    presented_token=issued.secret,
                    request=request,
                    redis=redis,
                    settings=settings,
                    upstream=_fake_upstream,
                    acquire_material=_fake_acquire,
                )
            except (NoAvailableAccount, UpstreamError) as exc:
                error_type = type(exc)

            return error_type, upstream_calls, acquire_calls, available_count
    finally:
        await engine.dispose()


# Feature: gozar, Property 6: For any fallback chain whose entries are all
# unavailable, or any request whose every available attempt fails, the evaluated
# attempt order is empty (or fully consumed) and the gateway returns a descriptive
# terminal error (no available account, or all fallbacks failed) and makes no further
# upstream attempts.
@hyp_settings(max_examples=100, deadline=None)
@given(
    entry_kinds=st.lists(
        st.sampled_from(_ENTRY_KINDS), min_size=0, max_size=8
    )
)
def test_fallback_exhaustion_produces_terminal_error(entry_kinds: list[str]) -> None:
    """Validates: Requirements 6.4, 10.3.

    With an upstream seam that always fails, a chain in which every entry is
    unavailable yields ``NoAvailableAccount`` with zero upstream attempts, while a
    chain with one or more available entries consumes exactly that available subset
    (one acquire + one upstream call each) before raising ``UpstreamError``.
    """
    error_type, upstream_calls, acquire_calls, available_count = asyncio.run(
        _run_exhaustion(entry_kinds)
    )

    # A terminal error is always raised; the pipeline never returns a response when
    # every attempt fails or none is available.
    assert error_type is not None

    if available_count == 0:
        # Attempt order is empty -> "no available account", and NO upstream attempt
        # (and no credential acquisition) is made (Requirement 6.4).
        assert error_type is NoAvailableAccount
        assert upstream_calls == 0
        assert acquire_calls == 0
    else:
        # Attempt order is fully consumed -> "all fallbacks failed": exactly one
        # acquire + one upstream call per available entry, then the terminal error
        # (Requirement 10.3). No further attempts beyond the available subset.
        assert error_type is UpstreamError
        assert upstream_calls == available_count
        assert acquire_calls == available_count


@pytest.mark.parametrize(
    "entry_kinds",
    [
        [],  # empty chain: no entries at all.
        ["disabled", "deleted", "reauth", "missing"],  # all unavailable.
    ],
)
def test_no_available_account_makes_no_upstream_attempt(entry_kinds: list[str]) -> None:
    """Concrete examples: a fully unavailable chain raises NoAvailableAccount (Req 6.4)."""
    error_type, upstream_calls, acquire_calls, available_count = asyncio.run(
        _run_exhaustion(entry_kinds)
    )
    assert available_count == 0
    assert error_type is NoAvailableAccount
    assert upstream_calls == 0
    assert acquire_calls == 0


def test_all_fallbacks_failed_consumes_every_available_entry() -> None:
    """Concrete example: three available entries all fail -> UpstreamError (Req 10.3)."""
    error_type, upstream_calls, acquire_calls, available_count = asyncio.run(
        _run_exhaustion(["available", "disabled", "available", "available"])
    )
    assert available_count == 3
    assert error_type is UpstreamError
    assert upstream_calls == 3
    assert acquire_calls == 3
