"""Property-based tests for the token status authorization gate (Property 12).

These tests validate Property 12 from the Gozar design: for any client token and
status, ``Token_Authority.verify`` authorizes a presented token *if and only if* the
token is active (enabled and not revoked); a disabled or revoked token is always
rejected; and re-enabling a disabled token restores acceptance (Requirements 8.4,
9.3, 9.4).

The token lifecycle functions operate on an ``AsyncSession``. To exercise the real
issuance / verification / lifecycle code paths without a live database, these tests
drive them through a tiny in-memory fake of the async session that supports exactly
the three operations the code under test performs:

* ``add`` + ``flush`` -- used by :func:`create_token` to persist a new row (the fake
  assigns the primary key the ORM would normally populate on flush);
* ``get(ClientToken, id)`` -- used by :func:`set_enabled` / :func:`revoke`;
* ``scalar(select(ClientToken).where(ClientToken.id_prefix == ...))`` -- the
  constant-work lookup :func:`verify` performs before the constant-time hash compare.

This keeps the tests free of a real database while still running the genuine status
gate against real, freshly issued token secrets.
"""

from __future__ import annotations

import asyncio
import base64
import uuid

from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from gozar.core.config import Settings
from gozar.tokens import service
from gozar.tokens.models import ClientToken, TokenStatus

# Fixed test-only secret material so issuance, encrypted reveal storage, and
# verification share deterministic configuration.
_MASTER_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
_SETTINGS = Settings(
    master_key=_MASTER_KEY,
    token_pepper="property-12-test-pepper",
)


class _FakeSession:
    """In-memory stand-in for ``AsyncSession`` covering the token code paths.

    Rows are kept in a dict keyed by ``ClientToken.id`` and indexed by ``id_prefix``
    for the verification lookup. Only the methods the code under test calls are
    implemented; anything else is intentionally absent so accidental reliance on real
    database behavior surfaces immediately.
    """

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, ClientToken] = {}

    def add(self, obj: object) -> None:
        # create_token adds a ClientToken whose id is unset until flush; assign the
        # primary key here exactly as a real flush would.
        if isinstance(obj, ClientToken):
            if obj.id is None:
                obj.id = uuid.uuid4()
            self._by_id[obj.id] = obj

    async def flush(self) -> None:  # no-op: rows are already "persisted" on add
        return None

    async def get(self, model: type, ident: uuid.UUID) -> object | None:
        if model is ClientToken:
            return self._by_id.get(ident)
        return None

    async def scalar(self, statement: object) -> object | None:
        # Only ``select(ClientToken).where(ClientToken.id_prefix == <value>)`` is
        # issued by verify(); resolve it against the in-memory index.
        whereclause = statement.whereclause  # type: ignore[attr-defined]
        wanted_prefix = whereclause.right.value
        for token in self._by_id.values():
            if token.id_prefix == wanted_prefix:
                return token
        return None


def _run(coro):
    """Execute an async coroutine to completion from a sync Hypothesis test."""
    return asyncio.run(coro)


async def _issue(session: _FakeSession, label: str):
    return await service.create_token(session, label, settings=_SETTINGS)


async def _verify(session: _FakeSession, presented: str):
    return await service.verify(session, presented, settings=_SETTINGS)


# Hypothesis strategies -------------------------------------------------------

# Arbitrary labels, including unicode and whitespace, to confirm the gate is
# label-agnostic (no language- or content-specific assumptions).
_labels = st.text(min_size=0, max_size=40)
# Non-active statuses that must always be rejected.
_inactive_statuses = st.sampled_from(
    [TokenStatus.DISABLED.value, TokenStatus.REVOKED.value]
)


# Feature: gozar, Property 12: For any client token and status, verification
# authorizes the request if and only if the token is active (enabled and not
# revoked); disabled or revoked tokens are always rejected; re-enabling a disabled
# token restores acceptance.
@hyp_settings(max_examples=150)
@given(label=_labels)
def test_active_token_is_authorized(label: str) -> None:
    """Validates: Requirements 8.4, 9.3, 9.4.

    A freshly issued (active) token verifies successfully and reports active status.
    """

    async def scenario() -> None:
        session = _FakeSession()
        issued = await _issue(session, label)

        result = await _verify(session, issued.secret)
        assert result is not None
        assert result.token_id == issued.token_id
        assert result.status == TokenStatus.ACTIVE.value

    _run(scenario())


# Feature: gozar, Property 12: For any client token and status, verification
# authorizes the request if and only if the token is active (enabled and not
# revoked); disabled or revoked tokens are always rejected; re-enabling a disabled
# token restores acceptance.
@hyp_settings(max_examples=150)
@given(label=_labels, status=_inactive_statuses)
def test_disabled_or_revoked_token_is_rejected(label: str, status: str) -> None:
    """Validates: Requirements 8.4, 9.3, 9.4.

    With a correct secret presented, a token whose status is disabled or revoked is
    never authorized; the gate denies independently of secret correctness.
    """

    async def scenario() -> None:
        session = _FakeSession()
        issued = await _issue(session, label)

        # Confirm it would be accepted while active, then move it out of active.
        assert await _verify(session, issued.secret) is not None
        if status == TokenStatus.DISABLED.value:
            await service.set_enabled(session, issued.token_id, False)
        else:
            await service.revoke(session, issued.token_id)

        assert await _verify(session, issued.secret) is None

    _run(scenario())


# Feature: gozar, Property 12: For any client token and status, verification
# authorizes the request if and only if the token is active (enabled and not
# revoked); disabled or revoked tokens are always rejected; re-enabling a disabled
# token restores acceptance.
@hyp_settings(max_examples=150)
@given(label=_labels)
def test_reenabling_disabled_token_restores_acceptance(label: str) -> None:
    """Validates: Requirements 9.3, 9.4.

    Disabling then re-enabling a token returns it to the authorized state, while
    revocation remains terminal and cannot be re-enabled.
    """

    async def scenario() -> None:
        session = _FakeSession()
        issued = await _issue(session, label)

        # active -> disabled -> rejected
        await service.set_enabled(session, issued.token_id, False)
        assert await _verify(session, issued.secret) is None

        # disabled -> re-enabled -> accepted again
        await service.set_enabled(session, issued.token_id, True)
        restored = await _verify(session, issued.secret)
        assert restored is not None
        assert restored.status == TokenStatus.ACTIVE.value

        # revoked is terminal: re-enabling must not resurrect the token
        await service.revoke(session, issued.token_id)
        assert await _verify(session, issued.secret) is None
        await service.set_enabled(session, issued.token_id, True)
        assert await _verify(session, issued.secret) is None

    _run(scenario())


# Feature: gozar, Property 12: For any client token and status, verification
# authorizes the request if and only if the token is active (enabled and not
# revoked); disabled or revoked tokens are always rejected; re-enabling a disabled
# token restores acceptance.
@hyp_settings(max_examples=150)
@given(label=_labels, status=st.sampled_from([s.value for s in TokenStatus]))
def test_authorized_iff_active_biconditional(label: str, status: str) -> None:
    """Validates: Requirements 8.4, 9.3, 9.4.

    The core biconditional: for any reachable status, ``verify`` returns a result
    (authorizes) if and only if the token's status is active.
    """

    async def scenario() -> None:
        session = _FakeSession()
        issued = await _issue(session, label)

        # Drive the token to the target status via the public lifecycle API.
        if status == TokenStatus.DISABLED.value:
            await service.set_enabled(session, issued.token_id, False)
        elif status == TokenStatus.REVOKED.value:
            await service.revoke(session, issued.token_id)
        # ACTIVE: leave as freshly issued.

        authorized = await _verify(session, issued.secret) is not None
        assert authorized == (status == TokenStatus.ACTIVE.value)

    _run(scenario())
