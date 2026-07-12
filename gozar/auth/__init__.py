"""Auth_Service: operator authentication, RBAC, session/JWT.

Public surface for the auth slices implemented so far:

* the :class:`Operator` model and fail-closed RBAC primitives,
* operator hashing / credential-policy / bootstrap service functions (task 3.1),
* login and signed session (JWT) tokens with refresh (task 3.2).
"""

from gozar.auth.models import Operator
from gozar.auth.rbac import (
    ROLE_PERMISSIONS,
    Permission,
    Role,
    role_has_permission,
)
from gozar.auth.service import (
    BootstrapAlreadyCompleteError,
    authenticate,
    bootstrap_required,
    create_initial_admin,
    enforce_credential_policy,
    hash_password,
    verify_password,
)
from gozar.auth.session import (
    BEARER,
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_REFRESH,
    SessionTokens,
    decode_session_token,
    issue_session_tokens,
    refresh_session,
)

__all__ = [
    "Operator",
    "Permission",
    "Role",
    "ROLE_PERMISSIONS",
    "role_has_permission",
    "BootstrapAlreadyCompleteError",
    "authenticate",
    "bootstrap_required",
    "create_initial_admin",
    "enforce_credential_policy",
    "hash_password",
    "verify_password",
    "BEARER",
    "TOKEN_TYPE_ACCESS",
    "TOKEN_TYPE_REFRESH",
    "SessionTokens",
    "decode_session_token",
    "issue_session_tokens",
    "refresh_session",
]
