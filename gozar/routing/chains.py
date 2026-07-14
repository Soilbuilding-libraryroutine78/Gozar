"""Pure fallback-chain evaluation.

This module turns a Fallback_Chain plus a snapshot of credential states into the
ordered list of credential ids the Proxy_Gateway should attempt. It is intentionally
free of I/O: persistence (the ``route_fallback_chain`` tables) and state collection
live elsewhere, so this logic is deterministic and directly property-testable
(Properties 5 and 7).

A :class:`RoutingChain` is the pure, in-memory view of a persisted chain: the ordered
sequence of credential ids defined by the chain's entries (read in ascending
``position``). The persistence layer maps ``route_fallback_chain_entry`` rows into
this value object; this module never touches the database.

:func:`evaluate_chain` applies two rules:

* **Skip unavailable entries, preserve order** (Property 5): the result is exactly
  the subset of chain entries that are available, in the same relative order as
  defined by the chain. Deleted, disabled, limit-reached, and reauth-required entries
  are omitted (Requirements 10.1, 10.2, 10.4, 11.1, 11.2, 11.3). An entry with no
  state in the snapshot is treated as deleted and skipped.
* **Session affinity** (Property 7): when a session is bound to a credential and that
  credential is available within the chain, it is moved to the front of the attempt
  order; the remaining available credentials keep their relative order
  (Requirement 12.2).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from gozar.routing.state import CredentialState, is_available

# A credential id that has no entry in the state snapshot is treated as deleted, so
# it is skipped exactly like an explicitly deleted credential.
_MISSING_STATE = CredentialState(deleted=True)


class FallbackPolicy(str, Enum):
    """Versioned policy controlling the edge after a failed provider attempt."""

    ANY_ERROR = "any_error"
    AUTH_OR_RETRYABLE = "auth_or_retryable"
    RETRYABLE = "retryable"


class RouteKind(str, Enum):
    """Request lane served by one ordered group of chain nodes."""

    CHAT = "chat"
    EMBEDDINGS = "embeddings"


@dataclass(frozen=True)
class RoutingTarget:
    """One provider attempt in a routing chain.

    ``model_id`` is the model sent to this target. ``None`` intentionally preserves
    the inbound request model, which is also the backwards-compatible behavior for
    chains created before per-node model routing existed.
    """

    account_id: UUID
    model_id: str | None = None
    fallback_policy: FallbackPolicy = FallbackPolicy.ANY_ERROR
    route_kind: RouteKind = RouteKind.CHAT
    node_id: UUID | None = None
    position: int | None = None


@dataclass(frozen=True)
class RoutingChain:
    """A pure, in-memory view of a Fallback_Chain.

    ``entries`` is the ordered tuple of credential ids defined by the chain's entries
    (ascending ``position``). ``chain_id`` and ``model_selector`` are carried for
    traceability and model matching but do not affect evaluation.
    """

    entries: tuple[RoutingTarget, ...]
    chain_id: UUID | None = None
    model_selector: str | None = None

    @classmethod
    def from_entries(
        cls,
        entries: Sequence[UUID | RoutingTarget],
        *,
        chain_id: UUID | None = None,
        model_selector: str | None = None,
    ) -> "RoutingChain":
        """Build a chain from an ordered sequence of credential ids."""
        return cls(
            entries=tuple(
                entry if isinstance(entry, RoutingTarget) else RoutingTarget(entry)
                for entry in entries
            ),
            chain_id=chain_id,
            model_selector=model_selector,
        )

    @property
    def account_ids(self) -> tuple[UUID, ...]:
        """Return credential ids in attempt order for state snapshotting."""

        return tuple(entry.account_id for entry in self.entries)

    def for_route(self, route_kind: RouteKind) -> "RoutingChain":
        """Return this chain with only the nodes assigned to ``route_kind``."""

        return RoutingChain(
            entries=tuple(
                entry for entry in self.entries if entry.route_kind is route_kind
            ),
            chain_id=self.chain_id,
            model_selector=self.model_selector,
        )


def evaluate_chain(
    chain: RoutingChain,
    states: Mapping[UUID, CredentialState],
    session_pref: UUID | None = None,
) -> list[RoutingTarget]:
    """Return the ordered list of available credential ids to attempt.

    The base order is the chain's entry order with every unavailable entry removed
    (Property 5). When ``session_pref`` is supplied and that credential is present in
    the available subset, it is promoted to the front while the remaining
    credentials keep their relative order (Property 7 / Requirement 12.2).

    Args:
        chain: The fallback chain to evaluate.
        states: Snapshot of credential states keyed by credential id. A missing entry
            is treated as deleted (skipped).
        session_pref: The credential the current session was previously bound to, if
            any. Honoured only when it is available within the chain.

    Returns:
        The ordered, available credential ids to attempt. Empty when no entry is
        available (the gateway then returns a terminal "no available account" error).
    """
    available = [
        entry
        for entry in chain.entries
        if is_available(states.get(entry.account_id, _MISSING_STATE))
    ]

    if session_pref is not None and any(
        entry.account_id == session_pref for entry in available
    ):
        # Promote the session-bound credential; keep the rest in their existing
        # relative order (Property 7).
        return [
            *[entry for entry in available if entry.account_id == session_pref],
            *[entry for entry in available if entry.account_id != session_pref],
        ]

    return available
