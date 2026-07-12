"""Credential state snapshot and the routing availability predicate.

The Flow_Controller decides routing order from a *snapshot* of each candidate
credential's state rather than from live database rows, which keeps the decision
logic pure and directly property-testable (design: "Evaluation is pure given a
snapshot of credential states").

:class:`CredentialState` captures exactly the four facts that determine whether a
credential may serve a request:

* ``deleted`` -- the credential has been soft-deleted (``deleted_at IS NOT NULL``);
  the entry must be skipped (Requirement 11.2).
* ``enabled`` -- the Operator has the credential enabled; a disabled credential is
  skipped (Requirements 5.1, 5.2, 11.1).
* ``requires_reauth`` -- a token refresh failed and the credential must be
  reconnected; it is unavailable until then (Requirements 3.3, 3.4, 11.3).
* ``limit_reached`` -- recorded usage has reached the configured Usage_Limit for the
  active measurement window; the credential is unavailable until the window resets
  (Requirements 4.2, 11.3). This boolean is computed upstream by
  :func:`gozar.usage.limits.limit_reached`, so this module stays free of limit math.

A credential is **available** if and only if it exists (not deleted), is enabled,
does not require reauthorization, and has not reached its usage limit. If any of
those conditions fails the credential is unavailable (Property 4).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CredentialState:
    """An immutable snapshot of the facts that gate a credential's availability.

    The defaults describe a freshly connected, fully usable credential (present,
    enabled, authorized, under limit). A state that is missing from a routing
    snapshot should be treated as deleted/unavailable by the caller.
    """

    deleted: bool = False
    enabled: bool = True
    requires_reauth: bool = False
    limit_reached: bool = False

    @property
    def available(self) -> bool:
        """Whether this credential may serve a request (see :func:`is_available`)."""
        return is_available(self)


def is_available(state: CredentialState) -> bool:
    """Return whether ``state`` describes an available credential (Property 4).

    Available iff the credential exists (not deleted), is enabled, does not require
    reauthorization, and has not reached its usage limit. Any single failing
    condition makes the credential unavailable (Requirements 3.4, 4.2, 5.1, 5.2,
    11.1, 11.2, 11.3).
    """
    return (
        not state.deleted
        and state.enabled
        and not state.requires_reauth
        and not state.limit_reached
    )
