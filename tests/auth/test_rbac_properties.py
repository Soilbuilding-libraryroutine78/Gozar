"""Property-based test for fail-closed administrative access (task 3.4).

Validates Property 21 from the Gozar design: any administrative or credential-
management route protected by the :func:`gozar.auth.rbac.require` dependency rejects a
request that lacks a valid operator identity with an authentication error (HTTP 401)
*before any handler logic executes* (Requirements 16.1, 16.3).

The property drives the real dependency through a real FastAPI app and ``TestClient``
(no mocking of the auth path): a route is mounted behind ``require(...)`` and its
handler records every invocation. Hypothesis then explores the space of *invalid
identities* - absent, malformed, wrong-scheme, expired, tampered, wrong-secret, and
wrong-type tokens - and asserts that every one is rejected with 401 and that the guarded
handler never ran. The two anchoring unit tests confirm the gate is not vacuous: a valid
token holding the permission reaches the handler (200), and an authenticated operator
whose role lacks the permission is denied (403).
"""

from __future__ import annotations

import uuid

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from gozar.auth.rbac import Identity, Permission, require
from gozar.auth.session import issue_session_tokens
from gozar.core.config import Settings
from gozar.core.errors import register_exception_handlers

# Fixed verification configuration so generated tokens are deterministic and isolated
# from the process environment.
_SETTINGS = Settings(
    jwt_secret="rbac-property-test-jwt-secret-value",
    jwt_access_ttl_seconds=900,
    jwt_refresh_ttl_seconds=1_209_600,
)
_OTHER_SETTINGS = Settings(
    jwt_secret="a-completely-different-secret-value",
    jwt_access_ttl_seconds=900,
    jwt_refresh_ttl_seconds=1_209_600,
)

# The permission the guarded route demands throughout these tests.
_REQUIRED = Permission.MANAGE_ACCOUNTS


def _build_app(settings: Settings) -> tuple[TestClient, dict[str, int]]:
    """Mount a single route guarded by ``require(_REQUIRED)`` and a call counter.

    Returns the client plus a mutable ``state`` dict whose ``handler_calls`` is
    incremented only if the protected handler body actually executes, letting the
    property assert that rejected requests never reach handler logic.
    """
    app = FastAPI()
    register_exception_handlers(app)
    state = {"handler_calls": 0}

    @app.get("/admin/accounts")
    async def list_accounts(
        identity: Identity = Depends(require(_REQUIRED, settings=settings)),
    ) -> dict:
        state["handler_calls"] += 1
        return {"operator": identity.operator_id, "role": identity.role}

    return TestClient(app, raise_server_exceptions=False), state


def _access_token(role: str, settings: Settings) -> str:
    """Issue a valid access token for ``role`` under ``settings``."""
    return issue_session_tokens(uuid.uuid4(), role, settings=settings).access_token


# HTTP header values must be transmittable as ASCII, so generated strings are drawn
# from printable ASCII (space through ``~``). This keeps inputs within the space of
# header values a client could actually send, rather than exercising the HTTP client's
# own encoding limits.
_header_text = st.text(
    alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E),
    min_size=0,
    max_size=40,
)
# Header values that do NOT carry a bearer scheme followed by a JWT, so they can never
# accidentally collide with a genuine credential.
_non_bearer_text = _header_text.filter(
    lambda s: not s.lower().startswith("bearer ")
)


@st.composite
def _invalid_authorization(draw: st.DrawFn) -> tuple[str, str | None]:
    """Generate an ``(kind, header_value)`` pair representing an invalid identity.

    ``header_value`` is ``None`` when no ``Authorization`` header should be sent. Every
    generated case must fail authentication: there is deliberately no branch that yields
    a valid, permission-bearing access token.
    """
    kind = draw(
        st.sampled_from(
            [
                "missing",
                "empty",
                "non_bearer_text",
                "wrong_scheme",
                "bearer_garbage",
                "expired_access",
                "refresh_token",
                "wrong_secret",
                "tampered",
            ]
        )
    )
    # Even tokens that WOULD carry the required permission must be rejected when the
    # identity itself is invalid, so admin is used freely below.
    role = draw(st.sampled_from(["admin", "viewer", "", "superuser"]))

    if kind == "missing":
        return kind, None
    if kind == "empty":
        return kind, ""
    if kind == "non_bearer_text":
        return kind, draw(_non_bearer_text)
    if kind == "wrong_scheme":
        scheme = draw(st.sampled_from(["Basic", "Token", "Digest", "JWT"]))
        return kind, f"{scheme} {draw(st.text(alphabet=st.characters(min_codepoint=0x21, max_codepoint=0x7E), min_size=1, max_size=30))}"
    if kind == "bearer_garbage":
        return kind, "Bearer " + draw(
            st.text(alphabet=st.characters(min_codepoint=0x21, max_codepoint=0x7E), min_size=1, max_size=40)
        )
    if kind == "expired_access":
        expired = Settings(
            jwt_secret=_SETTINGS.jwt_secret,
            jwt_access_ttl_seconds=-1,
            jwt_refresh_ttl_seconds=_SETTINGS.jwt_refresh_ttl_seconds,
        )
        token = issue_session_tokens(uuid.uuid4(), role, settings=expired).access_token
        return kind, f"Bearer {token}"
    if kind == "refresh_token":
        # A refresh token must not authorize an access-protected route (wrong type).
        token = issue_session_tokens(
            uuid.uuid4(), role, settings=_SETTINGS
        ).refresh_token
        return kind, f"Bearer {token}"
    if kind == "wrong_secret":
        token = _access_token(role, _OTHER_SETTINGS)
        return kind, f"Bearer {token}"
    # "tampered": mutate a character in the interior of the payload segment. Flipping a
    # byte of the signed content reliably breaks the HMAC signature (unlike flipping the
    # final base64 character, whose trailing bits may be ignored), so this must 401.
    token = _access_token(role, _SETTINGS)
    return kind, f"Bearer {_tamper_payload(token)}"


def _tamper_payload(token: str) -> str:
    """Return ``token`` with one interior payload character changed.

    Splits the JWT into ``header.payload.signature`` and replaces a character roughly
    in the middle of the payload segment with a different base64url character. This
    alters the signed content so HMAC verification fails deterministically.
    """
    header, payload, signature = token.split(".")
    index = len(payload) // 2
    original = payload[index]
    replacement = "A" if original != "A" else "B"
    mutated_payload = payload[:index] + replacement + payload[index + 1 :]
    return f"{header}.{mutated_payload}.{signature}"


# Feature: gozar, Property 21: For any administrative or credential-management route, a
# request without a valid operator identity is rejected with an authentication error
# before any handler logic executes.
@hyp_settings(max_examples=200)
@given(case=_invalid_authorization())
def test_request_without_valid_identity_is_rejected_before_handler(
    case: tuple[str, str | None],
) -> None:
    """Validates: Requirements 16.1, 16.3.

    For any request lacking a valid operator identity, the guarded route returns 401
    and the protected handler body never executes (fail-closed, deny by default).
    """
    _kind, header_value = case
    client, state = _build_app(_SETTINGS)

    headers = {} if header_value is None else {"Authorization": header_value}
    response = client.get("/admin/accounts", headers=headers)

    assert response.status_code == 401, (_kind, header_value, response.text)
    # The handler must not have run: rejection happens before any handler logic.
    assert state["handler_calls"] == 0
    # The error is an authentication error envelope, not a leak of the credential.
    assert response.json()["error"]["code"] == "AUTHENTICATION_ERROR"


# --- Anchoring unit tests: prove the gate is not vacuous ----------------------

def test_valid_token_with_permission_reaches_handler() -> None:
    """A valid admin session token holding the permission is granted (HTTP 200)."""
    client, state = _build_app(_SETTINGS)
    token = _access_token("admin", _SETTINGS)

    response = client.get(
        "/admin/accounts", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200, response.text
    assert state["handler_calls"] == 1
    assert response.json()["role"] == "admin"


def test_authenticated_but_unauthorized_is_denied_before_handler() -> None:
    """An authenticated operator whose role lacks the permission is denied (HTTP 403).

    The ``viewer`` role is not granted ``MANAGE_ACCOUNTS``; the request authenticates
    but must fail closed with 403 without running the handler.
    """
    client, state = _build_app(_SETTINGS)
    token = _access_token("viewer", _SETTINGS)

    response = client.get(
        "/admin/accounts", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 403, response.text
    assert state["handler_calls"] == 0
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"
