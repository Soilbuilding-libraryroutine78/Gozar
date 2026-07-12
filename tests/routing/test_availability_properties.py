"""Property-based tests for the credential availability predicate (Property 4).

These tests validate Property 4 from the Gozar design: for any upstream credential
state, the credential is available for routing if and only if it exists (not
deleted), is enabled, does not require reauthorization, and has not reached its
usage limit; if any of those conditions fails the credential is unavailable.

The predicate under test is the pure :func:`gozar.routing.state.is_available`
(mirrored by :attr:`CredentialState.available`). It takes a snapshot of the four
gating facts, so the whole input space is the cartesian product of four booleans
plus the spec's own reference definition of availability.
"""

from __future__ import annotations

from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from gozar.routing import CredentialState, is_available

# Every credential snapshot is fully described by four independent booleans, so a
# uniform booleans strategy explores the entire input space.
_states = st.builds(
    CredentialState,
    deleted=st.booleans(),
    enabled=st.booleans(),
    requires_reauth=st.booleans(),
    limit_reached=st.booleans(),
)


# Feature: gozar, Property 4: Credential availability predicate
@hyp_settings(max_examples=200)
@given(state=_states)
def test_available_iff_all_conditions_hold(state: CredentialState) -> None:
    """Validates: Requirements 3.4, 4.2, 5.1, 5.2, 11.1, 11.2, 11.3.

    The credential is available if and only if it exists (not deleted), is enabled,
    does not require reauthorization, and has not reached its usage limit. The
    right-hand side is an independent restatement of the spec definition, so the
    biconditional pins the predicate to the requirement for every snapshot.
    """
    expected = (
        not state.deleted
        and state.enabled
        and not state.requires_reauth
        and not state.limit_reached
    )
    assert is_available(state) is expected
    # The convenience property must agree with the free function.
    assert state.available is expected


# Feature: gozar, Property 4: Credential availability predicate
@hyp_settings(max_examples=200)
@given(state=_states)
def test_any_single_failing_condition_makes_unavailable(
    state: CredentialState,
) -> None:
    """Validates: Requirements 3.4, 4.2, 5.1, 5.2, 11.1, 11.2, 11.3.

    If any single gating condition fails the credential is unavailable, regardless
    of the other facts. Each requirement maps to one fact: deletion (11.2),
    disabled (5.1, 5.2, 11.1), reauthorization required (3.4, 11.3), and limit
    reached (4.2, 11.3).
    """
    if state.deleted or not state.enabled or state.requires_reauth or state.limit_reached:
        assert is_available(state) is False


# Feature: gozar, Property 4: Credential availability predicate
@hyp_settings(max_examples=200)
@given(state=_states)
def test_availability_implies_every_condition_holds(state: CredentialState) -> None:
    """Validates: Requirements 3.4, 4.2, 5.1, 5.2, 11.1, 11.2, 11.3.

    The converse direction: whenever the credential is reported available, every
    one of the four gating conditions must hold simultaneously.
    """
    if is_available(state):
        assert not state.deleted
        assert state.enabled
        assert not state.requires_reauth
        assert not state.limit_reached
