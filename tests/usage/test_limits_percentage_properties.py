"""Property-based tests for percentage consumption computation.

These tests validate Property 8 from the Gozar design: for any recorded usage and
any positive configured capacity, the consumed percentage equals usage / capacity *
100, is monotonically non-decreasing in usage, and a PERCENTAGE usage limit's
threshold is reached if and only if the consumed percentage meets or exceeds the
configured threshold.
"""

from __future__ import annotations

import math

from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from gozar.usage.limits import (
    LimitMetric,
    UsageLimitSpec,
    consumed_percentage,
    limit_reached,
)

# Non-negative usage values: finite, no NaN/inf, bounded to a realistic range so the
# float arithmetic stays well-conditioned for tolerance-based comparisons.
_usage = st.floats(
    min_value=0.0,
    max_value=1_000_000_000.0,
    allow_nan=False,
    allow_infinity=False,
)

# Strictly positive, finite capacity. A small lower bound keeps usage / capacity from
# blowing up beyond float tolerance while still exercising a wide dynamic range.
_capacity = st.floats(
    min_value=1e-3,
    max_value=1_000_000_000.0,
    allow_nan=False,
    allow_infinity=False,
)

# Percentage threshold for the PERCENTAGE limit metric; non-negative and finite.
_threshold = st.floats(
    min_value=0.0,
    max_value=1000.0,
    allow_nan=False,
    allow_infinity=False,
)


# Feature: gozar, Property 8: Percentage consumption computation
@hyp_settings(max_examples=200)
@given(usage=_usage, capacity=_capacity)
def test_consumed_percentage_equals_usage_over_capacity(
    usage: float, capacity: float
) -> None:
    """Validates: Requirements 4.3.

    The consumed percentage equals ``usage / capacity * 100`` for any non-negative
    usage and any strictly positive capacity.
    """
    result = consumed_percentage(usage, capacity)
    expected = usage / capacity * 100.0
    assert math.isclose(result, expected, rel_tol=1e-9, abs_tol=1e-12)


# Feature: gozar, Property 8: Percentage consumption computation
@hyp_settings(max_examples=200)
@given(usage1=_usage, usage2=_usage, capacity=_capacity)
def test_consumed_percentage_monotonic_non_decreasing(
    usage1: float, usage2: float, capacity: float
) -> None:
    """Validates: Requirements 4.3.

    For a fixed positive capacity, the consumed percentage is monotonically
    non-decreasing in usage: usage1 <= usage2 implies that the percentage for usage1
    does not exceed the percentage for usage2 (within float tolerance).
    """
    lower, higher = sorted((usage1, usage2))
    pct_lower = consumed_percentage(lower, capacity)
    pct_higher = consumed_percentage(higher, capacity)
    # Allow a small tolerance so float rounding never produces a spurious failure.
    assert pct_lower <= pct_higher + 1e-9


# Feature: gozar, Property 8: Percentage consumption computation
@hyp_settings(max_examples=200)
@given(consumption=_usage, capacity=_capacity, threshold=_threshold)
def test_percentage_threshold_reached_iff_at_or_above_threshold(
    consumption: float, capacity: float, threshold: float
) -> None:
    """Validates: Requirements 4.3.

    For a PERCENTAGE usage limit configured with a positive capacity and a percentage
    threshold, ``limit_reached`` is True if and only if the consumed percentage is
    greater than or equal to the configured threshold.
    """
    spec = UsageLimitSpec(
        metric=LimitMetric.PERCENTAGE,
        limit_value=threshold,
        capacity=capacity,
    )
    consumed = consumed_percentage(consumption, capacity)
    assert limit_reached(consumption, spec) == (consumed >= threshold)
