"""Property-based tests for usage-limit evaluation (Property 9).

These tests validate Property 9 from the Gozar design: for any subject and its
recorded consumption within the active measurement window, ``limit_reached`` is
``True`` if and only if consumption has met or exceeded the configured
``limit_value`` for that window. Reaching the limit is what makes a token rejected
or a credential unavailable; a window reset (consumption returning to zero) clears
that state. Evaluation always uses the spec passed to it (the most recently
persisted limit configuration).

The percentage metric is covered separately by Property 8 (task 4.2); this module
focuses on the general evaluation contract and the absolute metrics
(``request_count``, ``token_count``, ``cost_estimate``), plus the "latest config"
and window-reset semantics.
"""

from __future__ import annotations

from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from gozar.usage.limits import (
    LimitMetric,
    LimitWindow,
    UsageLimitSpec,
    limit_reached,
)

# The three absolute metrics whose ``limit_value`` is compared directly against
# recorded consumption (the percentage metric is validated by Property 8).
_ABSOLUTE_METRICS = [
    LimitMetric.REQUEST_COUNT,
    LimitMetric.TOKEN_COUNT,
    LimitMetric.COST_ESTIMATE,
]

# Non-negative, finite consumption and limit values mirroring real counters
# (request counts, token totals, cost estimates).
_non_negative = st.floats(
    min_value=0.0, max_value=1e12, allow_nan=False, allow_infinity=False
)

_metrics = st.sampled_from(_ABSOLUTE_METRICS)
_windows = st.sampled_from(list(LimitWindow))


# Feature: gozar, Property 9: Usage limit evaluation
@hyp_settings(max_examples=200)
@given(
    consumption=_non_negative,
    limit_value=_non_negative,
    metric=_metrics,
    window=_windows,
)
def test_absolute_limit_reached_iff_consumption_at_or_above_value(
    consumption: float,
    limit_value: float,
    metric: LimitMetric,
    window: LimitWindow,
) -> None:
    """Validates: Requirements 4.1, 4.2, 4.4, 9.1, 9.2.

    For absolute metrics the limit is reached if and only if consumption is greater
    than or equal to the configured ``limit_value`` for that window.
    """
    spec = UsageLimitSpec(metric=metric, limit_value=limit_value, window=window)

    assert limit_reached(consumption, spec) == (consumption >= limit_value)
    # The optional contextual ``window`` argument must not change the outcome.
    assert limit_reached(consumption, spec, window=window) == (
        consumption >= limit_value
    )


# Feature: gozar, Property 9: Usage limit evaluation
@hyp_settings(max_examples=200)
@given(
    consumption=_non_negative,
    limit_a=_non_negative,
    limit_b=_non_negative,
    metric=_metrics,
    window=_windows,
)
def test_latest_config_semantics_uses_only_the_passed_spec(
    consumption: float,
    limit_a: float,
    limit_b: float,
    metric: LimitMetric,
    window: LimitWindow,
) -> None:
    """Validates: Requirements 4.4, 9.1, 9.2.

    Evaluation reads only the spec it is given. Re-evaluating the same consumption
    against an updated spec (a different ``limit_value``) yields the outcome implied
    by the new value, demonstrating the function uses the most recently persisted
    configuration rather than any retained state.
    """
    spec_a = UsageLimitSpec(metric=metric, limit_value=limit_a, window=window)
    spec_b = UsageLimitSpec(metric=metric, limit_value=limit_b, window=window)

    assert limit_reached(consumption, spec_a) == (consumption >= limit_a)
    assert limit_reached(consumption, spec_b) == (consumption >= limit_b)


# Feature: gozar, Property 9: Usage limit evaluation
@hyp_settings(max_examples=200)
@given(
    limit_value=st.floats(
        min_value=1e-9, max_value=1e12, allow_nan=False, allow_infinity=False
    ),
    metric=_metrics,
    window=_windows,
)
def test_window_reset_clears_reached_state(
    limit_value: float,
    metric: LimitMetric,
    window: LimitWindow,
) -> None:
    """Validates: Requirements 4.2, 9.2.

    A window reset returns consumption to zero. For any positive limit, zero
    consumption is below the threshold, so the reached state is cleared and the
    subject is no longer rejected/unavailable.
    """
    spec = UsageLimitSpec(metric=metric, limit_value=limit_value, window=window)

    # After a reset consumption is 0, which is strictly below any positive limit.
    assert limit_reached(0.0, spec) is False
