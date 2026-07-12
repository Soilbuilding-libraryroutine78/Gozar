"""Property-based test for fallback ordering and skipping (Property 5).

Validates Property 5 from the Gozar design: for any fallback chain and any mapping
of its entries to credential states, evaluating the chain yields exactly the subset
of entries that are available, in the same relative order as defined in the chain,
with every unavailable (deleted, disabled, limit-reached, or reauth-required) entry
omitted.

These tests exercise the pure ``evaluate_chain`` against ``RoutingChain`` and the
``CredentialState`` availability predicate. Session affinity is a separate concern
(Property 7 / task 9.5), so every case here uses ``session_pref=None``.
"""

from __future__ import annotations

import uuid

from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from gozar.routing import CredentialState, RoutingChain, evaluate_chain, is_available


def _credential_states() -> st.SearchStrategy[CredentialState]:
    """Generate credential states spanning the full availability space.

    Every combination of the four gating booleans is reachable, so the generated
    chains include available entries and each kind of unavailable entry (deleted,
    disabled, reauth-required, limit-reached) as well as multiply-unavailable ones.
    """
    return st.builds(
        CredentialState,
        deleted=st.booleans(),
        enabled=st.booleans(),
        requires_reauth=st.booleans(),
        limit_reached=st.booleans(),
    )


@st.composite
def _chain_and_states(
    draw: st.DrawFn,
) -> tuple[RoutingChain, dict[uuid.UUID, CredentialState]]:
    """Build a chain of distinct credential ids paired with a state snapshot.

    Ids are distinct so positional ordering is unambiguous. A fraction of entries
    are intentionally left out of the state map to exercise the "missing state is
    treated as deleted" rule alongside explicit states.
    """
    entries = draw(
        st.lists(st.uuids(version=4), min_size=0, max_size=12, unique=True)
    )
    states: dict[uuid.UUID, CredentialState] = {}
    for account_id in entries:
        # Occasionally omit an entry's state entirely (-> treated as deleted).
        if draw(st.booleans()):
            states[account_id] = draw(_credential_states())
    chain = RoutingChain.from_entries(entries)
    return chain, states


# Feature: gozar, Property 5: For any fallback chain and any mapping of its entries
# to credential states, evaluating the chain yields exactly the subset of entries
# that are available, in the same relative order as defined in the chain, with every
# unavailable (deleted, disabled, limit-reached, or reauth-required) entry omitted.
@hyp_settings(max_examples=300)
@given(chain_and_states=_chain_and_states())
def test_evaluate_chain_is_ordered_available_subset(
    chain_and_states: tuple[RoutingChain, dict[uuid.UUID, CredentialState]],
) -> None:
    """Validates: Requirements 10.1, 10.2, 10.4, 11.1, 11.2, 11.3.

    The result equals the chain's entries filtered to the available ones, preserving
    relative order. A missing state is treated as deleted (unavailable).
    """
    chain, states = chain_and_states

    result = evaluate_chain(chain, states, session_pref=None)

    # Reference: keep each entry iff its state (defaulting to deleted when absent)
    # is available, in the chain's defined order.
    expected = [
        target
        for target in chain.entries
        if is_available(states.get(target.account_id, CredentialState(deleted=True)))
    ]

    assert result == expected


# Feature: gozar, Property 5: For any fallback chain and any mapping of its entries
# to credential states, evaluating the chain yields exactly the subset of entries
# that are available, in the same relative order as defined in the chain, with every
# unavailable (deleted, disabled, limit-reached, or reauth-required) entry omitted.
@hyp_settings(max_examples=300)
@given(chain_and_states=_chain_and_states())
def test_result_is_a_subsequence_with_only_available_entries(
    chain_and_states: tuple[RoutingChain, dict[uuid.UUID, CredentialState]],
) -> None:
    """Validates: Requirements 10.1, 10.2, 10.4, 11.1, 11.2, 11.3.

    Independently of the reference filter, assert the structural guarantees: the
    result is a subsequence of the chain (membership + order preserved, no
    additions/duplicates), every kept entry is available, and every omitted entry is
    unavailable.
    """
    chain, states = chain_and_states
    result = evaluate_chain(chain, states, session_pref=None)

    # 1. Order preservation / subsequence: result indices are strictly increasing
    #    within the chain, which also rules out reordering and duplicates.
    positions = [chain.entries.index(target) for target in result]
    assert positions == sorted(positions)
    assert len(set(positions)) == len(positions)

    # 2. Every kept entry is available; every omitted entry is unavailable. Together
    #    these prove the result is *exactly* the available subset.
    result_set = set(result)
    for target in chain.entries:
        state = states.get(target.account_id, CredentialState(deleted=True))
        assert (target in result_set) == is_available(state)
