"""Property-based test for account identifier uniqueness (Property 3).

Validates Property 3 from the Gozar design: for any sequence of subscription or
API-key account creations, every created account receives a distinct account
identifier and records its provider and connection timestamp.

The account identifier is :attr:`UpstreamCredential.id` (generated server-side as a
fresh ``uuid.uuid4()`` by the Account_Manager at connection time, Requirement 1.4),
``provider`` is the connected Provider, and the connection timestamp is
``connected_at``.

The units under test are the real Account_Manager connect flows in
:mod:`gozar.accounts.service`:

* :func:`~gozar.accounts.service.begin_subscription_connect` +
  :func:`~gozar.accounts.service.complete_subscription_connect` for
  Subscription_Accounts (providers ``codex`` / ``anthropic``), and
* :func:`~gozar.accounts.service.connect_api_key` for API_Key_Accounts
  (providers ``openai`` / ``openrouter``).

They are driven against the same in-memory fake-session/store pattern established in
``tests/accounts/test_connect.py`` so the real creation code paths run without a live
database, Redis, or network: the PKCE pending state is an in-memory store, the token
exchange and API-key validation are injected, and the fake session captures the rows
that would be persisted. The ``connected_at`` column is populated by the database via
``server_default=func.now()``; the fake session reproduces exactly that one DB-side
behaviour on ``flush`` (mirroring how ``tests/tokens/test_token_uniqueness_properties.py``
reproduces the DB-assigned primary key), and a separate schema-level test anchors that
the recording of the timestamp is a real, non-nullable guarantee of the model rather
than an artefact of the fake.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import base64

import jwt
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from gozar.accounts import service
from gozar.accounts.models import UpstreamCredential
from gozar.core.config import Settings

# A real base64-encoded 32-byte master key so envelope encryption works in-process
# (the connect flows encrypt the secret bundle before persisting it).
_MASTER_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")

# Which connect flow each supported Provider uses. ``codex``/``anthropic`` connect via
# the subscription OAuth (PKCE) flow; ``openai``/``openrouter`` connect via an API key.
_PROVIDER_KINDS: dict[str, str] = {
    "codex": "subscription",
    "anthropic": "subscription",
    "openai": "api_key",
    "openrouter": "api_key",
}


def _settings() -> Settings:
    """Settings with all four providers configured (base URLs + subscription OAuth).

    Passed via the ``settings`` override so the test is isolated from the process
    environment and the cached settings singleton.
    """
    return Settings(
        master_key=_MASTER_KEY,
        redis_url="redis://localhost:6379/0",
        provider_base_urls={
            "openai": "https://api.openai.com/v1",
            "openrouter": "https://openrouter.ai/api/v1",
            "codex": "https://chatgpt.com/backend-api/codex",
            "anthropic": "https://api.anthropic.com/v1",
        },
        provider_oauth={
            "codex": {
                "authorize_url": "https://auth.openai.com/oauth/authorize",
                "token_url": "https://auth.openai.com/oauth/token",
                "client_id": "sample-codex-client",
                "redirect_uri": "http://127.0.0.1:1455/auth/callback",
                "scopes": ["openid", "profile", "email"],
            },
            "anthropic": {
                "authorize_url": "https://auth.anthropic.com/oauth/authorize",
                "token_url": "https://auth.anthropic.com/oauth/token",
                "client_id": "sample-anthropic-client",
                "redirect_uri": "http://127.0.0.1:1456/auth/callback",
                "scopes": ["read", "write"],
            },
        },
    )


class _MemStore:
    """In-memory PendingConnectStore for the PKCE flow (no Redis)."""

    def __init__(self) -> None:
        self.data: dict[str, dict] = {}

    async def put(self, pending_id, data, ttl_seconds):
        self.data[pending_id] = data

    async def get(self, pending_id):
        return self.data.get(pending_id)

    async def delete(self, pending_id):
        self.data.pop(pending_id, None)


class _FakeSession:
    """Captures added rows and reproduces the DB-side ``connected_at`` default.

    The connect flows only call ``add`` then ``flush`` (no usage limit is attached
    during creation). The real database assigns ``connected_at`` via
    ``server_default=func.now()`` at insert time; this fake reproduces exactly that
    one behaviour on ``flush`` so a captured credential reflects what the database
    would have stored. The primary ``id`` is set explicitly by the service code, so
    it is left untouched; it is only backfilled here in the (unused) case it is ever
    left unset, matching the model's ``default=uuid.uuid4``.
    """

    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        for obj in self.added:
            if isinstance(obj, UpstreamCredential):
                if getattr(obj, "id", None) is None:
                    obj.id = uuid.uuid4()
                if getattr(obj, "connected_at", None) is None:
                    obj.connected_at = datetime.now(timezone.utc)


def _access_token(account_id: str) -> str:
    """Build a subscription access token (JWT) embedding a provider account id."""
    return jwt.encode(
        {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}},
        "irrelevant-test-secret",
        algorithm="HS256",
    )


async def _create_one(
    session: _FakeSession,
    store: _MemStore,
    settings: Settings,
    provider: str,
    index: int,
) -> UpstreamCredential:
    """Create one account through the real connect flow for ``provider``."""
    if _PROVIDER_KINDS[provider] == "subscription":
        challenge = await service.begin_subscription_connect(
            provider, settings=settings, store=store
        )

        async def fake_exchange(entry, code, verifier, _i=index, _p=provider):
            return {
                "access_token": _access_token(f"acct-{_p}-{_i}"),
                "refresh_token": f"refresh-{_p}-{_i}",
                "expires_in": 3600,
                "scope": "openid profile",
            }

        return await service.complete_subscription_connect(
            session,
            challenge.pending_id,
            code=f"auth-code-{index}",
            state=challenge.state,
            settings=settings,
            store=store,
            exchange=fake_exchange,
        )

    async def fake_validate(entry, api_key):
        return None

    return await service.connect_api_key(
        session,
        provider,
        f"sk-{provider}-{index}-secret",
        settings=settings,
        validate=fake_validate,
    )


async def _create_sequence(
    providers: list[str], settings: Settings
) -> list[UpstreamCredential]:
    """Create one account per entry in ``providers``, in order, on one session."""
    session = _FakeSession()
    store = _MemStore()
    created: list[UpstreamCredential] = []
    for index, provider in enumerate(providers):
        created.append(await _create_one(session, store, settings, provider, index))
    return created


# A sequence of account creations: each entry names the Provider to connect. Bounded
# so each example stays fast while still exercising many creations of both kinds.
_creation_sequences = st.lists(
    st.sampled_from(sorted(_PROVIDER_KINDS)),
    min_size=2,
    max_size=40,
)


# Feature: gozar, Property 3: Account identifier uniqueness
@hyp_settings(max_examples=200)
@given(providers=_creation_sequences)
def test_created_accounts_have_distinct_ids_and_record_provider_and_timestamp(
    providers: list[str],
) -> None:
    """Validates: Requirements 1.4.

    For any sequence of subscription or API-key account creations, every created
    account receives a distinct account identifier (``id``) and records its provider
    and connection timestamp (``connected_at``).

    The async creation is driven on a fresh event loop per example so the property
    test body stays synchronous (Hypothesis does not run async test bodies).
    """
    settings = _settings()
    before = datetime.now(timezone.utc) - timedelta(seconds=1)

    created = asyncio.run(_create_sequence(providers, settings))

    after = datetime.now(timezone.utc) + timedelta(seconds=1)

    # One credential was created per requested connection.
    assert len(created) == len(providers)

    ids = [credential.id for credential in created]
    # Every account received an identifier...
    assert all(account_id is not None for account_id in ids)
    # ...and every identifier is distinct across the whole sequence.
    assert len(set(ids)) == len(ids)

    for credential, expected_provider in zip(created, providers):
        # The connected Provider is recorded exactly as requested.
        assert credential.provider == expected_provider
        # The connection timestamp is recorded (non-null) and is a sane "now".
        assert credential.connected_at is not None
        assert before <= credential.connected_at <= after


def test_connection_timestamp_recording_is_a_model_level_guarantee() -> None:
    """Validates: Requirements 1.4.

    Anchor the "records its connection timestamp" guarantee at the schema level so it
    is a real property of :class:`UpstreamCredential` and not merely reproduced by the
    test's fake session: ``connected_at`` is non-nullable and carries a server-side
    default, and ``provider`` (also part of the recorded identity) is non-nullable.
    """
    columns = UpstreamCredential.__table__.columns

    connected_at = columns["connected_at"]
    assert connected_at.nullable is False
    assert connected_at.server_default is not None

    provider = columns["provider"]
    assert provider.nullable is False

    # The primary key (the account identifier) defaults to a fresh UUID per row.
    primary_key = columns["id"]
    assert primary_key.primary_key is True
    assert primary_key.default is not None
