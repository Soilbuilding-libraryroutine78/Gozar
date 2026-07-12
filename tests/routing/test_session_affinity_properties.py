"""Property-based test for session affinity preference (Property 7).

Validates Property 7 from the Gozar design (Requirement 12.2): for any set of
credential states and any session-bound credential, the attempt order produced by
``evaluate_chain`` places the bound credential first when it is available, and
otherwise equals the normal availability-ordered result with the relative order of
the remaining credentials unchanged.

The companion properties live in ``test_availability_properties.py`` (9.3) and
``test_fallback_ordering_properties.py`` (9.4); example/edge-case unit tests live in
``test_flow_controller.py``. This module exercises only the session-affinity rule
across many generated state snapshots and session preferences.
"""

from __future__ import annotations

import uuid
from uuid import UUID

from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from gozar.routing import CredentialState, RoutingChain, evaluate_chain

# Smart generators constrained to the real input space of the evaluator: a chain is
# an ordered list of distinct credential ids, each with an independent availability
# snapshot. We draw a pool of distinct ids up front so the chain, the state map, and
# the session preference can all reference the same id space (and so we can also pick
# an "outsider" id that is not in the chain).


def _state() -> st.SearchStrategy[CredentialState]:
    """A credential snapshot with each gating flag chosen independently."""
    return st.builds(
        CredentialState,
        deleted=st.booleans(),
        enabled=st.booleans(),
        requires_reauth=st.booleans(),
        limit_reached=st.booleans(),
    )


@st.composite
def _scenarios(draw: st.DrawFn) -> tuple[RoutingChain, dict[UUID, CredentialState], UUID | None]:
    """Generate a (chain, states, session_pref) triple over a shared id pool.

    ``session_pref`` is deliberately drawn to cover every meaningful case: ``None``,
    a chain entry (available or not), and an outsider id absent from the chain.
    """
    # Distinct ids: some used as chain entries, a few held back as "outsiders".
    pool = draw(
        st.lists(st.uuids(), min_size=0, max_size=8, unique=True)
    )
    n_entries = draw(st.integers(min_value=0, max_value=len(pool)))
    entries = pool[:n_entries]
    outsiders = pool[n_entries:]

    states: dict[UUID, CredentialState] = {}
    for account_id in entries:
        # Occasionally omit a state entirely so the "missing == deleted" path is hit.
        if draw(st.booleans()):
            states[account_id] = draw(_state())

    pref_choices: list[UUID | None] = [None, *entries, *outsiders]
    session_pref = draw(st.sampled_from(pref_choices))

    chain = RoutingChain.from_entries(entries, chain_id=uuid.uuid4())
    return chain, states, session_pref


# Feature: gozar, Property 7: For any set of credential states and any session-bound
# credential, the attempt order places the bound credential first when it is
# available, and otherwise equals the normal availability-ordered result with the
# relative order of the remaining credentials unchanged.
@hyp_settings(max_examples=300)
@given(scenario=_scenarios())
def test_session_affinity_preference(
    scenario: tuple[RoutingChain, dict[UUID, CredentialState], UUID | None],
) -> None:
    """Validates: Requirements 12.2.

    The session-affinity rule is defined purely in terms of the no-preference
    availability order (``baseline``):

    * When the bound credential is available (present in ``baseline``), it is moved
      to the front and the remaining entries keep their relative order, i.e. the
      result equals ``[pref] + (baseline with pref removed)``.
    * Otherwise (no preference, an unavailable preference, or an id absent from the
      chain), the result is exactly ``baseline`` -- affinity changes nothing.
    """
    chain, states, session_pref = scenario

    baseline = evaluate_chain(chain, states, session_pref=None)
    result = evaluate_chain(chain, states, session_pref=session_pref)

    baseline_ids = [target.account_id for target in baseline]
    result_ids = [target.account_id for target in result]

    if session_pref is not None and session_pref in baseline_ids:
        # Bound credential is available: promoted to the front, rest order preserved.
        assert result_ids[0] == session_pref
        assert result_ids[1:] == [aid for aid in baseline_ids if aid != session_pref]
        # The promoted result is a permutation of the baseline (same set, no losses).
        assert sorted(result_ids, key=str) == sorted(baseline_ids, key=str)
    else:
        # No usable preference: identical to the normal availability order.
        assert result == baseline
