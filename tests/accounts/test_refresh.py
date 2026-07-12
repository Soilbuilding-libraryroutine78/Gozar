"""Unit tests for locked subscription token refresh and lazy usable-token access (task 5.7).

These cover :func:`gozar.accounts.service.refresh_subscription` and
:func:`gozar.accounts.service.get_usable_token` without real network or Redis: the
per-account lock and the token refresh exchange are injected, and the database
session is a minimal in-memory fake that serves and retains the credential and its
encrypted secret rows. Envelope encryption runs for real (an in-process master key)
so the "replace the encrypted bundle" behaviour is exercised end to end.

Covered behaviour:

* Lazy refresh within the renewal window replaces the stored encrypted bundle and
  expiry, under the injected per-account lock (Requirements 3.1, 3.2).
* A failed refresh marks the Subscription_Account ``REQUIRES_REAUTH`` and records the
  failure reason, leaving the stored secret untouched (Requirement 3.3).
* ``get_usable_token`` refuses an account already flagged for reauthorization
  (Requirement 3.4) and refuses when a lazy refresh fails.
* ``get_usable_token`` does not refresh when the token is outside the renewal window.
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from gozar.accounts import service
from gozar.accounts.models import (
    CredentialKind,
    CredentialStatus,
    SubscriptionSecret,
    UpstreamCredential,
)
from gozar.core.config import Settings
from gozar.core.errors import NoAvailableAccount, UpstreamError, ValidationError

# A real base64-encoded 32-byte master key so envelope encryption works in-process.
_MASTER_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")


def _settings(**overrides) -> Settings:
    base = {
        "master_key": _MASTER_KEY,
        "redis_url": "redis://localhost:6379/0",
        "subscription_renewal_window_seconds": 300,
        "provider_base_urls": {
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


class _FakeSession:
    """In-memory session: serves and retains rows by (model, primary key)."""

    def __init__(self) -> None:
        self.objects: dict[tuple[type, object], object] = {}
        self.added: list = []
        self.flushes = 0

    def preload(self, model: type, key: object, obj: object) -> None:
        self.objects[(model, key)] = obj

    def add(self, obj) -> None:
        self.added.append(obj)

    async def get(self, model: type, key: object):
        return self.objects.get((model, key))

    async def flush(self) -> None:
        self.flushes += 1


class _FakeLock:
    """Async-context-manager lock that records acquire/release ordering."""

    def __init__(self, events: list[str], account_id: uuid.UUID) -> None:
        self._events = events
        self._account_id = account_id

    async def __aenter__(self) -> "_FakeLock":
        self._events.append(f"acquire:{self._account_id}")
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        self._events.append(f"release:{self._account_id}")
        return False


def _lock_factory(events: list[str]):
    def factory(account_id: uuid.UUID):
        return _FakeLock(events, account_id)

    return factory


def _make_subscription_account(
    settings: Settings,
    *,
    expires_at: datetime | None,
    status: CredentialStatus = CredentialStatus.ACTIVE,
    access_token: str = "access-old",
    refresh_token: str | None = "refresh-old",
) -> tuple[_FakeSession, uuid.UUID, UpstreamCredential, SubscriptionSecret]:
    account_id = uuid.uuid4()
    credential = UpstreamCredential(
        id=account_id,
        provider="codex",
        kind=CredentialKind.SUBSCRIPTION,
        label="codex",
        status=status,
        provider_account_ref="acct-xyz",
    )
    credential.deleted_at = None
    credential.requires_reauth_reason = None

    bundle = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "account_id": "acct-xyz",
        "scopes": "openid profile",
        "expires_at": expires_at.isoformat() if expires_at is not None else None,
    }
    record = service._encrypt_bundle(bundle, settings)
    secret = SubscriptionSecret(
        account_id=account_id,
        ciphertext=record.ciphertext,
        nonce=record.nonce,
        wrapped_dek=record.wrapped_dek,
        expires_at=expires_at,
    )

    session = _FakeSession()
    session.preload(UpstreamCredential, account_id, credential)
    session.preload(SubscriptionSecret, account_id, secret)
    return session, account_id, credential, secret


async def test_lazy_refresh_within_window_replaces_encrypted_bundle():
    settings = _settings()
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    # Expires in 60s; renewal window is 300s -> refresh is due.
    expires_at = now + timedelta(seconds=60)
    session, account_id, credential, secret = _make_subscription_account(
        settings, expires_at=expires_at
    )
    original_ciphertext = secret.ciphertext
    lock_events: list[str] = []

    refresh_calls: list[tuple[str, str]] = []

    async def fake_refresh(entry, refresh_token):
        refresh_calls.append((entry.provider_id.value, refresh_token))
        return {
            "access_token": "access-new",
            "refresh_token": "refresh-rotated",
            "expires_in": 3600,
            "scope": "openid profile",
        }

    material = await service.get_usable_token(
        session,
        account_id,
        settings=settings,
        now=now,
        refresh=fake_refresh,
        lock_factory=_lock_factory(lock_events),
    )

    # The stored refresh token was exchanged under the per-account lock.
    assert refresh_calls == [("codex", "refresh-old")]
    assert lock_events == [f"acquire:{account_id}", f"release:{account_id}"]

    # The encrypted bundle was replaced and the new token round-trips.
    assert secret.ciphertext != original_ciphertext
    refreshed_bundle = service._decrypt_bundle(secret, settings)
    assert refreshed_bundle["access_token"] == "access-new"
    # The rotated refresh token is persisted (token-sink safety).
    assert refreshed_bundle["refresh_token"] == "refresh-rotated"
    # New expiry was computed and stored.
    assert secret.expires_at is not None
    assert secret.expires_at > now

    # The caller receives the fresh access token and the account-id header value.
    assert material.access_token == "access-new"
    assert material.api_key is None
    assert material.provider_account_ref == "acct-xyz"
    assert credential.status is CredentialStatus.ACTIVE


async def test_refresh_failure_marks_requires_reauth_and_reason():
    settings = _settings()
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    expires_at = now + timedelta(seconds=60)
    session, account_id, credential, secret = _make_subscription_account(
        settings, expires_at=expires_at
    )
    original_ciphertext = secret.ciphertext

    async def failing_refresh(entry, refresh_token):
        raise UpstreamError("token endpoint rejected the refresh token")

    outcome = await service.refresh_subscription(
        session,
        account_id,
        settings=settings,
        refresh=failing_refresh,
        lock_factory=_lock_factory([]),
    )

    assert outcome.refreshed is False
    assert outcome.requires_reauth is True
    assert outcome.reason
    # The account is flagged for reauthorization with the reason recorded.
    assert credential.status is CredentialStatus.REQUIRES_REAUTH
    assert credential.requires_reauth_reason == outcome.reason
    # The stored secret is left untouched on failure.
    assert secret.ciphertext == original_ciphertext


async def test_refresh_with_no_stored_refresh_token_marks_reauth():
    settings = _settings()
    session, account_id, credential, _secret = _make_subscription_account(
        settings,
        expires_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        refresh_token=None,
    )

    async def must_not_run(entry, refresh_token):  # pragma: no cover - must not run
        raise AssertionError("exchange must not run without a refresh token")

    outcome = await service.refresh_subscription(
        session,
        account_id,
        settings=settings,
        refresh=must_not_run,
        lock_factory=_lock_factory([]),
    )

    assert outcome.requires_reauth is True
    assert credential.status is CredentialStatus.REQUIRES_REAUTH
    assert credential.requires_reauth_reason


async def test_get_usable_token_raises_when_requires_reauth():
    settings = _settings()
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    session, account_id, _credential, _secret = _make_subscription_account(
        settings,
        expires_at=now + timedelta(seconds=60),
        status=CredentialStatus.REQUIRES_REAUTH,
    )

    async def must_not_run(entry, refresh_token):  # pragma: no cover - must not run
        raise AssertionError("refresh must not run for a reauth-flagged account")

    with pytest.raises(NoAvailableAccount):
        await service.get_usable_token(
            session,
            account_id,
            settings=settings,
            now=now,
            refresh=must_not_run,
            lock_factory=_lock_factory([]),
        )


async def test_get_usable_token_raises_when_lazy_refresh_fails():
    settings = _settings()
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    session, account_id, credential, _secret = _make_subscription_account(
        settings, expires_at=now + timedelta(seconds=60)
    )

    async def failing_refresh(entry, refresh_token):
        raise UpstreamError("provider rejected the refresh")

    with pytest.raises(NoAvailableAccount):
        await service.get_usable_token(
            session,
            account_id,
            settings=settings,
            now=now,
            refresh=failing_refresh,
            lock_factory=_lock_factory([]),
        )
    # The failed lazy refresh left the account flagged for reauthorization.
    assert credential.status is CredentialStatus.REQUIRES_REAUTH


async def test_get_usable_token_skips_refresh_outside_window():
    settings = _settings()
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    # Expires well beyond the 300s renewal window -> no refresh.
    session, account_id, _credential, _secret = _make_subscription_account(
        settings, expires_at=now + timedelta(hours=2)
    )

    async def must_not_run(entry, refresh_token):  # pragma: no cover - must not run
        raise AssertionError("refresh must not run outside the renewal window")

    material = await service.get_usable_token(
        session,
        account_id,
        settings=settings,
        now=now,
        refresh=must_not_run,
        lock_factory=_lock_factory([]),
    )

    assert material.access_token == "access-old"


async def test_refresh_rejects_non_subscription_account():
    settings = _settings()
    account_id = uuid.uuid4()
    credential = UpstreamCredential(
        id=account_id,
        provider="openai",
        kind=CredentialKind.API_KEY,
        label="openai",
        status=CredentialStatus.ACTIVE,
    )
    credential.deleted_at = None
    session = _FakeSession()
    session.preload(UpstreamCredential, account_id, credential)

    with pytest.raises(ValidationError):
        await service.refresh_subscription(
            session,
            account_id,
            settings=_settings(provider_base_urls={"openai": "https://api.openai.com/v1"}),
            refresh=lambda e, rt: None,  # pragma: no cover - must not run
            lock_factory=_lock_factory([]),
        )
