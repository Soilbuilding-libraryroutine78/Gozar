"""Usage-limit value object and pure limit/percentage evaluation.

A Usage_Limit constrains either an Upstream_Credential or a Client_Token. This
module models the limit as a typed, immutable value object (:class:`UsageLimitSpec`)
and provides two **pure** evaluation functions used by both the Account_Manager and
the Token_Authority/Proxy_Gateway paths:

* :func:`consumed_percentage` -- the fraction of a configured capacity that has been
  consumed, expressed as a percentage (Requirement 4.3).
* :func:`limit_reached` -- whether recorded consumption has reached a configured
  limit (Requirements 4.1, 4.2, 4.4, 9.1, 9.2).

Both functions are side-effect free (no I/O, no clock, no global state), so they are
deterministic and directly property-testable. The caller is responsible for
supplying the consumption already aggregated over the limit's active measurement
window; window reset semantics live in the counter store, not here.

A limit's ``metric`` selects what is being counted:

* ``request_count`` -- number of proxied requests (absolute threshold).
* ``token_count`` -- provider-reported token totals (absolute threshold).
* ``cost_estimate`` -- estimated spend (absolute threshold).
* ``percentage`` -- a percentage of a configured ``capacity``; the limit is reached
  when consumed percentage meets or exceeds ``limit_value`` (Requirement 4.3).

For the three absolute metrics ``limit_value`` is compared directly against
consumption. For the ``percentage`` metric ``capacity`` is required and must be
positive, and ``limit_value`` is interpreted as the percentage threshold.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, model_validator


class LimitMetric(str, Enum):
    """What a Usage_Limit counts.

    The first three are *absolute* metrics compared directly against recorded
    consumption. ``PERCENTAGE`` is a *relative* metric evaluated against a
    configured capacity (Requirement 4.3).
    """

    REQUEST_COUNT = "request_count"
    TOKEN_COUNT = "token_count"
    COST_ESTIMATE = "cost_estimate"
    PERCENTAGE = "percentage"


class LimitWindow(str, Enum):
    """The measurement window over which consumption is accumulated.

    ``NONE`` means the limit is cumulative and never resets. The remaining windows
    reset on their natural boundary (Requirement 4.2 "until the limit's measurement
    window resets"); the actual reset is performed by the counter store, not by this
    pure module.
    """

    NONE = "none"
    DAILY = "daily"
    MONTHLY = "monthly"
    ROLLING_24H = "rolling_24h"


# Metrics whose ``limit_value`` is compared directly against consumption.
_ABSOLUTE_METRICS = frozenset(
    {LimitMetric.REQUEST_COUNT, LimitMetric.TOKEN_COUNT, LimitMetric.COST_ESTIMATE}
)


class UsageLimitSpec(BaseModel):
    """An immutable description of a single Usage_Limit.

    Attributes
    ----------
    metric:
        What the limit counts (see :class:`LimitMetric`).
    limit_value:
        The threshold. For absolute metrics this is the maximum allowed
        consumption; for ``percentage`` it is the percentage threshold (for example
        ``80`` meaning 80%). Must be non-negative.
    capacity:
        The denominator used when ``metric`` is ``percentage``; required and must be
        strictly positive in that case (Requirement 4.3). Must be ``None`` or unused
        for absolute metrics.
    window:
        The measurement window the limit applies to (see :class:`LimitWindow`).

    The model is frozen, so an instance is a hashable, comparable value object. The
    "most recently persisted spec" semantics of Requirement 9 are satisfied simply
    by passing the current spec to :func:`limit_reached`.
    """

    model_config = ConfigDict(frozen=True)

    metric: LimitMetric
    limit_value: float
    capacity: float | None = None
    window: LimitWindow = LimitWindow.NONE

    @model_validator(mode="after")
    def _validate_spec(self) -> "UsageLimitSpec":
        if self.limit_value < 0:
            raise ValueError("limit_value must be non-negative")
        if self.metric is LimitMetric.PERCENTAGE:
            if self.capacity is None or self.capacity <= 0:
                raise ValueError(
                    "percentage metric requires a positive capacity"
                )
        return self


def consumed_percentage(usage: float, capacity: float) -> float:
    """Return ``usage`` as a percentage of ``capacity`` (Requirement 4.3).

    The result is ``usage / capacity * 100``. For a fixed positive ``capacity`` the
    result is monotonically non-decreasing in ``usage`` (more usage never yields a
    smaller percentage), which backs the percentage-consumption property.

    Args:
        usage: Recorded consumption (non-negative in normal operation).
        capacity: The configured capacity; must be strictly positive.

    Returns:
        The consumed percentage as a float (may exceed 100 when usage exceeds
        capacity).

    Raises:
        ValueError: If ``capacity`` is not strictly positive.
    """
    if capacity <= 0:
        raise ValueError("capacity must be strictly positive")
    return usage / capacity * 100.0


def limit_reached(
    consumption: float,
    spec: UsageLimitSpec,
    window: LimitWindow | None = None,
) -> bool:
    """Return whether ``consumption`` has reached the limit described by ``spec``.

    Evaluation always uses the supplied ``spec`` (the most recently persisted limit
    configuration), satisfying Requirements 4.4 and 9 "no reconnect / latest config"
    semantics:

    * For absolute metrics (``request_count``, ``token_count``, ``cost_estimate``)
      the limit is reached iff ``consumption >= spec.limit_value``.
    * For the ``percentage`` metric the limit is reached iff
      ``consumed_percentage(consumption, spec.capacity) >= spec.limit_value``.

    The caller supplies ``consumption`` already aggregated over the limit's active
    measurement window; a window reset clears consumption upstream and therefore
    clears the reached state here.

    Args:
        consumption: Recorded consumption for the limit's subject within the active
            window.
        spec: The limit configuration to evaluate against.
        window: Optional contextual window the consumption was measured over. It is
            accepted for interface symmetry with window-scoped callers and does not
            alter the comparison; consumption is assumed to already be scoped to the
            intended window. Defaults to ``spec.window`` when ``None``.

    Returns:
        ``True`` if the limit is reached (subject becomes rejected/unavailable),
        ``False`` otherwise.
    """
    # ``window`` is contextual only; the spec carries the authoritative window and
    # the comparison operates on already-scoped consumption.
    _ = window if window is not None else spec.window

    if spec.metric is LimitMetric.PERCENTAGE:
        # capacity is guaranteed positive by UsageLimitSpec validation.
        assert spec.capacity is not None  # narrowed by the validator
        return consumed_percentage(consumption, spec.capacity) >= spec.limit_value

    # Absolute metrics: direct comparison against the threshold.
    return consumption >= spec.limit_value
