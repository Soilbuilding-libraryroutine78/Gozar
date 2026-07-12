"""Unit tests for Account_Manager connect/validation (task 5.6).

These cover the subscription OAuth (PKCE) connect flow and the API-key connect/
validation flow without any real network or Redis: the pending PKCE store is an
in-memory fake, the token exchange and the API-key validation are injected, and
the database session is a minimal fake that captures the rows that would be
persisted.

Covered behaviour:

* ``begin_subscription_connect`` builds an authorize URL carrying the PKCE S256
  challenge + state and stores the verifier server-side (never returns it).
* ``begin_device_subscription_connect`` starts the Codex device-code flow without
  exposing upstream device-auth ids, and ``complete_device_subscription_connect``
  keeps pending sessions alive until approval.
* ``begin_subscription_connect`` refuses API-key providers.
* ``complete_subscription_connect`` validates state, exchanges the code, derives
  the provider account ref from the access-token claims, persists an encrypted
  subscription bundle, and consumes the pending state (Req 1.1, 1.2, 1.4).
* ``complete_subscription_connect`` returns a descriptive error and creates no
  account on a bad/expired state or a failed exchange (Req 1.3).
* ``connect_api_key`` validates the key before creating the account, stores it
  encrypted, and creates no account when validation fails (Req 2.1, 2.2, 2.3).
"""

from __future__ import annotations

import base64
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest

from gozar.accounts import service
from gozar.accounts.models import (
    ApiKeySecret,
    CredentialKind,
    SubscriptionSecret,
    UpstreamCredential,
)
from gozar.core.config import Settings
from gozar.core.errors import UpstreamError, ValidationError
from gozar.providers.registry import ProviderEntry

# A real base64-encoded 32-byte master key so envelope encryption works in-process.
_MASTER_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")


def _settings(**overrides) -> Settings:
    base = {
        "master_key": _MASTER_KEY,
        "redis_url": "redis://localhost:6379/0",
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
    """In-memory PendingConnectStore for tests."""

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

    async def flush(self) -> None:  # noqa: D401 - test stub
        return None


def _access_token(account_id: str = "acct-123") -> str:
    return jwt.encode(
        {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}},
        "irrelevant-test-secret",
        algorithm="HS256",
    )


def _builtin_settings(**overrides) -> Settings:
    """Settings with no provider config so the registry's built-in defaults apply."""
    base = {
        "master_key": _MASTER_KEY,
        "redis_url": "redis://localhost:6379/0",
        "provider_base_urls": {},
        "provider_oauth": {},
    }
    base.update(overrides)
    return Settings(**base)


async def test_begin_subscription_connect_builds_pkce_authorize_url():
    settings = _settings()
    store = _MemStore()

    challenge = await service.begin_subscription_connect(
        "codex", settings=settings, store=store
    )

    parts = urlsplit(challenge.authorize_url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
        "https://auth.openai.com/oauth/authorize"
    )
    query = parse_qs(parts.query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == [challenge.state]
    assert query["client_id"] == ["sample-client"]
    assert query["code_challenge"][0]  # present and non-empty
    assert query["scope"] == ["openid profile email"]

    # The verifier is held server-side and never surfaced on the challenge.
    stored = store.data[challenge.pending_id]
    assert stored["state"] == challenge.state
    assert stored["verifier"]
    assert challenge.state not in stored["verifier"]


async def test_begin_codex_authorize_url_uses_offline_access_and_extra_params():
    """The Codex authorize URL (built-in defaults) requests a refresh token and
    carries the provider-specific extra params openclaw sends."""
    settings = _builtin_settings()
    store = _MemStore()

    challenge = await service.begin_subscription_connect(
        "codex", settings=settings, store=store
    )

    query = parse_qs(urlsplit(challenge.authorize_url).query)
    # offline_access is required to be issued a refresh_token.
    assert "offline_access" in query["scope"][0].split()
    # Extra authorize params from the built-in Codex defaults.
    assert query["id_token_add_organizations"] == ["true"]
    assert query["codex_cli_simplified_flow"] == ["true"]
    assert query["originator"] == ["codex_cli_rs"]
    # The public Codex client id is supplied as a built-in default.
    assert query["client_id"] == ["app_EMoamEEZ73f0CkXaXp7hrann"]


async def test_begin_codex_device_connect_stores_server_side_challenge():
    settings = _settings()
    store = _MemStore()

    async def fake_request_device_code(entry):
        assert entry.provider_id.value == "codex"
        return {
            "device_auth_id": "device-auth-123",
            "user_code": "ABCD-EFGH",
            "interval": 4,
        }

    challenge = await service.begin_device_subscription_connect(
        "codex",
        settings=settings,
        store=store,
        request_device_code=fake_request_device_code,
    )

    assert challenge.verification_url == "https://auth.openai.com/codex/device"
    assert challenge.user_code == "ABCD-EFGH"
    assert challenge.interval_seconds == 4

    stored = store.data[challenge.pending_id]
    assert stored == {
        "provider": "codex",
        "flow": "device_code",
        "device_auth_id": "device-auth-123",
        "user_code": "ABCD-EFGH",
    }


async def test_complete_codex_device_connect_pending_keeps_session_open():
    settings = _settings()
    store = _MemStore()

    async def fake_request_device_code(entry):
        return {
            "device_auth_id": "device-auth-123",
            "user_code": "ABCD-EFGH",
            "interval": 5,
        }

    challenge = await service.begin_device_subscription_connect(
        "codex",
        settings=settings,
        store=store,
        request_device_code=fake_request_device_code,
    )

    async def pending_poll(entry, device_auth_id, user_code):
        assert device_auth_id == "device-auth-123"
        assert user_code == "ABCD-EFGH"
        return None

    session = _FakeSession()
    outcome = await service.complete_device_subscription_connect(
        session,
        challenge.pending_id,
        settings=settings,
        store=store,
        poll_device_code=pending_poll,
    )

    assert outcome.pending is True
    assert outcome.credential is None
    assert session.added == []
    assert challenge.pending_id in store.data


async def test_complete_codex_device_connect_approval_persists_encrypted_bundle():
    settings = _settings()
    store = _MemStore()

    async def fake_request_device_code(entry):
        return {
            "device_auth_id": "device-auth-123",
            "user_code": "ABCD-EFGH",
            "interval": 5,
        }

    challenge = await service.begin_device_subscription_connect(
        "codex",
        settings=settings,
        store=store,
        request_device_code=fake_request_device_code,
    )

    async def approved_poll(entry, device_auth_id, user_code):
        return {
            "authorization_code": "approved-code",
            "code_verifier": "approved-verifier",
        }

    captured: dict = {}

    async def fake_exchange(entry, code, verifier):
        captured["code"] = code
        captured["verifier"] = verifier
        return {
            "access_token": _access_token("acct-device"),
            "refresh_token": "refresh-device",
            "expires_in": 3600,
        }

    session = _FakeSession()
    outcome = await service.complete_device_subscription_connect(
        session,
        challenge.pending_id,
        label="Codex device",
        settings=settings,
        store=store,
        poll_device_code=approved_poll,
        exchange=fake_exchange,
    )

    assert outcome.pending is False
    assert outcome.credential is not None
    assert outcome.credential.label == "Codex device"
    assert outcome.credential.provider_account_ref == "acct-device"
    assert captured == {"code": "approved-code", "verifier": "approved-verifier"}

    secrets_added = [r for r in session.added if isinstance(r, SubscriptionSecret)]
    assert len(secrets_added) == 1
    assert b"refresh-device" not in secrets_added[0].ciphertext
    assert challenge.pending_id not in store.data


async def test_complete_derives_account_ref_from_id_token_first():
    """When the exchange returns both id_token and access_token, the account ref is
    taken from the id_token first (matching the upstream claim order)."""
    settings = _builtin_settings()
    store = _MemStore()
    challenge = await service.begin_subscription_connect(
        "codex", settings=settings, store=store
    )

    async def fake_exchange(entry, code, verifier):
        return {
            "id_token": _access_token("acct-from-id-token"),
            "access_token": _access_token("acct-from-access-token"),
            "refresh_token": "refresh-abc",
            "expires_in": 3600,
        }

    session = _FakeSession()
    credential = await service.complete_subscription_connect(
        session,
        challenge.pending_id,
        code="auth-code",
        state=challenge.state,
        settings=settings,
        store=store,
        exchange=fake_exchange,
    )
    assert credential.provider_account_ref == "acct-from-id-token"


async def test_complete_falls_back_to_access_token_when_no_id_token():
    settings = _builtin_settings()
    store = _MemStore()
    challenge = await service.begin_subscription_connect(
        "codex", settings=settings, store=store
    )

    async def fake_exchange(entry, code, verifier):
        return {
            "access_token": _access_token("acct-from-access-token"),
            "refresh_token": "r",
            "expires_in": 3600,
        }

    session = _FakeSession()
    credential = await service.complete_subscription_connect(
        session,
        challenge.pending_id,
        code="auth-code",
        state=challenge.state,
        settings=settings,
        store=store,
        exchange=fake_exchange,
    )
    assert credential.provider_account_ref == "acct-from-access-token"


async def test_complete_accepts_full_redirect_url_paste():
    """The operator may paste the full loopback redirect URL instead of a bare code;
    the code (and state) are extracted and the connect still succeeds."""
    settings = _builtin_settings()
    store = _MemStore()
    challenge = await service.begin_subscription_connect(
        "codex", settings=settings, store=store
    )

    captured: dict = {}

    async def fake_exchange(entry, code, verifier):
        captured["code"] = code
        return {
            "access_token": _access_token("acct-xyz"),
            "refresh_token": "r",
            "expires_in": 3600,
        }

    pasted = (
        f"http://localhost:1455/auth/callback?code=the-real-code&state={challenge.state}"
    )
    session = _FakeSession()
    credential = await service.complete_subscription_connect(
        session,
        challenge.pending_id,
        code=pasted,
        state="",  # operator left the separate state field empty; taken from the URL
        settings=settings,
        store=store,
        exchange=fake_exchange,
    )
    assert captured["code"] == "the-real-code"
    assert credential.provider == "codex"


def test_extract_code_and_state_bare_code():
    """A bare authorization code yields the code unchanged and no state."""
    code, state = service.extract_code_and_state("  bare-auth-code  ")
    assert code == "bare-auth-code"
    assert state is None


def test_extract_code_and_state_full_redirect_url_with_code_and_state():
    """A full loopback redirect URL yields both the code and the state."""
    pasted = "http://localhost:1455/auth/callback?code=abc123&state=xyz789"
    code, state = service.extract_code_and_state(pasted)
    assert code == "abc123"
    assert state == "xyz789"


def test_extract_code_and_state_url_with_code_only():
    """A redirect URL carrying only ``code`` yields the code and no state."""
    code, state = service.extract_code_and_state(
        "http://localhost:53692/callback?code=onlycode"
    )
    assert code == "onlycode"
    assert state is None


def test_extract_code_and_state_bare_query_fragment():
    """A bare ``code=...&state=...`` fragment (no scheme/host) is parsed too."""
    code, state = service.extract_code_and_state("code=frag-code&state=frag-state")
    assert code == "frag-code"
    assert state == "frag-state"


def test_extract_code_and_state_junk_is_treated_as_bare_code():
    """Junk with no ``code=`` is returned verbatim (stripped) as a bare code."""
    code, state = service.extract_code_and_state("not a url at all")
    assert code == "not a url at all"
    assert state is None


async def test_complete_accepts_bare_code_without_state():
    """Manual-paste fallback: the operator pastes only the bare code (no state). The
    completion proceeds keyed solely by the unguessable pending_id and succeeds."""
    settings = _builtin_settings()
    store = _MemStore()
    challenge = await service.begin_subscription_connect(
        "codex", settings=settings, store=store
    )

    captured: dict = {}

    async def fake_exchange(entry, code, verifier):
        captured["code"] = code
        return {
            "access_token": _access_token("acct-bare"),
            "refresh_token": "r",
            "expires_in": 3600,
        }

    session = _FakeSession()
    credential = await service.complete_subscription_connect(
        session,
        challenge.pending_id,
        code="just-the-bare-code",
        # state omitted entirely (defaults to None) -- the manual-paste fallback.
        settings=settings,
        store=store,
        exchange=fake_exchange,
    )
    assert captured["code"] == "just-the-bare-code"
    assert credential.provider == "codex"
    # Pending state is still consumed so it cannot be replayed.
    assert challenge.pending_id not in store.data


async def test_default_exchange_payload_is_provider_shaped(monkeypatch):
    """Codex exchanges form-encoded without state; Anthropic exchanges JSON with the
    anti-CSRF state echoed back, all driven by the provider's OAuth metadata."""
    from gozar.providers.registry import ProviderId, get_provider

    captured: dict = {}

    async def fake_post(entry, payload, *, settings):
        captured["format"] = entry.oauth.token_request_format
        captured["payload"] = payload
        return {"access_token": "x"}

    monkeypatch.setattr(service, "_post_token_request", fake_post)
    settings = _builtin_settings()

    codex = get_provider(ProviderId.CODEX, settings=settings)
    await service._default_exchange(
        codex, "the-code", "the-verifier", state="the-state", settings=settings
    )
    assert captured["format"] == "form"
    assert "state" not in captured["payload"]
    assert captured["payload"]["code_verifier"] == "the-verifier"

    anthropic = get_provider(ProviderId.ANTHROPIC, settings=settings)
    await service._default_exchange(
        anthropic, "the-code", "the-verifier", state="the-state", settings=settings
    )
    assert captured["format"] == "json"
    assert captured["payload"]["state"] == "the-state"


async def test_begin_subscription_connect_rejects_api_key_provider():
    settings = _settings()
    with pytest.raises(ValidationError):
        await service.begin_subscription_connect(
            "openai", settings=settings, store=_MemStore()
        )


async def test_complete_subscription_connect_success_persists_encrypted_bundle():
    settings = _settings()
    store = _MemStore()
    challenge = await service.begin_subscription_connect(
        "codex", settings=settings, store=store
    )
    expected_verifier = store.data[challenge.pending_id]["verifier"]

    captured: dict = {}

    async def fake_exchange(entry: ProviderEntry, code: str, verifier: str):
        captured["entry"] = entry
        captured["code"] = code
        captured["verifier"] = verifier
        return {
            "access_token": _access_token("acct-xyz"),
            "refresh_token": "refresh-abc",
            "expires_in": 3600,
            "scope": "openid profile",
        }

    session = _FakeSession()
    credential = await service.complete_subscription_connect(
        session,
        challenge.pending_id,
        code="auth-code",
        state=challenge.state,
        settings=settings,
        store=store,
        exchange=fake_exchange,
    )

    # The stored PKCE verifier was used for the exchange.
    assert captured["verifier"] == expected_verifier
    assert captured["code"] == "auth-code"

    assert isinstance(credential, UpstreamCredential)
    assert credential.kind is CredentialKind.SUBSCRIPTION
    assert credential.provider == "codex"
    assert credential.provider_account_ref == "acct-xyz"

    secrets_added = [r for r in session.added if isinstance(r, SubscriptionSecret)]
    assert len(secrets_added) == 1
    secret = secrets_added[0]
    # Stored material is ciphertext, never the plaintext token.
    assert b"refresh-abc" not in secret.ciphertext
    assert secret.expires_at is not None

    # Pending state consumed (cannot be replayed).
    assert challenge.pending_id not in store.data


async def test_complete_rejects_bad_state_and_creates_no_account():
    settings = _settings()
    store = _MemStore()
    challenge = await service.begin_subscription_connect(
        "codex", settings=settings, store=store
    )
    session = _FakeSession()

    async def fake_exchange(entry, code, verifier):  # pragma: no cover - must not run
        raise AssertionError("exchange must not be called on a state mismatch")

    with pytest.raises(ValidationError):
        await service.complete_subscription_connect(
            session,
            challenge.pending_id,
            code="auth-code",
            state="wrong-state",
            settings=settings,
            store=store,
            exchange=fake_exchange,
        )
    assert session.added == []
    # The pending state is consumed on a CSRF-style mismatch.
    assert challenge.pending_id not in store.data


async def test_complete_unknown_pending_id_raises():
    settings = _settings()
    session = _FakeSession()
    with pytest.raises(ValidationError):
        await service.complete_subscription_connect(
            session,
            "does-not-exist",
            code="c",
            state="s",
            settings=settings,
            store=_MemStore(),
        )
    assert session.added == []


async def test_complete_exchange_failure_creates_no_account():
    settings = _settings()
    store = _MemStore()
    challenge = await service.begin_subscription_connect(
        "codex", settings=settings, store=store
    )
    session = _FakeSession()

    async def failing_exchange(entry, code, verifier):
        raise UpstreamError("token endpoint rejected the code")

    with pytest.raises(ValidationError):
        await service.complete_subscription_connect(
            session,
            challenge.pending_id,
            code="bad-code",
            state=challenge.state,
            settings=settings,
            store=store,
            exchange=failing_exchange,
        )
    assert session.added == []


async def test_complete_missing_access_token_creates_no_account():
    settings = _settings()
    store = _MemStore()
    challenge = await service.begin_subscription_connect(
        "codex", settings=settings, store=store
    )
    session = _FakeSession()

    async def empty_exchange(entry, code, verifier):
        return {"refresh_token": "r", "expires_in": 60}

    with pytest.raises(ValidationError):
        await service.complete_subscription_connect(
            session,
            challenge.pending_id,
            code="c",
            state=challenge.state,
            settings=settings,
            store=store,
            exchange=empty_exchange,
        )
    assert session.added == []


async def test_connect_api_key_success_validates_then_persists_encrypted():
    settings = _settings()
    session = _FakeSession()
    calls: dict = {}

    async def fake_validate(entry: ProviderEntry, api_key: str):
        calls["validated"] = api_key

    credential = await service.connect_api_key(
        session,
        "openai",
        "sk-secret-key-value",
        label="my-openai",
        settings=settings,
        validate=fake_validate,
    )

    # Validation ran before creation.
    assert calls["validated"] == "sk-secret-key-value"
    assert isinstance(credential, UpstreamCredential)
    assert credential.kind is CredentialKind.API_KEY
    assert credential.label == "my-openai"

    secrets_added = [r for r in session.added if isinstance(r, ApiKeySecret)]
    assert len(secrets_added) == 1
    assert b"sk-secret-key-value" not in secrets_added[0].ciphertext


async def test_connect_api_key_validation_failure_creates_no_account():
    settings = _settings()
    session = _FakeSession()

    async def failing_validate(entry, api_key):
        raise UpstreamError("provider returned 401")

    with pytest.raises(ValidationError):
        await service.connect_api_key(
            session,
            "openai",
            "sk-bad",
            settings=settings,
            validate=failing_validate,
        )
    assert session.added == []


async def test_connect_api_key_rejects_subscription_provider():
    settings = _settings()
    session = _FakeSession()

    async def unused_validate(entry, api_key):  # pragma: no cover - must not run
        raise AssertionError("validation must not run for a subscription provider")

    with pytest.raises(ValidationError):
        await service.connect_api_key(
            session,
            "codex",
            "sk-x",
            settings=settings,
            validate=unused_validate,
        )
    assert session.added == []


async def test_empty_api_key_rejected():
    settings = _settings()
    session = _FakeSession()
    with pytest.raises(ValidationError):
        await service.connect_api_key(session, "openai", "   ", settings=settings)
    assert session.added == []
