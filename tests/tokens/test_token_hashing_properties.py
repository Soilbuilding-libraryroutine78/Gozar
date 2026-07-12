"""Property-based tests for client-token hashing and verification (Property 11).

These tests validate Property 11 from the Gozar design (Requirement 8.2): the
Token_Authority stores only a *non-reversible* representation of a Client_Token --
a keyed HMAC-SHA256 digest, never the secret itself -- and verification accepts the
correct presented secret (via a constant-time compare) while rejecting any other
secret.

The real :func:`gozar.tokens.service.create_token`, :func:`~gozar.tokens.service.verify`,
and :func:`~gozar.tokens.service.hash_secret` code paths are exercised. Because the
project's async DB driver requires a running PostgreSQL, the persistence layer is
replaced with a tiny in-memory stand-in for ``AsyncSession`` that implements exactly
the two operations these functions use (``add``/``flush`` to persist a token and
``scalar`` to look it up by its non-secret ``id_prefix``). This keeps the test
hermetic while running the genuine hashing and constant-time-comparison logic.

The token *status* gate (disabled/revoked/limit) is covered separately by Property 12
(task 8.4); this module focuses solely on the hashing and secret-verification contract.
"""

from __future__ import annotations

import asyncio
import base64
import uuid

from hypothesis import assume, given, settings as hyp_settings
from hypothesis import strategies as st

from gozar.core.config import Settings
from gozar.tokens.models import ClientToken
from gozar.tokens.service import (
    _TOKEN_SCHEME,
    create_token,
    hash_secret,
    verify,
)


def _settings() -> Settings:
    """Settings carrying a fixed server-side pepper so HMAC hashing is configured.

    Passing this via the ``settings`` override isolates the test from the process
    environment and the cached settings singleton.
    """
    master_key = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
    return Settings(
        master_key=master_key,
        token_pepper="unit-test-client-token-pepper-value",
    )


_SETTINGS = _settings()


class _FakeAsyncSession:
    """Minimal in-memory stand-in for ``AsyncSession`` for the token code paths.

    Supports only what :func:`create_token` (``add`` + ``flush``) and :func:`verify`
    (``scalar`` lookup by ``id_prefix``) require. ``flush`` assigns the primary-key
    ``id`` the way a real flush would apply the model's ``default=uuid.uuid4``.
    """

    def __init__(self) -> None:
        self._tokens: list[ClientToken] = []

    def add(self, obj: ClientToken) -> None:
        self._tokens.append(obj)

    async def flush(self) -> None:
        for token in self._tokens:
            if token.id is None:
                token.id = uuid.uuid4()
            if token.status is None:
                token.status = "active"

    async def scalar(self, statement):
        """Return the stored token whose ``id_prefix`` the statement filters on.

        ``verify`` issues ``select(ClientToken).where(ClientToken.id_prefix == p)``;
        the bound value ``p`` is read from the compiled statement parameters and
        matched against the stored rows (each ``id_prefix`` is unique).
        """
        bound_values = set(statement.compile().params.values())
        for token in self._tokens:
            if token.id_prefix in bound_values:
                return token
        return None


def _split_secret(presented: str, id_prefix: str) -> str:
    """Return just the secret portion of a ``gz-<id_prefix>-<secret>`` string."""
    return presented[len(f"{_TOKEN_SCHEME}-{id_prefix}-"):]


# Feature: gozar, Property 11: Client token hashing and verification
@hyp_settings(max_examples=150)
@given(
    label=st.text(min_size=1, max_size=64),
    wrong_secret=st.text(
        alphabet=st.characters(min_codepoint=33, max_codepoint=126),
        min_size=1,
        max_size=80,
    ),
)
def test_issued_token_verifies_and_storage_is_non_reversible(
    label: str, wrong_secret: str
) -> None:
    """Validates: Requirements 8.2.

    For any issued client token: verifying the correct presented secret succeeds,
    verifying a different secret fails, and the persisted representation is a
    non-reversible HMAC digest that is never equal to the secret value.
    """

    async def _check() -> None:
        session = _FakeAsyncSession()
        issued = await create_token(session, label, settings=_SETTINGS)

        # The only stored token, with its persisted hash, is what verify reads.
        stored = session._tokens[0]
        real_secret = _split_secret(issued.secret, issued.id_prefix)

        # Non-reversible storage: the persisted representation is the keyed HMAC
        # digest, never the raw secret or the presentable token string.
        assert stored.token_hash == hash_secret(real_secret, settings=_SETTINGS)
        assert stored.token_hash != real_secret.encode("utf-8")
        assert stored.token_hash != issued.secret.encode("utf-8")
        assert len(stored.token_hash) == 32  # SHA-256 digest size.

        # Correct presented secret authenticates.
        ok = await verify(session, issued.secret, settings=_SETTINGS)
        assert ok is not None
        assert ok.token_id == issued.token_id

        # Any *different* secret presented under the same id_prefix is rejected.
        assume(wrong_secret != real_secret)
        forged = f"{_TOKEN_SCHEME}-{issued.id_prefix}-{wrong_secret}"
        assert await verify(session, forged, settings=_SETTINGS) is None

    asyncio.run(_check())


# Feature: gozar, Property 11: Client token hashing and verification
@hyp_settings(max_examples=200)
@given(
    secret=st.text(min_size=1, max_size=256),
    other=st.text(min_size=1, max_size=256),
)
def test_hash_is_deterministic_non_reversible_and_collision_distinct(
    secret: str, other: str
) -> None:
    """Validates: Requirements 8.2.

    The stored HMAC representation is deterministic for a given secret, never equal
    to the secret value (non-reversible), and differs for distinct secrets -- so a
    constant-time compare accepts only the matching secret.
    """
    digest = hash_secret(secret, settings=_SETTINGS)

    # Deterministic: the same secret always hashes to the same stored value.
    assert hash_secret(secret, settings=_SETTINGS) == digest
    # Non-reversible: the stored representation is never the plaintext secret.
    assert digest != secret.encode("utf-8")
    assert len(digest) == 32

    # Distinct secrets produce distinct digests (no collision for differing input),
    # which is what makes verification of a wrong secret fail.
    if other != secret:
        assert hash_secret(other, settings=_SETTINGS) != digest
