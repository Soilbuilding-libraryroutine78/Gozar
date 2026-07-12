"""Integration tests: the admin accounts/tokens lists report real recorded usage.

These pin the wiring fixed in this change -- the operator-facing
``GET /api/accounts`` and ``GET /api/tokens`` views read the Usage_Recorder
consumption counters (the same Redis counters the gateway enforces limits against)
instead of the previous hardcoded ``0.0`` (Requirements 5.4, 8.3, 13.3).

The routes acquire the counter store via :func:`gozar.core.redis.get_redis`; here
that seam is replaced with the in-memory Redis fake the metering tests use, seeded
through the real :func:`gozar.usage.service.record_usage` path so the figures are
produced exactly as in production. A companion test confirms the views degrade to
``0.0`` (rather than erroring) when the counter store is unavailable.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from gozar.accounts.service import connect_api_key, set_usage_limit as set_account_limit
from gozar.api import accounts as accounts_router
from gozar.api import tokens as tokens_router
from gozar.tokens.service import create_token, set_usage_limit as set_token_limit
from gozar.usage.limits import LimitMetric, LimitWindow, UsageLimitSpec
from gozar.usage.service import UsageEvent, record_usage

# Reuse the metering test's in-memory Redis stand-in.
from tests.usage.test_usage_recording import FakeRedis


async def _accept_key(entry, api_key):  # noqa: ANN001
    """Injected API-key validation that accepts the key without a network call."""
    return None


@pytest_asyncio.fixture
async def seeded_with_usage(sessionmaker, settings):
    """Seed an account + token (each with a daily token-count limit) and meter usage.

    Returns the ids and the FakeRedis carrying the recorded counters so the test can
    pin it as the route's counter store.
    """
    redis = FakeRedis()
    async with sessionmaker() as session:
        issued = await create_token(session, "usage-token", None, settings=settings)
        credential = await connect_api_key(
            session, "openai", "sk-test", settings=settings, validate=_accept_key
        )
        # Give both subjects a daily token-count limit so the view reads the
        # token counter in the DAILY window.
        spec = UsageLimitSpec(
            metric=LimitMetric.TOKEN_COUNT,
            limit_value=10_000,
            window=LimitWindow.DAILY,
        )
        await set_token_limit(session, issued.token_id, spec)
        await set_account_limit(session, credential.id, spec)
        await session.commit()

    # Meter two completed requests (18 + 22 = 40 tokens) against both subjects.
    async with sessionmaker() as session:
        for total in (18, 22):
            await record_usage(
                session,
                UsageEvent(
                    correlation_id=uuid.uuid4(),
                    client_token_id=issued.token_id,
                    account_id=credential.id,
                    provider="openai",
                    prompt_tokens=total,
                    completion_tokens=0,
                    total_tokens=total,
                ),
                redis=redis,
            )
        await session.commit()

    return {
        "redis": redis,
        "token_id": issued.token_id,
        "account_id": credential.id,
    }


def test_accounts_list_reports_recorded_consumption(
    client, auth_header, seeded_with_usage, monkeypatch
):
    """The accounts view reflects the metered token counter, not a hardcoded 0.0."""
    monkeypatch.setattr(
        accounts_router, "get_redis", lambda: seeded_with_usage["redis"]
    )

    resp = client.get("/api/accounts", headers=auth_header("admin"))

    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert len(items) == 1
    assert items[0]["account_id"] == str(seeded_with_usage["account_id"])
    assert items[0]["consumption"] == 40.0


def test_tokens_list_reports_recorded_usage(
    client, auth_header, seeded_with_usage, monkeypatch
):
    """The tokens view reflects the metered token counter, not a hardcoded 0.0."""
    monkeypatch.setattr(tokens_router, "get_redis", lambda: seeded_with_usage["redis"])

    resp = client.get("/api/tokens", headers=auth_header("admin"))

    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert len(items) == 1
    assert items[0]["token_id"] == str(seeded_with_usage["token_id"])
    assert items[0]["usage"] == 40.0


def test_accounts_list_degrades_to_zero_when_counter_store_unavailable(
    client, auth_header, seeded_with_usage, monkeypatch
):
    """When the counter store cannot be reached the view still renders, with 0.0."""

    def _boom():
        raise RuntimeError("GOZAR_REDIS_URL is not configured")

    monkeypatch.setattr(accounts_router, "get_redis", _boom)

    resp = client.get("/api/accounts", headers=auth_header("admin"))

    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert len(items) == 1
    assert items[0]["consumption"] == 0.0
