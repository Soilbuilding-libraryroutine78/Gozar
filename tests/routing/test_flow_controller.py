"""Unit tests for the Flow_Controller pure logic and session-affinity map.

Covers the availability predicate (Property 4 territory), pure chain evaluation with
order preservation and skipping (Property 5), session affinity (Property 7 /
Requirement 12.2), and the Redis-backed session map round-trip. The dedicated
property-based tests live in tasks 9.3-9.5; these are example/edge-case unit tests.
"""

from __future__ import annotations

import uuid

import pytest

from gozar.routing import (
    CredentialState,
    RouteKind,
    RoutingChain,
    RoutingTarget,
    evaluate_chain,
    get_attempt_order,
    get_session_binding,
    is_available,
    record_session_binding,
)


class FakeRedis:
    """Minimal in-memory async stand-in for the subset of redis.asyncio used here.

    Implements ``set`` (with ``ex`` TTL capture) and ``get`` so the session-map
    serialization/round-trip logic is exercised for real, without a live server.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value
        self.ttls[key] = ex

    async def get(self, key: str) -> str | None:
        return self.store.get(key)


# --- availability predicate --------------------------------------------------


def test_default_state_is_available() -> None:
    assert is_available(CredentialState()) is True
    assert CredentialState().available is True


@pytest.mark.parametrize(
    "state",
    [
        CredentialState(deleted=True),
        CredentialState(enabled=False),
        CredentialState(requires_reauth=True),
        CredentialState(limit_reached=True),
    ],
)
def test_any_single_failing_condition_is_unavailable(state: CredentialState) -> None:
    assert is_available(state) is False


def test_all_conditions_must_hold_for_availability() -> None:
    assert is_available(
        CredentialState(
            deleted=False, enabled=True, requires_reauth=False, limit_reached=False
        )
    )


# --- pure chain evaluation ---------------------------------------------------


def test_evaluate_chain_preserves_order_and_skips_unavailable() -> None:
    a, b, c, d = (uuid.uuid4() for _ in range(4))
    chain = RoutingChain.from_entries([a, b, c, d])
    states = {
        a: CredentialState(),  # available
        b: CredentialState(enabled=False),  # skipped
        c: CredentialState(limit_reached=True),  # skipped
        d: CredentialState(),  # available
    }
    assert [target.account_id for target in evaluate_chain(chain, states)] == [a, d]


def test_missing_state_is_treated_as_deleted() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    chain = RoutingChain.from_entries([a, b])
    # No state for ``b`` -> treated as deleted/skipped.
    assert [
        target.account_id
        for target in evaluate_chain(chain, {a: CredentialState()})
    ] == [a]


def test_evaluate_chain_preserves_provider_model_per_target() -> None:
    primary, fallback = uuid.uuid4(), uuid.uuid4()
    chain = RoutingChain.from_entries(
        [
            RoutingTarget(primary, "gpt-5.4-mini"),
            RoutingTarget(fallback, "google/gemini-2.5-flash"),
        ]
    )

    assert evaluate_chain(
        chain,
        {primary: CredentialState(), fallback: CredentialState()},
    ) == list(chain.entries)


def test_all_unavailable_yields_empty_order() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    chain = RoutingChain.from_entries([a, b])
    states = {
        a: CredentialState(requires_reauth=True),
        b: CredentialState(deleted=True),
    }
    assert evaluate_chain(chain, states) == []


def test_empty_chain_yields_empty_order() -> None:
    assert evaluate_chain(RoutingChain.from_entries([]), {}) == []


# --- session affinity --------------------------------------------------------


def test_session_pref_promoted_to_front_when_available() -> None:
    a, b, c = (uuid.uuid4() for _ in range(3))
    chain = RoutingChain.from_entries([a, b, c])
    states = {a: CredentialState(), b: CredentialState(), c: CredentialState()}
    # Prefer c: it moves to the front, the rest keep their relative order.
    assert [
        target.account_id
        for target in evaluate_chain(chain, states, session_pref=c)
    ] == [c, a, b]


def test_session_pref_ignored_when_unavailable() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    chain = RoutingChain.from_entries([a, b])
    states = {a: CredentialState(), b: CredentialState(limit_reached=True)}
    # b is bound but unavailable -> normal availability order.
    assert [
        target.account_id
        for target in evaluate_chain(chain, states, session_pref=b)
    ] == [a]


def test_session_pref_not_in_chain_is_ignored() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    outsider = uuid.uuid4()
    chain = RoutingChain.from_entries([a, b])
    states = {a: CredentialState(), b: CredentialState()}
    assert [
        target.account_id
        for target in evaluate_chain(chain, states, session_pref=outsider)
    ] == [a, b]


# --- Redis session map -------------------------------------------------------


async def test_record_and_read_session_binding_round_trip() -> None:
    redis = FakeRedis()
    session_id = "sess-123"
    account_id = uuid.uuid4()

    await record_session_binding(session_id, account_id, redis=redis)
    # Stored under the namespaced key with a positive TTL.
    assert redis.store["route:session:sess-123"] == str(account_id)
    assert redis.ttls["route:session:sess-123"] and redis.ttls[
        "route:session:sess-123"
    ] > 0

    assert await get_session_binding(session_id, redis=redis) == account_id


async def test_record_session_binding_noops_on_empty_session() -> None:
    redis = FakeRedis()
    await record_session_binding("", uuid.uuid4(), redis=redis)
    assert redis.store == {}


async def test_get_session_binding_none_when_absent_or_malformed() -> None:
    redis = FakeRedis()
    assert await get_session_binding(None, redis=redis) is None
    assert await get_session_binding("missing", redis=redis) is None

    await redis.set("route:session:bad", "not-a-uuid")
    assert await get_session_binding("bad", redis=redis) is None


async def test_session_affinity_is_isolated_between_request_lanes() -> None:
    redis = FakeRedis()
    chat_account = uuid.uuid4()
    embedding_account = uuid.uuid4()

    await record_session_binding("shared", chat_account, redis=redis)
    await record_session_binding(
        "shared",
        embedding_account,
        redis=redis,
        route_kind=RouteKind.EMBEDDINGS,
    )

    assert await get_session_binding("shared", redis=redis) == chat_account
    assert (
        await get_session_binding(
            "shared",
            redis=redis,
            route_kind=RouteKind.EMBEDDINGS,
        )
        == embedding_account
    )


async def test_get_attempt_order_applies_session_affinity_from_redis() -> None:
    redis = FakeRedis()
    a, b = uuid.uuid4(), uuid.uuid4()
    chain = RoutingChain.from_entries([a, b])
    states = {a: CredentialState(), b: CredentialState()}

    # Without a binding: plain availability order.
    assert [
        target.account_id
        for target in await get_attempt_order(chain, states, None, redis=redis)
    ] == [a, b]

    # Bind the session to b; it should now be tried first.
    await record_session_binding("s1", b, redis=redis)
    assert [
        target.account_id
        for target in await get_attempt_order(chain, states, "s1", redis=redis)
    ] == [b, a]
