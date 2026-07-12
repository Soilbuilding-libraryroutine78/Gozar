"""Role-based access control primitives (fail-closed).

This module defines the :class:`Permission` and :class:`Role` enumerations and the
role-to-permission mapping that the Auth_Service uses to authorize administrative
operations (Requirements 16.1, 16.3).

The model is **fail-closed**: authorization defaults to *deny*. A role only grants
the permissions explicitly listed for it in :data:`ROLE_PERMISSIONS`; any role,
permission, or mapping that is unknown or absent yields ``False`` from
:func:`role_has_permission`. The ``require(permission)`` FastAPI dependency built on
top of this (task 3.3) therefore denies by default and grants only explicitly.

Routes declare a required :class:`Permission` and depend on :func:`require`, the
fail-closed FastAPI dependency that authenticates the operator's session token and
consults :func:`role_has_permission` before any handler logic runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Awaitable, Callable

from fastapi import Header

from gozar.auth.session import TOKEN_TYPE_ACCESS, decode_session_token
from gozar.core.config import Settings
from gozar.core.errors import AuthError, PermissionError


class Permission(str, Enum):
    """A single administrative capability a route may require.

    Permissions are granular and resource-oriented (steering §9) so a route can
    demand exactly the capability it needs rather than a coarse "is admin" check.
    """

    MANAGE_ACCOUNTS = "manage_accounts"
    MANAGE_TOKENS = "manage_tokens"
    MANAGE_CHAINS = "manage_chains"
    VIEW_TRACES = "view_traces"
    VIEW_ANALYTICS = "view_analytics"
    MANAGE_OPERATORS = "manage_operators"


class Role(str, Enum):
    """An operator role.

    Gozar's launch requirement is single-operator, but the design accommodates
    multi-operator RBAC. ``ADMIN`` holds every permission; ``VIEWER`` is a
    read-only role retained as a clean seam for future delegation.
    """

    ADMIN = "admin"
    VIEWER = "viewer"


# Explicit grant table. A role grants ONLY the permissions listed here. This is the
# single source of truth for authorization and the basis of the fail-closed default:
# anything not listed is denied.
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ADMIN: frozenset(Permission),
    Role.VIEWER: frozenset({Permission.VIEW_TRACES, Permission.VIEW_ANALYTICS}),
}


def role_has_permission(role: Role | str, permission: Permission | str) -> bool:
    """Return whether ``role`` is explicitly granted ``permission`` (fail-closed).

    Accepts either enum members or their string values (for example a ``role``
    column read back from the database). Any value that does not resolve to a known
    :class:`Role`/:class:`Permission`, or a role with no grant entry, returns
    ``False`` - the system denies by default and grants only explicitly.
    """
    try:
        role = Role(role)
        permission = Permission(permission)
    except ValueError:
        # Unknown role or permission -> deny.
        return False
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


@dataclass(frozen=True)
class Identity:
    """The authenticated operator identity resolved from a valid session token.

    Returned by the :func:`require` dependency to a route handler once the request
    has cleared authentication and authorization, so the handler can attribute the
    action to an operator without re-parsing the token.
    """

    operator_id: str
    role: str


def _extract_bearer_token(authorization: str | None) -> str:
    """Return the bearer token from an ``Authorization`` header, or fail closed.

    Raises :class:`~gozar.core.errors.AuthError` (HTTP 401) when the header is
    absent, uses a scheme other than ``Bearer``, or carries no token value. The
    error message never echoes the supplied header (Requirement 16.4).
    """
    if not authorization:
        raise AuthError("missing authentication credentials")
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise AuthError("invalid authentication credentials")
    return token


def require(
    permission: Permission,
    *,
    settings: Settings | None = None,
) -> Callable[[str | None], Awaitable[Identity]]:
    """Build a fail-closed FastAPI dependency guarding a single :class:`Permission`.

    The returned dependency enforces, in order and before any route handler runs
    (Requirements 16.1, 16.3):

    1. A valid operator identity must be presented as an ``Authorization: Bearer``
       access token. A missing, malformed, expired, tampered, wrong-secret, or
       wrong-type token raises :class:`~gozar.core.errors.AuthError` (HTTP 401).
    2. The token's role must be *explicitly* granted ``permission`` per
       :func:`role_has_permission`. An authenticated operator whose role lacks the
       permission raises :class:`~gozar.core.errors.PermissionError` (HTTP 403).

    Authorization defaults to deny: only a valid identity holding the required
    permission reaches the handler, which then receives the resolved
    :class:`Identity`. Routes that must hide the existence of a protected resource
    map the 403 to 404 at the route level; this dependency owns the 401/403 gate.

    ``settings`` may be supplied to pin the JWT verification configuration (used in
    tests); when omitted the process configuration is used.
    """

    async def _dependency(
        authorization: Annotated[str | None, Header()] = None,
    ) -> Identity:
        token = _extract_bearer_token(authorization)
        # Verifies signature, expiry, and that this is an access (not refresh) token;
        # raises AuthError (401) on any invalid/expired/tampered/wrong-type token.
        claims = decode_session_token(token, TOKEN_TYPE_ACCESS, settings=settings)
        role = claims.get("role", "")
        if not role_has_permission(role, permission):
            # Authenticated but not authorized -> fail closed with 403.
            raise PermissionError(
                "you do not have permission to perform this action"
            )
        return Identity(operator_id=str(claims.get("sub", "")), role=str(role))

    return _dependency
