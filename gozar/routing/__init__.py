"""Flow_Controller: fallback chains, availability, session affinity.

Public surface:

* :class:`CredentialState` / :func:`is_available` -- the routing availability
  predicate (Property 4).
* :class:`RoutingChain` / :func:`evaluate_chain` -- pure fallback-chain evaluation
  with order preservation and session affinity (Properties 5 and 7).
* :func:`get_attempt_order`, :func:`record_session_binding`,
  :func:`get_session_binding` -- the Redis-backed session-affinity map and
  attempt-order assembly (Requirement 12).
* :class:`RouteFallbackChain` / :class:`RouteFallbackChainEntry` -- the persisted
  Fallback_Chain ORM models, and the :mod:`gozar.routing.service` CRUD operations
  (:func:`create_chain`, :func:`edit_chain`, :func:`list_chains`, ...) that map
  persisted rows to :class:`RoutingChain` for evaluation (Requirements 10.1, 10.4,
  11.4).
"""

from gozar.routing.chains import (
    FallbackPolicy,
    RouteKind,
    RoutingChain,
    RoutingTarget,
    evaluate_chain,
)
from gozar.routing.models import RouteFallbackChain, RouteFallbackChainEntry
from gozar.routing.service import (
    ChainEntryView,
    ChainEntryInput,
    ChainView,
    create_chain,
    delete_chain,
    edit_chain,
    get_chain,
    list_chains,
    load_routing_chain,
    upsert_chain_by_key,
)
from gozar.routing.session import (
    get_attempt_order,
    get_session_binding,
    record_session_binding,
)
from gozar.routing.state import CredentialState, is_available

__all__ = [
    "CredentialState",
    "is_available",
    "RoutingChain",
    "RoutingTarget",
    "FallbackPolicy",
    "RouteKind",
    "evaluate_chain",
    "get_attempt_order",
    "get_session_binding",
    "record_session_binding",
    "RouteFallbackChain",
    "RouteFallbackChainEntry",
    "ChainEntryView",
    "ChainEntryInput",
    "ChainView",
    "create_chain",
    "edit_chain",
    "get_chain",
    "list_chains",
    "delete_chain",
    "load_routing_chain",
    "upsert_chain_by_key",
]
