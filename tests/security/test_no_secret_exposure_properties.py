"""Cross-cutting property-based tests for Property 22 (no secret exposure).

These tests validate Property 22 from the Gozar design:

    *For any* serialized account view, token view, trace entry, or log record
    produced by the system, the output contains no upstream credential secret, no
    subscription token, and no client-token secret value.

    Validates: Requirements 5.4, 8.3, 16.4.

This is the security backstop for the four surfaces through which a secret could
plausibly escape, and each surface is exercised through its **real** code path with
a randomly generated, high-entropy secret embedded in the originating input:

* **Account views** -- a metered API key is connected via
  :func:`gozar.accounts.service.connect_api_key` and a subscription token bundle is
  connected via :func:`~gozar.accounts.service.begin_subscription_connect` /
  :func:`~gozar.accounts.service.complete_subscription_connect`; the resulting
  accounts are listed with :func:`~gozar.accounts.service.list_accounts` and
  serialized with :class:`gozar.api.schemas.AccountResponse`. The encrypted secrets
  (the API key and the subscription access/refresh tokens) must never appear in the
  serialized view (Requirement 5.4).
* **Token views** -- a Client_Token is issued with the real
  :func:`gozar.tokens.service.create_token` (the secret is returned explicitly),
  listed with :func:`~gozar.tokens.service.list_tokens`, and serialized with
  :class:`gozar.api.schemas.TokenResponse`. The issued secret must never appear in
  the listing (Requirement 8.3).
* **Trace entries** -- a trace is opened and finalized with the real
  :func:`gozar.usage.service.open_trace` / :func:`~gozar.usage.service.finalize_trace`
  for a request whose originating context carried a secret, then serialized with
  :class:`gozar.api.schemas.TraceDetailResponse`. The trace metadata value objects
  expose only non-secret fields, so the secret from the originating request can never
  reach the stored trace (Requirements 14.x, 16.4).
* **Log records** -- secret-bearing payloads are pushed through the real logging
  redaction path (:func:`gozar.core.logging.redact`, the
  :class:`~gozar.core.logging.SecretRedactingFilter`, and the
  :class:`~gozar.core.logging.JsonFormatter`); the emitted JSON must contain no
  secret value (Requirement 16.4).

Persistence-backed surfaces run against a fresh in-memory SQLite database per
example (the project's test convention; see ``tests/usage/test_trace_roundtrip_properties.py``)
so the tests are hermetic -- no real database, Redis, or network. Network and Redis
seams are satisfied with injected fakes (a no-op API-key validator, an in-memory
PKCE pending store, and an injected token exchange).

Secrets are generated as high-entropy alphanumeric strings (>=24 chars) so a
generated secret cannot coincidentally collide with a provider id, label, UUID, or
ISO timestamp in the serialized output, which would otherwise mask a real leak.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from gozar.accounts.models import (
    AccountUsageLimit,
    ApiKeySecret,
    SubscriptionSecret,
    UpstreamCredential,
)
from gozar.accounts.service import (
    PendingConnectStore,
    begin_subscription_connect,
    complete_subscription_connect,
    connect_api_key,
    list_accounts,
)
from gozar.api.schemas import AccountResponse, TokenResponse, TraceDetailResponse
from gozar.core.config import Settings
from gozar.core.db import Base
from gozar.core.logging import (
    REDACTED,
    JsonFormatter,
    SecretRedactingFilter,
    redact,
)
from gozar.providers.registry import ProviderEntry
from gozar.tokens.models import ClientToken, TokenUsageLimit
from gozar.tokens.service import _TOKEN_SCHEME, create_token, list_tokens
from gozar.usage.models import TraceLog
from gozar.usage.service import (
    TRACE_OUTCOMES,
    InboundMeta,
    OutboundMeta,
    finalize_trace,
    open_trace,
)

# A deterministic, well-formed 32-byte master key (base64) for envelope encryption.
_TEST_MASTER_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()


def _settings() -> Settings:
    """Test settings with envelope-encryption, token-pepper, and provider config.

    Includes a base URL for the API-key provider (``openai``) and full OAuth metadata
    for a subscription provider (``codex``) so the real connect code paths resolve a
    provider entry. The token exchange and API-key validation are injected, so no
    network call is made.
    """
    return Settings(
        master_key=_TEST_MASTER_KEY,
        token_pepper="prop22-client-token-pepper",
        jwt_secret="prop22-jwt-secret",
        redis_url="redis://localhost:6379/0",
        provider_base_urls={
            "openai": "https://api.openai.com/v1",
            "codex": "https://chatgpt.com/backend-api/codex",
        },
        provider_oauth={
            "codex": {
                "authorize_url": "https://auth.example.test/authorize",
                "token_url": "https://auth.example.test/token",
                "client_id": "gozar-test-client",
                "redirect_uri": "https://gozar.test/callback",
                "scopes": ["openid", "profile"],
            }
        },
    )


_SETTINGS = _settings()

# High-entropy secret material: long enough that a generated value cannot collide
# with any non-secret token in the serialized output (provider ids, labels, UUIDs).
_ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_secret = st.text(alphabet=_ALNUM, min_size=24, max_size=64)

# Safe, non-secret free-text for trace metadata fields. Restricted to lowercase
# letters and spaces so a generated metadata value can never coincidentally equal or
# contain the sentinel-prefixed secret below (the trace fields are legitimately
# free-text, so an accidental string collision would be a false positive, not a leak).
_safe_text = st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", max_size=32)

# Trace secret carries a distinctive sentinel (uppercase + underscore) that the
# lowercase-only ``_safe_text`` strategy cannot produce, so any appearance of the
# secret in the serialized trace is a genuine leak rather than a generator collision.
_trace_secret = st.text(alphabet=_ALNUM, min_size=24, max_size=64).map(
    lambda body: f"SENTINEL_SECRET_{body}"
)


@st.composite
def _secret_shaped(draw: st.DrawFn) -> str:
    """Generate a value matching one of the known secret value-patterns.

    Mirrors the shapes :func:`gozar.core.logging.redact` recognises by value
    (a Gozar client token, an OpenAI/Anthropic ``sk-`` key, or a ``Bearer`` header
    value) so the value-pattern redaction path is genuinely exercised.
    """
    body = draw(st.text(alphabet=_ALNUM, min_size=12, max_size=40))
    kind = draw(st.sampled_from(["gz", "sk", "sk-ant", "bearer"]))
    if kind == "gz":
        prefix = draw(st.text(alphabet=_ALNUM, min_size=2, max_size=8))
        return f"gz-{prefix}-{body}"
    if kind == "sk":
        return f"sk-{body}"
    if kind == "sk-ant":
        return f"sk-ant-{body}"
    return f"Bearer {body}"


# --- injected fakes (no network / no Redis) ----------------------------------
class _InMemoryPendingStore:
    """In-memory :class:`PendingConnectStore` for the subscription PKCE state."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    async def put(
        self, pending_id: str, data: dict[str, Any], ttl_seconds: int
    ) -> None:
        self._data[pending_id] = dict(data)

    async def get(self, pending_id: str) -> dict[str, Any] | None:
        value = self._data.get(pending_id)
        return dict(value) if value is not None else None

    async def delete(self, pending_id: str) -> None:
        self._data.pop(pending_id, None)


async def _noop_validate(entry: ProviderEntry, api_key: str) -> None:
    """Injected API-key validator: accept any key without an upstream call."""
    return None


def _engine_and_maker() -> tuple[Any, async_sessionmaker[AsyncSession]]:
    """Create a fresh in-memory SQLite engine and session factory."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, maker


# =============================================================================
# Surface A: account views
# =============================================================================
async def _build_account_views_json(
    api_key: str, sub_access: str, sub_refresh: str
) -> list[str]:
    """Connect an API-key account and a subscription account, then serialize views.

    Returns the JSON serialization of every :class:`AccountResponse` produced by the
    real :func:`list_accounts` path. The embedded secrets are the API key and the
    subscription access/refresh tokens.
    """
    engine, maker = _engine_and_maker()
    tables = [
        UpstreamCredential.__table__,
        SubscriptionSecret.__table__,
        ApiKeySecret.__table__,
        AccountUsageLimit.__table__,
    ]
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=tables)
        async with maker() as session:
            # API-key account: the key is validated (injected no-op) then encrypted.
            await connect_api_key(
                session,
                "openai",
                api_key,
                label="metered-account",
                settings=_SETTINGS,
                validate=_noop_validate,
            )

            # Subscription account: drive the real OAuth+PKCE connect with an
            # in-memory pending store and an injected token exchange that returns a
            # bundle carrying the secret access/refresh tokens.
            store: PendingConnectStore = _InMemoryPendingStore()
            challenge = await begin_subscription_connect(
                "codex", settings=_SETTINGS, store=store
            )

            async def _exchange(
                entry: ProviderEntry, code: str, verifier: str
            ) -> dict[str, Any]:
                return {
                    "access_token": sub_access,
                    "refresh_token": sub_refresh,
                    "expires_in": 3600,
                    "scope": "openid profile",
                }

            await complete_subscription_connect(
                session,
                challenge.pending_id,
                code="authorization-code",
                state=challenge.state,
                label="subscription-account",
                settings=_SETTINGS,
                store=store,
                exchange=_exchange,
            )

            views = await list_accounts(session)
            return [AccountResponse.from_view(view).model_dump_json() for view in views]
    finally:
        await engine.dispose()


# Feature: gozar, Property 22: No secret exposure in views, traces, or logs
@hyp_settings(max_examples=100, deadline=None)
@given(api_key=_secret, sub_access=_secret, sub_refresh=_secret)
def test_account_views_never_expose_secrets(
    api_key: str, sub_access: str, sub_refresh: str
) -> None:
    """Validates: Requirements 5.4, 16.4.

    For any connected API-key account and subscription account, the serialized
    account view (``AccountResponse``) contains neither the stored API key nor the
    subscription access/refresh tokens.
    """
    serialized = asyncio.run(
        _build_account_views_json(api_key, sub_access, sub_refresh)
    )
    # Two accounts were connected; both views must be secret-free.
    assert len(serialized) == 2
    for payload in serialized:
        assert api_key not in payload
        assert sub_access not in payload
        assert sub_refresh not in payload


# =============================================================================
# Surface B: token views
# =============================================================================
async def _build_token_views_json(label: str) -> tuple[str, list[str]]:
    """Issue a Client_Token, then serialize the listing.

    Returns the issued secret and the JSON serialization of every
    :class:`TokenResponse` produced by the real :func:`list_tokens` path.
    """
    engine, maker = _engine_and_maker()
    tables = [ClientToken.__table__, TokenUsageLimit.__table__]
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=tables)
        async with maker() as session:
            issued = await create_token(session, label, settings=_SETTINGS)
            views = await list_tokens(session)
            payloads = [TokenResponse.from_view(view).model_dump_json() for view in views]
            return issued.secret, payloads
    finally:
        await engine.dispose()


# Feature: gozar, Property 22: No secret exposure in views, traces, or logs
@hyp_settings(max_examples=100, deadline=None)
@given(label=st.text(min_size=1, max_size=64))
def test_token_views_never_expose_secrets(label: str) -> None:
    """Validates: Requirements 8.3, 16.4.

    For any issued Client_Token, the serialized token listing (``TokenResponse``)
    contains neither the full issued secret string nor its random secret portion.
    """
    issued_secret, serialized = asyncio.run(_build_token_views_json(label))
    assert len(serialized) == 1

    # The random secret portion of ``gz-<id_prefix>-<secret>`` (the truly sensitive
    # part) is checked in addition to the full presentable string.
    secret_portion = issued_secret.split("-", 2)[2]
    for payload in serialized:
        assert issued_secret not in payload
        assert secret_portion not in payload


# =============================================================================
# Surface C: trace entries
# =============================================================================
def _inbound_from_request(request: dict[str, Any]) -> InboundMeta:
    """Build trace inbound metadata the way the gateway would, from a request.

    The originating ``request`` carries secret material (an ``authorization`` header
    and the presented ``client_token``), but only the non-secret request-shape fields
    are extracted into the trace metadata -- the value object exposes no field through
    which a secret could be carried (Requirement 16.4).
    """
    return InboundMeta(
        method=request["method"],
        model=request.get("model"),
        stream=request.get("stream", False),
        session_id=request.get("session_id"),
        request_bytes=request.get("request_bytes"),
    )


async def _build_trace_json(
    inbound: InboundMeta, outbound: OutboundMeta, started: datetime, ended: datetime
) -> str:
    """Open and finalize a trace, then serialize it via ``TraceDetailResponse``."""
    engine, maker = _engine_and_maker()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(
                Base.metadata.create_all, tables=[TraceLog.__table__]
            )
        async with maker() as session:
            correlation_id = uuid.uuid4()
            await open_trace(session, correlation_id, inbound, now=started)
            await finalize_trace(session, correlation_id, outbound, now=ended)
            session.expire_all()
            row = await session.get(TraceLog, correlation_id)
            assert row is not None
            return TraceDetailResponse.from_trace(row).model_dump_json()
    finally:
        await engine.dispose()


# Feature: gozar, Property 22: No secret exposure in views, traces, or logs
@hyp_settings(max_examples=100, deadline=None)
@given(
    secret=_trace_secret,
    method=st.sampled_from(["GET", "POST", "PUT", "PATCH", "DELETE"]),
    model=st.none() | _safe_text,
    stream=st.booleans(),
    session_id=st.none() | _safe_text,
    request_bytes=st.none() | st.integers(min_value=0, max_value=10_000_000),
    outcome=st.sampled_from(TRACE_OUTCOMES),
    status_code=st.none() | st.integers(min_value=100, max_value=599),
    finish_reason=st.none() | _safe_text,
    response_bytes=st.none() | st.integers(min_value=0, max_value=10_000_000),
    elapsed_seconds=st.integers(min_value=0, max_value=86_400),
)
def test_trace_entries_never_expose_secrets(
    secret: str,
    method: str,
    model: str | None,
    stream: bool,
    session_id: str | None,
    request_bytes: int | None,
    outcome: str,
    status_code: int | None,
    finish_reason: str | None,
    response_bytes: int | None,
    elapsed_seconds: int,
) -> None:
    """Validates: Requirements 16.4.

    For a request whose originating context carried a secret (an authorization header
    and the presented client token), the persisted and serialized trace
    (``TraceDetailResponse``) contains none of that secret material: the trace
    metadata value objects expose only non-secret request/response-shape fields.
    """
    # The originating request carries secret material that must NOT reach the trace.
    request = {
        "method": method,
        "model": model,
        "stream": stream,
        "session_id": session_id,
        "request_bytes": request_bytes,
        "authorization": f"Bearer {secret}",
        "client_token": f"gz-prefix-{secret}",
    }
    inbound = _inbound_from_request(request)
    outbound = OutboundMeta(
        outcome=outcome,
        status_code=status_code,
        account_id=uuid.uuid4(),
        finish_reason=finish_reason,
        response_bytes=response_bytes,
    )

    started = datetime(2024, 1, 1, tzinfo=timezone.utc)
    ended = started + timedelta(seconds=elapsed_seconds)
    payload = asyncio.run(_build_trace_json(inbound, outbound, started, ended))

    assert secret not in payload


# =============================================================================
# Surface D: log records
# =============================================================================
def _emit_log_json(message: str, extra: dict[str, Any]) -> str:
    """Run a log record through the real redaction filter and JSON formatter.

    Builds a :class:`logging.LogRecord` directly (so the assertion is deterministic
    and does not depend on global capture across Hypothesis examples), applies the
    :class:`SecretRedactingFilter`, and renders it with :class:`JsonFormatter`.
    """
    record = logging.LogRecord(
        name="gozar.security.prop22",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    SecretRedactingFilter().filter(record)
    return JsonFormatter().format(record)


# Feature: gozar, Property 22: No secret exposure in views, traces, or logs
@hyp_settings(max_examples=200)
@given(
    key_secret=_secret,
    value_secret=_secret_shaped(),
    message_secret=_secret_shaped(),
)
def test_log_records_never_expose_secrets(
    key_secret: str, value_secret: str, message_secret: str
) -> None:
    """Validates: Requirements 16.4.

    For any payload carrying secret material -- under a secret-named key, as a
    secret-shaped value, or embedded in the log message -- the redaction utility and
    the structured-logging path mask every secret before emission, while non-secret
    fields survive.
    """
    # 1) The pure redaction utility scrubs both key-name and value-pattern secrets.
    payload = {
        "access_token": key_secret,
        "api_key": key_secret,
        "nested": {"refresh_token": key_secret, "note": value_secret},
        "correlation_id": "trace-1234",
    }
    redacted = redact(payload)
    assert redacted["access_token"] == REDACTED
    assert redacted["api_key"] == REDACTED
    assert redacted["nested"]["refresh_token"] == REDACTED
    # A secret-shaped value sitting in a non-secret-named field is still masked.
    assert value_secret not in redacted["nested"]["note"]
    # Non-secret metadata is preserved.
    assert redacted["correlation_id"] == "trace-1234"

    # 2) The structured-logging path (filter + formatter) masks secrets end to end.
    line = _emit_log_json(
        f"upstream call authenticated with {message_secret} completed",
        {
            "access_token": key_secret,
            "authorization": f"Bearer {key_secret}",
            "subscription_token": key_secret,
            "correlation_id": "trace-1234",
        },
    )
    # No secret material appears anywhere in the emitted JSON line.
    assert key_secret not in line
    assert value_secret not in line
    assert message_secret not in line
    # The line is still a useful, redacted structured record.
    assert REDACTED in line
    assert "trace-1234" in line
