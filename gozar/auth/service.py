"""Auth_Service: operator password hashing, credential policy, and bootstrap.

This module implements the first slice of the Auth_Service (design "Auth_Service"):

* **Password hashing** with Argon2id via :class:`argon2.PasswordHasher`
  (:func:`hash_password` / :func:`verify_password`).
* **Credential policy enforcement** driven entirely by configuration
  (:func:`enforce_credential_policy`, Requirement 16.5). There are no magic numbers
  in the logic; every rule is read from :class:`~gozar.core.config.Settings`.
* **First-run bootstrap gating** (:func:`bootstrap_required`, Requirement 19.2): the
  instance has no administrative access until an :class:`Operator` exists.
* **Initial admin creation** (:func:`create_initial_admin`): allowed only while the
  instance is un-bootstrapped, enforces the credential policy, and persists an
  admin-role operator with an Argon2id password hash.

* **Login / session issuance** (:func:`authenticate`, Requirement 16.1): looks up an
  operator by username, verifies the Argon2id password, and on success issues
  short-lived signed session (JWT) tokens with a refresh token (see
  :mod:`gozar.auth.session`). Failed logins raise a generic :class:`AuthError` that
  never reveals whether the username or the password was wrong (no user enumeration).

The ``require(permission)`` dependency (task 3.3) is intentionally out of scope here;
:func:`gozar.auth.session.decode_session_token` provides the verification seam it
will build on.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import (
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gozar.auth.models import Operator
from gozar.auth.rbac import Role
from gozar.auth.session import SessionTokens, issue_session_tokens
from gozar.core.config import Settings, get_settings
from gozar.core.errors import AuthError, GozarError, ValidationError

# A single shared Argon2id hasher. argon2-cffi defaults to the Argon2id variant with
# sensible memory/time/parallelism parameters; these are tuning parameters, not
# secrets, and may be promoted to configuration if a deployment needs to adjust them.
_hasher = PasswordHasher()

# A fixed Argon2id hash used only to spend a comparable amount of time verifying a
# password when the username is unknown, so the unknown-user and wrong-password login
# paths are not trivially distinguishable by timing. Computed once at import; its
# plaintext is irrelevant and is never a valid operator credential.
_DUMMY_PASSWORD_HASH = _hasher.hash("gozar-dummy-password-for-constant-time-login")


class BootstrapAlreadyCompleteError(GozarError):
    """Raised when initial-admin creation is attempted after bootstrap (HTTP 409).

    Once any :class:`Operator` exists the instance is bootstrapped, and the
    first-run admin-creation path is closed (Requirement 19.2).
    """

    status_code = 409
    code = "BOOTSTRAP_ALREADY_COMPLETE"


def hash_password(password: str) -> str:
    """Return an Argon2id PHC-format hash of ``password``.

    The returned string embeds the algorithm, parameters, salt, and digest, so it is
    self-describing for verification. The plaintext password is never stored.
    """
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Verify ``password`` against a stored Argon2id ``password_hash``.

    Returns ``True`` on a match and ``False`` on any mismatch or malformed hash.
    Does not raise on a wrong password; callers branch on the boolean.
    """
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def enforce_credential_policy(
    username: str,
    password: str,
    *,
    settings: Settings | None = None,
) -> None:
    """Validate a username/password against the configured credential policy.

    Every rule is sourced from :class:`~gozar.core.config.Settings` (minimum lengths
    and character-class requirements); the function contains no hardcoded thresholds.
    Raises :class:`~gozar.core.errors.ValidationError` listing every violation when
    the credentials do not comply, and returns ``None`` when they do (Requirement
    16.5).
    """
    settings = settings or get_settings()
    violations: list[dict[str, str]] = []

    if len(username) < settings.username_min_length:
        violations.append(
            {
                "field": "username",
                "message": (
                    f"username must be at least {settings.username_min_length} "
                    "characters"
                ),
            }
        )

    if len(password) < settings.password_min_length:
        violations.append(
            {
                "field": "password",
                "message": (
                    f"password must be at least {settings.password_min_length} "
                    "characters"
                ),
            }
        )
    if settings.password_require_uppercase and not any(c.isupper() for c in password):
        violations.append(
            {"field": "password", "message": "password must contain an uppercase letter"}
        )
    if settings.password_require_lowercase and not any(c.islower() for c in password):
        violations.append(
            {"field": "password", "message": "password must contain a lowercase letter"}
        )
    if settings.password_require_digit and not any(c.isdigit() for c in password):
        violations.append(
            {"field": "password", "message": "password must contain a digit"}
        )
    if settings.password_require_symbol and not any(
        not c.isalnum() for c in password
    ):
        violations.append(
            {"field": "password", "message": "password must contain a symbol"}
        )

    if violations:
        raise ValidationError(
            "credentials do not meet the configured policy",
            details=violations,
        )


async def bootstrap_required(session: AsyncSession) -> bool:
    """Return ``True`` when no :class:`Operator` exists (first-run gating).

    While this returns ``True`` the instance has no administrative credential and
    administrative functions must remain closed until an admin is established
    (Requirement 19.2).
    """
    count = await session.scalar(select(func.count()).select_from(Operator))
    return (count or 0) == 0


async def create_initial_admin(
    session: AsyncSession,
    username: str,
    password: str,
    *,
    settings: Settings | None = None,
) -> Operator:
    """Create the first administrative operator (first-run only).

    Permitted only while :func:`bootstrap_required` is ``True``; once any operator
    exists this path is closed with :class:`BootstrapAlreadyCompleteError`. Enforces
    the configured credential policy before creating the operator, then persists an
    ``ADMIN``-role operator whose password is stored as an Argon2id hash.

    Raises:
        BootstrapAlreadyCompleteError: If an operator already exists.
        ValidationError: If the credentials violate the configured policy.
    """
    if not await bootstrap_required(session):
        raise BootstrapAlreadyCompleteError(
            "administrative bootstrap has already been completed"
        )

    enforce_credential_policy(username, password, settings=settings)

    operator = Operator(
        username=username,
        password_hash=hash_password(password),
        role=Role.ADMIN.value,
    )
    session.add(operator)
    # Flush so the row is assigned and uniqueness is enforced within this transaction
    # without committing; the session dependency owns the commit boundary.
    await session.flush()
    return operator


async def authenticate(
    session: AsyncSession,
    username: str,
    password: str,
    *,
    settings: Settings | None = None,
) -> SessionTokens:
    """Authenticate an operator and issue signed session tokens (Requirement 16.1).

    Looks up the operator by username and verifies the presented password against the
    stored Argon2id hash. On success, issues a short-lived signed access token plus a
    longer-lived refresh token (see :mod:`gozar.auth.session`).

    Both the "unknown username" and "wrong password" paths raise the **same** generic
    :class:`~gozar.core.errors.AuthError` so the response never reveals which half of
    the credential was wrong (no user enumeration). In the unknown-username case the
    password is still verified against a throwaway hash so the two paths take a
    comparable amount of time, avoiding a timing oracle that would distinguish them.

    On the credential policy (Requirement 16.5): the configured policy constrains how
    credentials are *created* (see :func:`enforce_credential_policy`). Re-validating a
    presented password's complexity at login provides no security benefit and would
    risk both locking out operators whose password predates a policy change and
    turning a wrong-password attempt into a distinguishable validation error. The
    enforcement applicable at authentication time is therefore the authentication
    check itself; there are no additional login-time policy knobs in configuration.

    Never logs the username, password, or issued tokens.
    """
    settings = settings or get_settings()

    operator = await session.scalar(
        select(Operator).where(Operator.username == username)
    )

    if operator is None:
        # Perform a dummy verification so the unknown-user path costs roughly the same
        # as the wrong-password path, then fail with the identical generic error.
        verify_password(_DUMMY_PASSWORD_HASH, password)
        raise AuthError("invalid username or password")

    if not verify_password(operator.password_hash, password):
        raise AuthError("invalid username or password")

    return issue_session_tokens(operator.id, operator.role, settings=settings)
