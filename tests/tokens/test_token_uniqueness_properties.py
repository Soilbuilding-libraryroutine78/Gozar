"""Property-based test for client-token uniqueness (Property 10).

Validates Property 10 from the Gozar design: for any sequence of client-token
creations, every issued token has a distinct lookup ``id_prefix`` and a distinct
secret value. ``id_prefix`` is the non-secret lookup key persisted on the row and
embedded in the presentable ``gz-<id_prefix>-<secret>`` string; the secret is the
high-entropy material rendered once at creation. Both must be unique across every
issuance so a presented token always resolves to exactly one token row.

The Token_Authority's :func:`gozar.tokens.service.create_token` is the unit under
test. It is driven against a minimal in-memory fake of the async session (mirroring
the fake-session convention used by ``tests/auth/test_session_tokens.py``) so the
generation logic is exercised without a live database: the fake only needs to accept
added rows and assign primary keys on flush, which is all ``create_token`` requires
when no usage limit is attached.
"""

from __future__ import annotations

import asyncio
import base64
import uuid

from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from gozar.core.config import Settings
from gozar.tokens.service import create_token


def _settings() -> Settings:
    """Settings carrying a fixed token pepper so HMAC hashing is configured.

    Passing this via the ``settings`` override keeps the test isolated from the
    process environment and the cached settings singleton.
    """
    master_key = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
    return Settings(
        master_key=master_key,
        token_pepper="unit-test-client-token-pepper-value",
    )


class _FakeSession:
    """Minimal in-memory stand-in for the async session used by ``create_token``.

    ``create_token`` (without a usage limit) only calls ``add`` then ``flush``; the
    real database assigns the row's primary key on flush via the model's
    ``default=uuid.uuid4``. The fake reproduces just that: it records added rows and
    assigns any missing id on flush, keeping the test free of a live database.
    """

    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()


# The number of tokens to issue in a single generated sequence. Bounded so each
# example stays fast while still exercising many simultaneous issuances.
_sequence_lengths = st.integers(min_value=2, max_value=50)


async def _issue_sequence(count: int, settings: Settings) -> tuple[list[str], list[str]]:
    """Issue ``count`` client tokens in sequence and collect their parts.

    Returns the list of ``id_prefix`` lookup keys and the list of full secret
    strings, in issuance order.
    """
    session = _FakeSession()
    prefixes: list[str] = []
    secrets_seen: list[str] = []
    for index in range(count):
        issued = await create_token(session, label=f"token-{index}", settings=settings)
        prefixes.append(issued.id_prefix)
        # ``issued.secret`` is the full ``gz-<id_prefix>-<secret>`` string; it is the
        # only place the secret is exposed and must itself be unique per issuance.
        secrets_seen.append(issued.secret)
    return prefixes, secrets_seen


# Feature: gozar, Property 10: Client token uniqueness
@hyp_settings(max_examples=200)
@given(count=_sequence_lengths)
def test_issued_tokens_have_distinct_prefixes_and_secrets(count: int) -> None:
    """Validates: Requirements 8.1.

    For any sequence of client-token creations, every issued token has a distinct
    lookup ``id_prefix`` and a distinct secret value.

    The async issuance is driven on a fresh event loop per example so the property
    test itself stays synchronous (Hypothesis does not run async test bodies).
    """
    settings = _settings()

    prefixes, secrets_seen = asyncio.run(_issue_sequence(count, settings))

    # Distinct lookup id prefixes: no two issued tokens collide on the lookup key.
    assert len(set(prefixes)) == count
    # Distinct secret values: every issued token string is unique.
    assert len(set(secrets_seen)) == count
