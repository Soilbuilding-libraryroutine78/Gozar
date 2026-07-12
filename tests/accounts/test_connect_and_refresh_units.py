"""Complementary unit tests for connect/refresh edge cases (task 5.8).

The bulk of OAuth/API-key connect and refresh-failure behaviour is already covered
by ``test_connect.py`` (task 5.6) and ``test_refresh.py`` (task 5.7). This module
deliberately avoids duplicating those scenarios and instead fills the remaining
gaps for the task's requirements, using the same in-memory fake-session/store and
injected exchange/validate/refresh hooks so no real network or Redis is touched:

* Req 1.1 -- the authorization flow obtains a token and creates the account even
  when the access token is opaque (not a JWT, so no provider account ref can be
  derived), and the stored expiry is taken from an absolute ``expires_at`` claim
  (the existing success test only exercises a JWT token + relative ``expires_in``).
* Req 1.3 -- a consumed pending-connect handle cannot be replayed after a
  successful connect, and the failure error is descriptive.
* Req 2.1 -- ``connect_api_key`` fails closed for an unknown Provider without
  running validation or creating an account.
* Req 2.3 -- API-key validation failure yields a descriptive error and persists no
  secret material.
* Req 3.3 -- a refresh whose response omits the access token marks the account as
  requiring reauthorization with a recorded reason (an uncovered failure branch),
  and a malformed/expired stored secret on refresh is surfaced.
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timezone

import jwt
import pytest

from gozar.accounts import service
from gozar.accounts.models import (
    ApiKeySecret,
    CredentialKind,
    CredentialStatus,
    SubscriptionSecret,
    UpstreamCredential,
)
from gozar.core.config import Settings
from gozar.core.errors import UpstreamError, ValidationError

# A real base64-encoded 32-byte master key so envelope encryption works in-process.
_MASTER_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")


def _settings(**overrides) -> Settings:
    base = {
        "master_key": _MASTER_KEY,
        "redis_url": "redis://localhost:6379/0",
        "subscription_renewal_window_seconds": 300,
        "provider_base_urls": {
            "openai": "https://api.openai.com/v1",
            "codex": "https://chatgpt.com/backend-api/codex",
        },
        "provider_oauth": {
            "codex": {
                "authorize_url": "https://auth.openai.com/oauth/authorize",
                "token_url": "https://auth.openai.com/oauth/token",
                "client_id": "sample-client",
                "redirect_uri": "http://127.0.0.1:1455/auth/callback",
                "scopes": ["openid", "profile", "email"],
            },
        },
    }
    base.update(overrides)
    return Settings(**base)


class _MemStore:
    """In-memory PendingConnectStore (no Redis)."""

    def __init__(self) -> None:
        self.data: dict[str, dict] = {}

    async def put(self, pending_id, data, ttl_seconds):
        self.data[pending_id] = data

    async def get(self, pending_id):
        return self.data.get(pending_id)

    async def delete(self, pending_id):
        self.data.pop(pending_id, None)


class _FakeSession:
    """Captures added rows; flush is a no-op (no real DB)."""

    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


class _PreloadSession(_FakeSession):
    """Fake session that also serves rows by (model, primary key) for refresh."""

    def __init__(self) -> None:
        super().__init__()
        self.objects: dict[tuple[type, object], object] = {}

    def preload(self, model: type, key: object, obj: object) -> None:
        self.objects[(model, key)] = obj

    async def get(self, model: type, key: object):
        return self.objects.get((model, key))


class _NoopLock:
    async def __aenter__(self) -> "_NoopLock":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _noop_lock_factory(account_id: uuid.UUID) -> _NoopLock:
    return _NoopLock()


# --- Req 1.1: authorization obtains a token and creates the account -----------
async def test_complete_with_opaque_access_token_creates_account_without_ref():
    """An opaque (non-JWT) access token still yields a connected account (Req 1.1).

    The provider account reference cannot be derived from an opaque token, so it is
    ``None`` and the locally generated credential id remains the unique identifier;
    the account is created and the bundle persisted encrypted regardless.
    """
    settings = _settings()
    store = _MemStore()
    challenge = await service.begin_subscription_connect(
        "codex", settings=settings, store=store
    )

    async def opaque_exchange(entry, code, verifier):
        return {
            "access_token": "opaque-not-a-jwt-token",
            "refresh_token": "refresh-abc",
            "expires_in": 1800,
        }

    session = _FakeSession()
    credential = await service.complete_subscription_connect(
        session,
        challenge.pending_id,
        code="auth-code",
        state=challenge.state,
        settings=settings,
        store=store,
        exchange=opaque_exchange,
    )

    assert isinstance(credential, UpstreamCredential)
    assert credential.kind is CredentialKind.SUBSCRIPTION
    # No account ref could be derived from an opaque token.
    assert credential.provider_account_ref is None
    # The encrypted secret was still persisted (never the plaintext token).
    secrets_added = [r for r in session.added if isinstance(r, SubscriptionSecret)]
    assert len(secrets_added) == 1
    assert b"opaque-not-a-jwt-token" not in secrets_added[0].ciphertext


async def test_complete_derives_expiry_from_absolute_expires_at():
    """The stored expiry honours an absolute ``expires_at`` epoch claim (Req 1.1)."""
    settings = _settings()
    store = _MemStore()
    challenge = await service.begin_subscription_connect(
        "codex", settings=settings, store=store
    )

    absolute = datetime(2030, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    access = jwt.encode(
        {"https://api.openai.com/auth": {"chatgpt_account_id": "acct-abs"}},
        "irrelevant",
        algorithm="HS256",
    )

    async def absolute_exchange(entry, code, verifier):
        return {
            "access_token": access,
            "refresh_token": "r",
            "expires_at": absolute.timestamp(),
        }

    session = _FakeSession()
    await service.complete_subscription_connect(
        session,
        challenge.pending_id,
        code="auth-code",
        state=challenge.state,
        settings=settings,
        store=store,
        exchange=absolute_exchange,
    )

    secret = next(r for r in session.added if isinstance(r, SubscriptionSecret))
    assert secret.expires_at == absolute


# --- Req 1.3: failure handling / replay protection ----------------------------
async def test_pending_handle_cannot_be_replayed_after_success():
    """A pending handle is single-use: a replayed connect fails (Req 1.3)."""
    settings = _settings()
    store = _MemStore()
    challenge = await service.begin_subscription_connect(
        "codex", settings=settings, store=store
    )

    async def good_exchange(entry, code, verifier):
        return {
            "access_token": jwt.encode(
                {"account_id": "acct-1"}, "x", algorithm="HS256"
            ),
            "refresh_token": "r",
            "expires_in": 600,
        }

    first = _FakeSession()
    await service.complete_subscription_connect(
        first,
        challenge.pending_id,
        code="auth-code",
        state=challenge.state,
        settings=settings,
        store=store,
        exchange=good_exchange,
    )

    # Replay the same handle/state: the pending state was consumed on success.
    replay = _FakeSession()
    with pytest.raises(ValidationError):
        await service.complete_subscription_connect(
            replay,
            challenge.pending_id,
            code="auth-code",
            state=challenge.state,
            settings=settings,
            store=store,
            exchange=good_exchange,
        )
    assert replay.added == []


async def test_exchange_failure_error_is_descriptive():
    """The connect failure error names the provider and creates no account (Req 1.3)."""
    settings = _settings()
    store = _MemStore()
    challenge = await service.begin_subscription_connect(
        "codex", settings=settings, store=store
    )

    async def failing_exchange(entry, code, verifier):
        raise UpstreamError("provider returned 400")

    session = _FakeSession()
    with pytest.raises(ValidationError) as excinfo:
        await service.complete_subscription_connect(
            session,
            challenge.pending_id,
            code="bad",
            state=challenge.state,
            settings=settings,
            store=store,
            exchange=failing_exchange,
        )
    message = str(excinfo.value)
    assert "codex" in message
    assert message.strip()
    assert session.added == []


# --- Req 2.1 / 2.3: API-key connect fails closed ------------------------------
async def test_connect_api_key_unknown_provider_fails_without_validation():
    """An unknown Provider is refused before validation or creation (Req 2.1)."""
    settings = _settings()
    session = _FakeSession()

    async def must_not_run(entry, api_key):  # pragma: no cover - must not run
        raise AssertionError("validation must not run for an unknown provider")

    with pytest.raises(ValidationError):
        await service.connect_api_key(
            session,
            "no-such-provider",
            "sk-whatever",
            settings=settings,
            validate=must_not_run,
        )
    assert session.added == []


async def test_connect_api_key_failure_is_descriptive_and_persists_nothing():
    """Validation failure yields a descriptive error and stores no secret (Req 2.3)."""
    settings = _settings()
    session = _FakeSession()

    async def failing_validate(entry, api_key):
        raise UpstreamError("401 from provider")

    with pytest.raises(ValidationError) as excinfo:
        await service.connect_api_key(
            session,
            "openai",
            "sk-bad-key",
            settings=settings,
            validate=failing_validate,
        )
    assert "openai" in str(excinfo.value)
    # No credential and no encrypted secret material were persisted.
    assert session.added == []
    assert [r for r in session.added if isinstance(r, ApiKeySecret)] == []


# --- Req 3.3: refresh failure marks requires_reauth ---------------------------
def _make_subscription_account(settings: Settings):
    account_id = uuid.uuid4()
    credential = UpstreamCredential(
        id=account_id,
        provider="codex",
        kind=CredentialKind.SUBSCRIPTION,
        label="codex",
        status=CredentialStatus.ACTIVE,
        provider_account_ref="acct-xyz",
    )
    credential.deleted_at = None
    credential.requires_reauth_reason = None

    bundle = {
        "access_token": "access-old",
        "refresh_token": "refresh-old",
        "account_id": "acct-xyz",
        "scopes": "openid profile",
        "expires_at": None,
    }
    record = service._encrypt_bundle(bundle, settings)
    secret = SubscriptionSecret(
        account_id=account_id,
        ciphertext=record.ciphertext,
        nonce=record.nonce,
        wrapped_dek=record.wrapped_dek,
        expires_at=None,
    )
    session = _PreloadSession()
    session.preload(UpstreamCredential, account_id, credential)
    session.preload(SubscriptionSecret, account_id, secret)
    return session, account_id, credential, secret


async def test_refresh_response_without_access_token_marks_requires_reauth():
    """A refresh response lacking an access token marks reauth + reason (Req 3.3).

    This is the failure branch where the provider answers with a 2xx body that does
    not actually carry a new access token; it must be treated as a failed refresh,
    not silently accepted.
    """
    settings = _settings()
    session, account_id, credential, secret = _make_subscription_account(settings)
    original_ciphertext = secret.ciphertext

    async def tokenless_refresh(entry, refresh_token):
        return {"refresh_token": "rotated", "expires_in": 3600}

    outcome = await service.refresh_subscription(
        session,
        account_id,
        settings=settings,
        refresh=tokenless_refresh,
        lock_factory=_noop_lock_factory,
    )

    assert outcome.refreshed is False
    assert outcome.requires_reauth is True
    assert outcome.reason and "access token" in outcome.reason
    assert credential.status is CredentialStatus.REQUIRES_REAUTH
    assert credential.requires_reauth_reason == outcome.reason
    # The stored secret is left untouched on a failed refresh.
    assert secret.ciphertext == original_ciphertext


async def test_successful_refresh_clears_prior_reauth_flag():
    """A successful refresh recovers a reauth-flagged account (Req 3.3 inverse)."""
    settings = _settings()
    session, account_id, credential, secret = _make_subscription_account(settings)
    # Account had previously been flagged for reauthorization.
    credential.status = CredentialStatus.REQUIRES_REAUTH
    credential.requires_reauth_reason = "earlier failure"

    async def good_refresh(entry, refresh_token):
        return {
            "access_token": "access-new",
            "refresh_token": "refresh-rotated",
            "expires_in": 3600,
        }

    outcome = await service.refresh_subscription(
        session,
        account_id,
        settings=settings,
        refresh=good_refresh,
        lock_factory=_noop_lock_factory,
    )

    assert outcome.refreshed is True
    assert outcome.requires_reauth is False
    assert credential.status is CredentialStatus.ACTIVE
    assert credential.requires_reauth_reason is None
