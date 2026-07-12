"""Property-based tests for the subscription refresh-window predicate (Property 2).

These tests validate Property 2 from the Gozar design: for any token expiry
timestamp, current time, and renewal window, the refresh-needed predicate returns
``True`` exactly when the time remaining until expiry is less than or equal to the
renewal window -- including already-expired tokens, whose remaining time is negative
and therefore always within any non-negative window.

The pure predicate under test is :func:`gozar.accounts.service.refresh_needed`, which
takes ``(expires_at, now, renewal_window)`` and reads no clock and mutates no state.

To avoid a tautological "compare the implementation against itself" test, the oracle
here computes the time remaining independently via
``(expires_at - now).total_seconds()`` and compares it (in seconds) against the
renewal window in seconds, rather than reusing the predicate's own ``timedelta``
comparison.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from gozar.accounts.service import refresh_needed

# Bound the clock to a wide but overflow-safe range so that ``now + offset`` stays a
# representable datetime for every generated offset below.
_clocks = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2100, 1, 1),
)

# Offsets span well before to well after "now": negative offsets model
# already-expired tokens, zero models expiry exactly at ``now``, and positive offsets
# model tokens with time remaining. This deliberately straddles the renewal-window
# boundary in both directions so the iff is exercised on both sides.
_offsets = st.timedeltas(
    min_value=timedelta(days=-60),
    max_value=timedelta(days=60),
)

# Non-negative renewal windows mirror real configuration
# (``subscription_renewal_window_seconds`` is a non-negative integer of seconds).
_windows = st.timedeltas(
    min_value=timedelta(0),
    max_value=timedelta(days=30),
)


# Feature: gozar, Property 2: Refresh window predicate
@hyp_settings(max_examples=200)
@given(now=_clocks, offset=_offsets, renewal_window=_windows)
def test_refresh_needed_iff_remaining_within_window(
    now: datetime,
    offset: timedelta,
    renewal_window: timedelta,
) -> None:
    """Validates: Requirements 3.1.

    ``refresh_needed`` is ``True`` if and only if the time remaining until expiry is
    at or below the renewal window. The expiry is constructed as ``now + offset`` so
    the time remaining equals ``offset``; the oracle compares that remaining time, in
    seconds, against the window in seconds independently of the predicate's internal
    ``timedelta`` arithmetic.
    """
    expires_at = now + offset

    remaining_seconds = (expires_at - now).total_seconds()
    window_seconds = renewal_window.total_seconds()
    expected = remaining_seconds <= window_seconds

    assert refresh_needed(expires_at, now, renewal_window) is expected


# Feature: gozar, Property 2: Refresh window predicate
@hyp_settings(max_examples=200)
@given(now=_clocks, renewal_window=_windows)
def test_already_expired_token_always_needs_refresh(
    now: datetime,
    renewal_window: timedelta,
) -> None:
    """Validates: Requirements 3.1.

    An already-expired token (expiry at or before ``now``) has zero or negative time
    remaining, which is at or below any non-negative renewal window, so refresh is
    always needed.
    """
    expired_at = now - timedelta(seconds=1)

    assert refresh_needed(expired_at, now, renewal_window) is True
    # Expiry exactly at ``now`` (zero remaining) is also within the window.
    assert refresh_needed(now, now, renewal_window) is True


# Feature: gozar, Property 2: Refresh window predicate
@hyp_settings(max_examples=200)
@given(now=_clocks, renewal_window=_windows)
def test_boundary_is_inclusive_and_just_outside_is_excluded(
    now: datetime,
    renewal_window: timedelta,
) -> None:
    """Validates: Requirements 3.1.

    The predicate is inclusive at the boundary: when the time remaining equals the
    renewal window exactly, refresh is needed. A token whose remaining time is just
    beyond the window does not yet need refresh.
    """
    # Remaining time exactly equal to the window -> within window -> refresh needed.
    at_boundary = now + renewal_window
    assert refresh_needed(at_boundary, now, renewal_window) is True

    # Remaining time one microsecond beyond the window -> not yet due.
    just_outside = now + renewal_window + timedelta(microseconds=1)
    assert refresh_needed(just_outside, now, renewal_window) is False
