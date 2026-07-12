"""Unit tests for first-run bootstrap gating and credential policy (task 3.5).

Covers two Auth_Service guarantees:

* **First-run bootstrap gating (Requirement 19.2):** until an :class:`Operator`
  exists the instance is un-bootstrapped and administrative access is closed.
  ``bootstrap_required`` reports that state, ``create_initial_admin`` is the only
  path that establishes the first admin, and once an admin exists that path closes
  with a 409 so a second "initial" admin can never be created.

* **Configured credential policy (Requirement 16.5):** ``enforce_credential_policy``
  rejects credentials that violate the configured rules and accepts compliant ones,
  reading every threshold from :class:`~gozar.core.config.Settings` (no magic
  numbers). ``create_initial_admin`` enforces that policy before persisting an
  operator.

The async session is exercised through a tiny in-memory fake (mirroring the
fake-session pattern in ``test_session_tokens.py``) so these tests drive the real
service code paths without a database.
"""

from __future__ import annotations

import pytest

from gozar.auth.rbac import Role
from gozar.auth.service import (
    BootstrapAlreadyCompleteError,
    bootstrap_required,
    create_initial_admin,
    enforce_credential_policy,
    verify_password,
)
from gozar.core.config import Settings
from gozar.core.errors import ValidationError


def _settings(**overrides) -> Settings:
    """Settings with an explicit, fully-enabled credential policy for determinism."""
    base = {
        "jwt_secret": "unit-test-jwt-secret-value",
        "username_min_length": 3,
        "password_min_length": 12,
        "password_require_uppercase": True,
        "password_require_lowercase": True,
        "password_require_digit": True,
        "password_require_symbol": True,
    }
    base.update(overrides)
    return Settings(**base)


# A password that satisfies the fully-enabled policy above.
_VALID_PASSWORD = "Sup3rSecret!pass"
_VALID_USERNAME = "operator-1"


class _FakeSession:
    """In-memory fake of the async session used by the bootstrap code paths.

    Tracks the operators that have been ``add``-ed so that the ``COUNT`` query in
    :func:`bootstrap_required` reflects reality, and records whether ``flush`` ran.
    """

    def __init__(self, operators=None) -> None:
        self.operators = list(operators or [])
        self.flush_calls = 0

    async def scalar(self, statement):  # signature-compatible with AsyncSession
        # bootstrap_required issues `select(func.count()).select_from(Operator)`.
        if "count" in str(statement).lower():
            return len(self.operators)
        # Any other lookup (by username) returns the first stored operator or None.
        return self.operators[0] if self.operators else None

    def add(self, obj) -> None:
        self.operators.append(obj)

    async def flush(self) -> None:
        self.flush_calls += 1


# --- First-run bootstrap gating (Requirement 19.2) ---------------------------

async def test_bootstrap_required_true_when_no_operator_exists() -> None:
    session = _FakeSession()
    assert await bootstrap_required(session) is True


async def test_bootstrap_required_false_once_an_operator_exists() -> None:
    session = _FakeSession(operators=[object()])
    assert await bootstrap_required(session) is False


async def test_create_initial_admin_creates_admin_with_hashed_password() -> None:
    settings = _settings()
    session = _FakeSession()

    operator = await create_initial_admin(
        session, _VALID_USERNAME, _VALID_PASSWORD, settings=settings
    )

    # The first admin is persisted with the ADMIN role.
    assert operator.username == _VALID_USERNAME
    assert operator.role == Role.ADMIN.value
    assert session.operators == [operator]
    assert session.flush_calls == 1

    # The password is stored only as a verifiable hash, never as plaintext.
    assert operator.password_hash != _VALID_PASSWORD
    assert verify_password(operator.password_hash, _VALID_PASSWORD) is True


async def test_bootstrap_gate_closes_after_initial_admin_is_created() -> None:
    settings = _settings()
    session = _FakeSession()

    assert await bootstrap_required(session) is True
    await create_initial_admin(session, _VALID_USERNAME, _VALID_PASSWORD, settings=settings)
    # Once an admin exists the instance is bootstrapped and the gate is closed.
    assert await bootstrap_required(session) is False


async def test_create_initial_admin_blocked_once_bootstrapped() -> None:
    settings = _settings()
    session = _FakeSession(operators=[object()])

    with pytest.raises(BootstrapAlreadyCompleteError) as excinfo:
        await create_initial_admin(
            session, "another-admin", _VALID_PASSWORD, settings=settings
        )

    # The closed path is a 409 and must not create a second operator.
    assert excinfo.value.status_code == 409
    assert len(session.operators) == 1
    assert session.flush_calls == 0


async def test_create_initial_admin_with_weak_password_creates_no_operator() -> None:
    settings = _settings()
    session = _FakeSession()

    with pytest.raises(ValidationError):
        await create_initial_admin(session, _VALID_USERNAME, "short", settings=settings)

    # Policy is enforced before persistence: the instance stays un-bootstrapped.
    assert session.operators == []
    assert session.flush_calls == 0
    assert await bootstrap_required(session) is True


# --- Configured credential policy (Requirement 16.5) -------------------------

def test_policy_accepts_compliant_credentials() -> None:
    # A compliant credential returns None (no exception).
    assert (
        enforce_credential_policy(
            _VALID_USERNAME, _VALID_PASSWORD, settings=_settings()
        )
        is None
    )


def test_policy_rejects_short_username() -> None:
    with pytest.raises(ValidationError) as excinfo:
        enforce_credential_policy("ab", _VALID_PASSWORD, settings=_settings())
    assert any(d["field"] == "username" for d in excinfo.value.details)


def test_policy_rejects_short_password() -> None:
    with pytest.raises(ValidationError) as excinfo:
        enforce_credential_policy(_VALID_USERNAME, "Ab1!xyz", settings=_settings())
    assert any(d["field"] == "password" for d in excinfo.value.details)


def test_policy_rejects_password_missing_uppercase() -> None:
    with pytest.raises(ValidationError):
        enforce_credential_policy(
            _VALID_USERNAME, "sup3rsecret!pass", settings=_settings()
        )


def test_policy_rejects_password_missing_lowercase() -> None:
    with pytest.raises(ValidationError):
        enforce_credential_policy(
            _VALID_USERNAME, "SUP3RSECRET!PASS", settings=_settings()
        )


def test_policy_rejects_password_missing_digit() -> None:
    with pytest.raises(ValidationError):
        enforce_credential_policy(
            _VALID_USERNAME, "SuperSecret!pass", settings=_settings()
        )


def test_policy_rejects_password_missing_symbol() -> None:
    with pytest.raises(ValidationError):
        enforce_credential_policy(
            _VALID_USERNAME, "Sup3rSecretpass", settings=_settings()
        )


def test_policy_collects_all_violations_together() -> None:
    # A short, all-lowercase, no-digit, no-symbol password plus a short username
    # should surface multiple distinct violations in one error.
    with pytest.raises(ValidationError) as excinfo:
        enforce_credential_policy("ab", "short", settings=_settings())
    fields = {d["field"] for d in excinfo.value.details}
    assert {"username", "password"} <= fields
    assert len(excinfo.value.details) >= 3


def test_policy_is_config_driven_relaxed_rules_accept_simpler_password() -> None:
    # Disabling the character-class rules and lowering the minimum length is honored
    # by the enforcement logic - the policy comes entirely from configuration.
    relaxed = _settings(
        password_min_length=4,
        password_require_uppercase=False,
        password_require_lowercase=False,
        password_require_digit=False,
        password_require_symbol=False,
        username_min_length=1,
    )
    assert enforce_credential_policy("a", "abcd", settings=relaxed) is None


def test_policy_is_config_driven_stricter_length_rejects_borderline_password() -> None:
    # Raising the minimum length rejects a password the default policy would accept.
    stricter = _settings(password_min_length=64)
    with pytest.raises(ValidationError):
        enforce_credential_policy(_VALID_USERNAME, _VALID_PASSWORD, settings=stricter)
