"""Unit tests for signed operator session tokens and login (task 3.2).

Covers Requirement 16.1 (short-lived signed session tokens with refresh) and the
no-user-enumeration login behavior:

* a successful login issues a verifiable access + refresh pair;
* a wrong password and an unknown username both raise the same generic ``AuthError``;
* expired, tampered, and wrong-type tokens are rejected on decode;
* a refresh token can be exchanged for a new, valid access token.

The operator lookup is exercised through a tiny in-memory fake of the async session
(``scalar``), keeping these tests free of a real database while still driving the real
``authenticate`` code path.
"""

from __future__ import annotations

import uuid

import jwt
import pytest

from gozar.auth.service import authenticate, hash_password
from gozar.auth.session import (
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_REFRESH,
    decode_session_token,
    issue_session_tokens,
    refresh_session,
)
from gozar.core.config import Settings
from gozar.core.errors import AuthError


def _settings(**overrides) -> Settings:
    """Settings carrying a fixed JWT secret and sane TTLs for deterministic tests."""
    base = {
        "jwt_secret": "unit-test-jwt-secret-value",
        "jwt_access_ttl_seconds": 900,
        "jwt_refresh_ttl_seconds": 1_209_600,
    }
    base.update(overrides)
    return Settings(**base)


class _FakeOperator:
    """Minimal stand-in for the Operator ORM row used by ``authenticate``."""

    def __init__(self, username: str, password: str, role: str = "admin") -> None:
        self.id = uuid.uuid4()
        self.username = username
        self.password_hash = hash_password(password)
        self.role = role


class _FakeSession:
    """In-memory fake of the async session, returning a preset operator (or None)."""

    def __init__(self, operator: _FakeOperator | None) -> None:
        self._operator = operator

    async def scalar(self, _statement):  # signature-compatible with AsyncSession
        return self._operator


# --- Token issuance / decoding ------------------------------------------------

def test_issue_session_tokens_are_valid_and_carry_expected_claims() -> None:
    settings = _settings()
    operator_id = uuid.uuid4()

    tokens = issue_session_tokens(operator_id, "admin", settings=settings)

    assert tokens.token_type == "bearer"
    assert tokens.expires_in == settings.jwt_access_ttl_seconds
    assert tokens.access_token and tokens.refresh_token
    assert tokens.access_token != tokens.refresh_token

    access_claims = decode_session_token(
        tokens.access_token, TOKEN_TYPE_ACCESS, settings=settings
    )
    assert access_claims["sub"] == str(operator_id)
    assert access_claims["role"] == "admin"
    assert access_claims["type"] == TOKEN_TYPE_ACCESS
    assert "jti" in access_claims

    refresh_claims = decode_session_token(
        tokens.refresh_token, TOKEN_TYPE_REFRESH, settings=settings
    )
    assert refresh_claims["type"] == TOKEN_TYPE_REFRESH
    # Distinct token ids so individual tokens are addressable.
    assert access_claims["jti"] != refresh_claims["jti"]


def test_expired_access_token_is_rejected() -> None:
    # Negative TTL makes the access token already expired at issue time.
    settings = _settings(jwt_access_ttl_seconds=-1)
    tokens = issue_session_tokens(uuid.uuid4(), "admin", settings=settings)

    with pytest.raises(AuthError):
        decode_session_token(tokens.access_token, TOKEN_TYPE_ACCESS, settings=settings)


def test_tampered_token_is_rejected() -> None:
    settings = _settings()
    tokens = issue_session_tokens(uuid.uuid4(), "admin", settings=settings)

    # Mutate a character in the MIDDLE of the payload segment. Flipping the last
    # base64url character of the signature is unreliable (its trailing bits can decode
    # to the same bytes, leaving the signature valid); changing an interior payload
    # character always alters the signed content, so verification fails deterministically.
    header, payload, signature = tokens.access_token.split(".")
    index = len(payload) // 2
    original = payload[index]
    mutated_payload = payload[:index] + ("A" if original != "A" else "B") + payload[index + 1 :]
    tampered = f"{header}.{mutated_payload}.{signature}"

    with pytest.raises(AuthError):
        decode_session_token(tampered, TOKEN_TYPE_ACCESS, settings=settings)


def test_token_signed_with_other_secret_is_rejected() -> None:
    settings = _settings()
    tokens = issue_session_tokens(uuid.uuid4(), "admin", settings=settings)

    other = _settings(jwt_secret="a-completely-different-secret")
    with pytest.raises(AuthError):
        decode_session_token(tokens.access_token, TOKEN_TYPE_ACCESS, settings=other)


def test_wrong_type_token_is_rejected() -> None:
    settings = _settings()
    tokens = issue_session_tokens(uuid.uuid4(), "admin", settings=settings)

    # An access token must not validate where a refresh token is expected.
    with pytest.raises(AuthError):
        decode_session_token(tokens.access_token, TOKEN_TYPE_REFRESH, settings=settings)
    # ...and vice-versa.
    with pytest.raises(AuthError):
        decode_session_token(tokens.refresh_token, TOKEN_TYPE_ACCESS, settings=settings)


def test_empty_token_is_rejected() -> None:
    settings = _settings()
    with pytest.raises(AuthError):
        decode_session_token("", TOKEN_TYPE_ACCESS, settings=settings)


def test_token_missing_required_claim_is_rejected() -> None:
    # A token without the required ``type``/``exp`` claims must not decode.
    settings = _settings()
    bare = jwt.encode({"sub": "x"}, settings.jwt_secret, algorithm="HS256")
    with pytest.raises(AuthError):
        decode_session_token(bare, TOKEN_TYPE_ACCESS, settings=settings)


# --- Refresh ------------------------------------------------------------------

def test_refresh_issues_a_new_valid_access_token() -> None:
    settings = _settings()
    operator_id = uuid.uuid4()
    original = issue_session_tokens(operator_id, "admin", settings=settings)

    refreshed = refresh_session(original.refresh_token, settings=settings)

    claims = decode_session_token(
        refreshed.access_token, TOKEN_TYPE_ACCESS, settings=settings
    )
    assert claims["sub"] == str(operator_id)
    assert claims["role"] == "admin"


def test_refresh_rejects_an_access_token() -> None:
    settings = _settings()
    tokens = issue_session_tokens(uuid.uuid4(), "admin", settings=settings)

    # Presenting an access token where a refresh token is required must fail.
    with pytest.raises(AuthError):
        refresh_session(tokens.access_token, settings=settings)


# --- Login (authenticate) -----------------------------------------------------

@pytest.mark.asyncio
async def test_authenticate_success_issues_valid_tokens() -> None:
    settings = _settings()
    operator = _FakeOperator("operator-1", "Sup3rSecret!pass", role="admin")
    session = _FakeSession(operator)

    tokens = await authenticate(
        session, "operator-1", "Sup3rSecret!pass", settings=settings
    )

    claims = decode_session_token(
        tokens.access_token, TOKEN_TYPE_ACCESS, settings=settings
    )
    assert claims["sub"] == str(operator.id)
    assert claims["role"] == "admin"


@pytest.mark.asyncio
async def test_authenticate_wrong_password_raises_autherror() -> None:
    settings = _settings()
    operator = _FakeOperator("operator-1", "Sup3rSecret!pass")
    session = _FakeSession(operator)

    with pytest.raises(AuthError):
        await authenticate(session, "operator-1", "wrong-password", settings=settings)


@pytest.mark.asyncio
async def test_authenticate_unknown_user_raises_autherror() -> None:
    settings = _settings()
    session = _FakeSession(None)

    with pytest.raises(AuthError):
        await authenticate(session, "ghost", "any-password", settings=settings)


@pytest.mark.asyncio
async def test_authenticate_generic_message_does_not_enumerate_users() -> None:
    # Wrong-password and unknown-user must surface the identical message so a caller
    # cannot tell which half of the credential was wrong.
    settings = _settings()
    known = _FakeSession(_FakeOperator("operator-1", "Sup3rSecret!pass"))
    unknown = _FakeSession(None)

    with pytest.raises(AuthError) as wrong_pw:
        await authenticate(known, "operator-1", "nope", settings=settings)
    with pytest.raises(AuthError) as no_user:
        await authenticate(unknown, "ghost", "nope", settings=settings)

    assert str(wrong_pw.value) == str(no_user.value)
