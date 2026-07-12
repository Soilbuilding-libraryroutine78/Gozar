"""Unit/integration tests for the non-streaming chat-completions pipeline.

These call :func:`gozar.gateway.pipeline.complete_chat_completion` directly with an
in-memory database session, an in-memory Redis fake, and an injected upstream caller,
so the full hot path (auth -> limit -> routing/fallback -> translation -> upstream ->
usage/trace recording) is exercised without any network or real Redis. Credential
acquisition is injected so these tests focus on orchestration; the real
``get_usable_token`` decrypt path is covered end-to-end in ``test_router.py``.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from gozar.accounts.models import (
    CredentialKind,
    CredentialStatus,
    UpstreamCredential,
)
from gozar.accounts.service import ProviderCredentialMaterial
from gozar.core.errors import (
    AuthError,
    NoAvailableAccount,
    RateLimitError,
    UpstreamError,
)
from gozar.gateway.pipeline import complete_chat_completion
from gozar.routing.chains import FallbackPolicy
from gozar.routing.service import ChainEntryInput, create_chain
from gozar.routing.session import record_session_binding
from gozar.tokens.service import create_token
from gozar.translation.types import OpenAIChatRequest
from gozar.usage.limits import LimitMetric, LimitWindow, UsageLimitSpec
from gozar.usage.models import TraceLog, UsageRecord
from gozar.usage.service import SUBJECT_TOKEN, counter_key

from conftest import material_for, openai_response


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class FakeUpstream:
    """Injected :data:`UpstreamCaller` that records calls and can fail per-account."""

    def __init__(self, *, fail_for=frozenset(), response=None) -> None:
        self.calls: list[uuid.UUID] = []
        self._fail_for = set(fail_for)
        self._response = response or openai_response()

    async def __call__(self, entry, material, adapter, body) -> dict:
        self.calls.append(material.account_id)
        if material.account_id in self._fail_for:
            raise UpstreamError("simulated upstream failure")
        return self._response


class FailsFirstWithStatus:
    """Injected upstream caller that fails once with a chosen upstream status."""

    def __init__(self, status_code: int, *, response=None) -> None:
        self.calls: list[tuple[uuid.UUID, str | None]] = []
        self._status_code = status_code
        self._response = response or openai_response()

    async def __call__(self, entry, material, adapter, body) -> dict:
        self.calls.append((material.account_id, material.access_token))
        if len(self.calls) == 1:
            raise UpstreamError(
                f"upstream provider {entry.provider_id.value!r} returned "
                f"status {self._status_code}",
                details=[{"upstream_status": self._status_code}],
            )
        return self._response


class RecordsEffectiveModels:
    """Fail the first target and capture the provider-specific model sent to each."""

    def __init__(self, first_status: int = 401) -> None:
        self.calls: list[tuple[uuid.UUID, str]] = []
        self._first_status = first_status

    async def __call__(self, entry, material, adapter, body) -> dict:
        self.calls.append((material.account_id, body.model))
        if len(self.calls) == 1:
            raise UpstreamError(
                "first provider failed",
                details=[{"upstream_status": self._first_status}],
            )
        return openai_response(content="provider-specific fallback", model=body.model)


def _acquire_fake(provider: str = "openai"):
    async def acquire(session, account_id):
        return material_for(account_id, provider=provider)

    return acquire


def _acquire_by_account(providers: dict[uuid.UUID, str]):
    async def acquire(session, account_id):
        return material_for(account_id, provider=providers[account_id])

    return acquire


def _refreshable_subscription_acquire(refreshed: dict[str, bool]):
    async def acquire(session, account_id):
        token = "access-new" if refreshed["done"] else "access-old"
        return ProviderCredentialMaterial(
            account_id=account_id,
            provider="openai",
            kind=CredentialKind.SUBSCRIPTION,
            access_token=token,
            api_key=None,
            provider_account_ref=None,
            expires_at=None,
        )

    return acquire


async def _add_account(
    session,
    *,
    provider: str = "openai",
    status: CredentialStatus = CredentialStatus.ACTIVE,
) -> uuid.UUID:
    account_id = uuid.uuid4()
    session.add(
        UpstreamCredential(
            id=account_id,
            provider=provider,
            kind=CredentialKind.API_KEY,
            label=f"acct-{account_id.hex[:6]}",
            status=status,
        )
    )
    await session.flush()
    return account_id


def _request(model: str = "gpt-4o", stream: bool = False) -> OpenAIChatRequest:
    return OpenAIChatRequest(
        model=model,
        messages=[{"role": "user", "content": "ping"}],
        stream=stream,
    )


async def _issue_token(session, settings, limit=None, assigned_chain_id=None) -> str:
    issued = await create_token(
        session,
        "test-token",
        limit,
        assigned_chain_id,
        settings=settings,
    )
    return issued.secret


# --------------------------------------------------------------------------- #
# Authentication (Requirement 6.2)
# --------------------------------------------------------------------------- #
async def test_missing_token_rejected_without_upstream(session, redis, settings):
    upstream = FakeUpstream()
    with pytest.raises(AuthError):
        await complete_chat_completion(
            session,
            presented_token=None,
            request=_request(),
            redis=redis,
            settings=settings,
            upstream=upstream,
            acquire_material=_acquire_fake(),
        )
    assert upstream.calls == []


async def test_invalid_token_rejected_without_upstream(session, redis, settings):
    upstream = FakeUpstream()
    with pytest.raises(AuthError):
        await complete_chat_completion(
            session,
            presented_token="gz-deadbeef-not-a-real-secret",
            request=_request(),
            redis=redis,
            settings=settings,
            upstream=upstream,
            acquire_material=_acquire_fake(),
        )
    assert upstream.calls == []

    # The trace was opened and finalized as a client error (Requirement 14.x).
    trace = (await session.scalars(select(TraceLog))).first()
    assert trace is not None
    assert trace.outcome == "client_error"
    assert trace.status_code == 401


# --------------------------------------------------------------------------- #
# Successful round-trip + usage/trace recording (Requirements 6.1, 13.1, 14.x)
# --------------------------------------------------------------------------- #
async def test_successful_round_trip_records_usage_and_trace(session, redis, settings):
    token = await _issue_token(session, settings)
    account_id = await _add_account(session)
    await create_chain(session, "default", [account_id])

    upstream = FakeUpstream(response=openai_response(content="pong"))
    response = await complete_chat_completion(
        session,
        presented_token=token,
        request=_request(),
        redis=redis,
        settings=settings,
        upstream=upstream,
        acquire_material=_acquire_fake(),
    )

    assert upstream.calls == [account_id]
    assert response.choices[0].message.content == "pong"
    assert response.usage is not None and response.usage.total_tokens == 18

    record = (await session.scalars(select(UsageRecord))).one()
    assert record.account_id == account_id
    assert record.total_tokens == 18
    assert record.provider_metering_missing is False

    trace = (await session.scalars(select(TraceLog))).one()
    assert trace.outcome == "success"
    assert trace.status_code == 200
    assert trace.account_id == account_id


async def test_missing_provider_metering_flagged(session, redis, settings):
    token = await _issue_token(session, settings)
    account_id = await _add_account(session)
    await create_chain(session, "default", [account_id])

    no_usage = openai_response()
    no_usage.pop("usage")
    upstream = FakeUpstream(response=no_usage)

    await complete_chat_completion(
        session,
        presented_token=token,
        request=_request(),
        redis=redis,
        settings=settings,
        upstream=upstream,
        acquire_material=_acquire_fake(),
    )

    record = (await session.scalars(select(UsageRecord))).one()
    assert record.total_tokens == 0
    assert record.provider_metering_missing is True


# --------------------------------------------------------------------------- #
# Terminal errors (Requirements 6.4, 10.3)
# --------------------------------------------------------------------------- #
async def test_no_chain_configured_yields_no_available_account(
    session, redis, settings
):
    token = await _issue_token(session, settings)
    upstream = FakeUpstream()
    with pytest.raises(NoAvailableAccount):
        await complete_chat_completion(
            session,
            presented_token=token,
            request=_request(),
            redis=redis,
            settings=settings,
            upstream=upstream,
            acquire_material=_acquire_fake(),
        )
    assert upstream.calls == []


async def test_all_entries_unavailable_yields_no_available_account(
    session, redis, settings
):
    token = await _issue_token(session, settings)
    disabled = await _add_account(session, status=CredentialStatus.DISABLED)
    reauth = await _add_account(session, status=CredentialStatus.REQUIRES_REAUTH)
    await create_chain(session, "default", [disabled, reauth])

    upstream = FakeUpstream()
    with pytest.raises(NoAvailableAccount):
        await complete_chat_completion(
            session,
            presented_token=token,
            request=_request(),
            redis=redis,
            settings=settings,
            upstream=upstream,
            acquire_material=_acquire_fake(),
        )
    assert upstream.calls == []

    trace = (await session.scalars(select(TraceLog))).one()
    assert trace.outcome == "no_account"


async def test_all_fallbacks_failed(session, redis, settings):
    token = await _issue_token(session, settings)
    first = await _add_account(session)
    second = await _add_account(session)
    await create_chain(session, "default", [first, second])

    upstream = FakeUpstream(fail_for={first, second})
    with pytest.raises(UpstreamError) as exc_info:
        await complete_chat_completion(
            session,
            presented_token=token,
            request=_request(),
            redis=redis,
            settings=settings,
            upstream=upstream,
            acquire_material=_acquire_fake(),
        )
    # Both available credentials were attempted before giving up.
    assert upstream.calls == [first, second]
    assert "last error: simulated upstream failure" in exc_info.value.message

    trace = (await session.scalars(select(TraceLog))).one()
    assert trace.outcome == "all_fallbacks_failed"


# --------------------------------------------------------------------------- #
# Fallback advances on upstream error (Requirements 10.2, 11.x)
# --------------------------------------------------------------------------- #
async def test_fallback_advances_to_next_on_upstream_error(session, redis, settings):
    token = await _issue_token(session, settings)
    first = await _add_account(session)
    second = await _add_account(session)
    chain = await create_chain(session, "default", [first, second])

    upstream = FakeUpstream(fail_for={first}, response=openai_response(content="ok"))
    response = await complete_chat_completion(
        session,
        presented_token=token,
        request=_request(),
        redis=redis,
        settings=settings,
        upstream=upstream,
        acquire_material=_acquire_fake(),
    )

    assert upstream.calls == [first, second]
    assert response.choices[0].message.content == "ok"

    record = (await session.scalars(select(UsageRecord))).one()
    assert record.account_id == second

    trace = (await session.scalars(select(TraceLog))).one()
    assert trace.inbound_meta["chain_id"] == str(chain.chain_id)
    routing = trace.outbound_meta["routing"]
    assert routing["chain_id"] == str(chain.chain_id)
    assert routing["attempt_count"] == 2
    assert routing["selected_position"] == 1
    first_attempt, selected_attempt = routing["attempts"]
    assert first_attempt["position"] == 0
    assert first_attempt["outcome"] == "error"
    assert first_attempt["fallback_taken"] is True
    assert first_attempt["error"]["code"] == "UPSTREAM_ERROR"
    assert selected_attempt["position"] == 1
    assert selected_attempt["outcome"] == "success"
    assert selected_attempt["usage"]["total_tokens"] == 18


async def test_fallback_rewrites_model_for_each_provider_node(session, redis, settings):
    token = await _issue_token(session, settings)
    primary = await _add_account(session, provider="openai")
    fallback = await _add_account(session, provider="openrouter")
    await create_chain(
        session,
        "provider-aware",
        [
            ChainEntryInput(
                primary,
                "gpt-5.4-mini",
                FallbackPolicy.AUTH_OR_RETRYABLE,
            ),
            ChainEntryInput(fallback, "google/gemini-2.5-flash"),
        ],
    )

    upstream = RecordsEffectiveModels(first_status=401)
    response = await complete_chat_completion(
        session,
        presented_token=token,
        request=_request(model="route-input"),
        redis=redis,
        settings=settings,
        upstream=upstream,
        acquire_material=_acquire_by_account(
            {primary: "openai", fallback: "openrouter"}
        ),
    )

    assert upstream.calls == [
        (primary, "gpt-5.4-mini"),
        (fallback, "google/gemini-2.5-flash"),
    ]
    assert response.choices[0].message.content == "provider-specific fallback"


async def test_retryable_policy_stops_on_non_retryable_provider_error(
    session, redis, settings
):
    token = await _issue_token(session, settings)
    first = await _add_account(session)
    second = await _add_account(session)
    await create_chain(
        session,
        "strict",
        [
            ChainEntryInput(first, "gpt-primary", FallbackPolicy.RETRYABLE),
            ChainEntryInput(second, "gpt-fallback"),
        ],
    )

    upstream = RecordsEffectiveModels(first_status=400)
    with pytest.raises(UpstreamError):
        await complete_chat_completion(
            session,
            presented_token=token,
            request=_request(),
            redis=redis,
            settings=settings,
            upstream=upstream,
            acquire_material=_acquire_fake(),
        )

    assert upstream.calls == [(first, "gpt-primary")]


async def test_subscription_401_refreshes_and_retries_same_credential(
    session, redis, settings
):
    token = await _issue_token(session, settings)
    account_id = await _add_account(session)
    await create_chain(session, "default", [account_id])

    refreshed = {"done": False}
    refresh_calls: list[uuid.UUID] = []

    async def refresh_on_auth_error(session, refresh_account_id):
        refresh_calls.append(refresh_account_id)
        refreshed["done"] = True
        return True

    upstream = FailsFirstWithStatus(
        401,
        response=openai_response(content="served after refresh"),
    )
    response = await complete_chat_completion(
        session,
        presented_token=token,
        request=_request(),
        redis=redis,
        settings=settings,
        upstream=upstream,
        acquire_material=_refreshable_subscription_acquire(refreshed),
        refresh_on_auth_error=refresh_on_auth_error,
    )

    assert response.choices[0].message.content == "served after refresh"
    assert refresh_calls == [account_id]
    assert upstream.calls == [
        (account_id, "access-old"),
        (account_id, "access-new"),
    ]


async def test_api_key_401_does_not_trigger_subscription_refresh(
    session, redis, settings
):
    token = await _issue_token(session, settings)
    first = await _add_account(session)
    second = await _add_account(session)
    await create_chain(session, "default", [first, second])

    refresh_calls: list[uuid.UUID] = []

    async def refresh_on_auth_error(session, account_id):
        refresh_calls.append(account_id)
        return True

    upstream = FailsFirstWithStatus(401, response=openai_response(content="fallback ok"))
    response = await complete_chat_completion(
        session,
        presented_token=token,
        request=_request(),
        redis=redis,
        settings=settings,
        upstream=upstream,
        acquire_material=_acquire_fake(),
        refresh_on_auth_error=refresh_on_auth_error,
    )

    assert response.choices[0].message.content == "fallback ok"
    assert refresh_calls == []
    assert [account_id for account_id, _token in upstream.calls] == [first, second]


async def test_disabled_entry_is_skipped_then_next_served(session, redis, settings):
    token = await _issue_token(session, settings)
    disabled = await _add_account(session, status=CredentialStatus.DISABLED)
    good = await _add_account(session)
    await create_chain(session, "default", [disabled, good])

    upstream = FakeUpstream()
    response = await complete_chat_completion(
        session,
        presented_token=token,
        request=_request(),
        redis=redis,
        settings=settings,
        upstream=upstream,
        acquire_material=_acquire_fake(),
    )
    # The disabled credential is never even attempted.
    assert upstream.calls == [good]
    assert response.choices[0].message.content


# --------------------------------------------------------------------------- #
# Token usage limit (Requirement 9.2)
# --------------------------------------------------------------------------- #
async def test_token_over_limit_rejected(session, redis, settings):
    limit = UsageLimitSpec(
        metric=LimitMetric.REQUEST_COUNT,
        limit_value=1,
        window=LimitWindow.NONE,
    )
    issued = await create_token(session, "limited", limit, settings=settings)
    account_id = await _add_account(session)
    await create_chain(session, "default", [account_id])

    # Pre-load the token's request-count counter at/over the limit.
    key = counter_key(SUBJECT_TOKEN, issued.token_id, LimitMetric.REQUEST_COUNT,
                      LimitWindow.NONE)
    redis._data[key] = "1"

    upstream = FakeUpstream()
    with pytest.raises(RateLimitError):
        await complete_chat_completion(
            session,
            presented_token=issued.secret,
            request=_request(),
            redis=redis,
            settings=settings,
            upstream=upstream,
            acquire_material=_acquire_fake(),
        )
    assert upstream.calls == []


# --------------------------------------------------------------------------- #
# Session affinity (Requirement 12.2)
# --------------------------------------------------------------------------- #
async def test_session_affinity_prefers_bound_credential(session, redis, settings):
    token = await _issue_token(session, settings)
    first = await _add_account(session)
    second = await _add_account(session)
    await create_chain(session, "default", [first, second])

    # Bind the session to the *second* credential; it should be tried first.
    await record_session_binding("sess-1", second, redis=redis, settings=settings)

    upstream = FakeUpstream()
    await complete_chat_completion(
        session,
        presented_token=token,
        request=_request(),
        session_id="sess-1",
        redis=redis,
        settings=settings,
        upstream=upstream,
        acquire_material=_acquire_fake(),
    )
    assert upstream.calls[0] == second


async def test_model_selector_chooses_matching_chain(session, redis, settings):
    token = await _issue_token(session, settings)
    default_account = await _add_account(session)
    special_account = await _add_account(session)
    await create_chain(session, "default", [default_account])
    await create_chain(
        session, "special", [special_account], model_selector="gpt-4o"
    )

    upstream = FakeUpstream()
    await complete_chat_completion(
        session,
        presented_token=token,
        request=_request(model="gpt-4o"),
        redis=redis,
        settings=settings,
        upstream=upstream,
        acquire_material=_acquire_fake(),
    )
    # The model-matched chain (special) is used over the catch-all default.
    assert upstream.calls == [special_account]


async def test_token_assigned_chain_overrides_model_selector(session, redis, settings):
    default_account = await _add_account(session)
    special_account = await _add_account(session)
    pinned_account = await _add_account(session)
    await create_chain(session, "default", [default_account])
    await create_chain(
        session, "special", [special_account], model_selector="gpt-4o"
    )
    pinned = await create_chain(session, "pinned", [pinned_account])
    token = await _issue_token(session, settings, assigned_chain_id=pinned.chain_id)

    upstream = FakeUpstream()
    await complete_chat_completion(
        session,
        presented_token=token,
        request=_request(model="gpt-4o"),
        redis=redis,
        settings=settings,
        upstream=upstream,
        acquire_material=_acquire_fake(),
    )

    assert upstream.calls == [pinned_account]


async def test_per_call_chain_override_wins_over_api_key_default(
    session, redis, settings
):
    pinned_account = await _add_account(session)
    override_account = await _add_account(session)
    pinned = await create_chain(session, "pinned", [pinned_account])
    override = await create_chain(
        session,
        "one-call",
        [ChainEntryInput(override_account, "gpt-one-call")],
    )
    token = await _issue_token(session, settings, assigned_chain_id=pinned.chain_id)

    upstream = FakeUpstream()
    await complete_chat_completion(
        session,
        presented_token=token,
        request=_request(),
        chain_override_id=override.chain_id,
        redis=redis,
        settings=settings,
        upstream=upstream,
        acquire_material=_acquire_fake(),
    )

    assert upstream.calls == [override_account]
