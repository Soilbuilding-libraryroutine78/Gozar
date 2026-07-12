"""Tests for the public auth control-path router (``/api/auth``).

Exercises the operator-authentication HTTP surface the Web_Console depends on,
driving the real app through ``TestClient`` against the in-memory database (the
project's test convention) with no network or real database/Redis:

* **Bootstrap gating (Requirement 19.2):** a fresh instance reports
  ``bootstrap_required: true``; ``POST /api/auth/bootstrap`` creates the first admin
  and logs them in; once an admin exists the status flips to ``false`` and a second
  bootstrap attempt fails closed with 409. The credential policy is enforced on
  creation (Requirement 16.5).
* **Login (Requirement 16.1):** valid credentials return a ``SessionTokens`` bundle;
  a wrong password or unknown user fails with a generic 401 that never reveals which
  half was wrong.
* **Refresh:** a refresh token issued at login is exchanged for a fresh pair; an
  invalid/garbage refresh token (and an access token used as a refresh token) is
  rejected with 401.
* The auth routes are public (no ``Authorization`` header needed), unlike every other
  admin route.

The ``client``, ``settings``, and ``sessionmaker`` fixtures come from
:mod:`tests.api.conftest`.
"""

from __future__ import annotations

import pytest

# A username/password that satisfy the default credential policy (12+ chars, upper,
# lower, digit, symbol).
_USERNAME = "operator-1"
_PASSWORD = "Sup3rSecret!pass"


def _token_bundle_is_wellformed(body: dict) -> None:
    """Assert a response body is a complete SessionTokens bundle."""
    assert set(body) == {"access_token", "refresh_token", "token_type", "expires_in"}
    assert body["token_type"] == "bearer"
    assert isinstance(body["expires_in"], int) and body["expires_in"] > 0
    assert body["access_token"] and body["refresh_token"]
    assert body["access_token"] != body["refresh_token"]


# --- Bootstrap gating (Requirement 19.2) -------------------------------------


def test_bootstrap_status_true_on_fresh_instance(client):
    """A fresh instance reports that first-run bootstrap is still required."""
    resp = client.get("/api/auth/bootstrap")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"bootstrap_required": True}


def test_bootstrap_creates_first_admin_and_logs_in(client):
    """Creating the first admin returns a session bundle and is auto-logged-in."""
    resp = client.post(
        "/api/auth/bootstrap", json={"username": _USERNAME, "password": _PASSWORD}
    )
    assert resp.status_code == 201, resp.text
    _token_bundle_is_wellformed(resp.json())

    # The gate is now closed: bootstrap is no longer required.
    status_resp = client.get("/api/auth/bootstrap")
    assert status_resp.json() == {"bootstrap_required": False}


def test_second_bootstrap_is_rejected_with_409(client):
    """Once an admin exists the first-run bootstrap path fails closed (409)."""
    first = client.post(
        "/api/auth/bootstrap", json={"username": _USERNAME, "password": _PASSWORD}
    )
    assert first.status_code == 201, first.text

    second = client.post(
        "/api/auth/bootstrap",
        json={"username": "operator-2", "password": _PASSWORD},
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "BOOTSTRAP_ALREADY_COMPLETE"


def test_bootstrap_enforces_credential_policy(client):
    """A weak password is rejected (400) and creates no admin (Requirement 16.5)."""
    resp = client.post(
        "/api/auth/bootstrap", json={"username": _USERNAME, "password": "short"}
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    # The instance stays un-bootstrapped.
    assert client.get("/api/auth/bootstrap").json() == {"bootstrap_required": True}


# --- Login (Requirement 16.1) ------------------------------------------------


def test_login_success_returns_session_tokens(client):
    """A correct credential pair returns a well-formed session token bundle."""
    client.post(
        "/api/auth/bootstrap", json={"username": _USERNAME, "password": _PASSWORD}
    )

    resp = client.post(
        "/api/auth/login", json={"username": _USERNAME, "password": _PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    _token_bundle_is_wellformed(resp.json())


def test_login_wrong_password_is_rejected_with_401(client):
    """A wrong password fails with a generic 401 and no token."""
    client.post(
        "/api/auth/bootstrap", json={"username": _USERNAME, "password": _PASSWORD}
    )

    resp = client.post(
        "/api/auth/login", json={"username": _USERNAME, "password": "Wr0ngPass!word"}
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "AUTHENTICATION_ERROR"


def test_login_unknown_user_is_rejected_with_401(client):
    """An unknown username fails with the same generic 401 (no user enumeration)."""
    client.post(
        "/api/auth/bootstrap", json={"username": _USERNAME, "password": _PASSWORD}
    )

    resp = client.post(
        "/api/auth/login", json={"username": "nobody", "password": _PASSWORD}
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "AUTHENTICATION_ERROR"


def test_login_issued_access_token_authorizes_admin_routes(client):
    """A token minted by login is accepted by a fail-closed admin route."""
    client.post(
        "/api/auth/bootstrap", json={"username": _USERNAME, "password": _PASSWORD}
    )
    login = client.post(
        "/api/auth/login", json={"username": _USERNAME, "password": _PASSWORD}
    )
    access = login.json()["access_token"]

    resp = client.get("/api/accounts", headers={"Authorization": f"Bearer {access}"})
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)


# --- Refresh -----------------------------------------------------------------


def test_refresh_returns_new_session_tokens(client):
    """A valid refresh token is exchanged for a fresh session bundle."""
    client.post(
        "/api/auth/bootstrap", json={"username": _USERNAME, "password": _PASSWORD}
    )
    login = client.post(
        "/api/auth/login", json={"username": _USERNAME, "password": _PASSWORD}
    )
    refresh_token = login.json()["refresh_token"]

    resp = client.post("/api/auth/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 200, resp.text
    _token_bundle_is_wellformed(resp.json())


def test_refresh_with_garbage_token_is_rejected_with_401(client):
    """A malformed refresh token is rejected with 401."""
    resp = client.post("/api/auth/refresh", json={"refresh_token": "not-a-jwt"})
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "AUTHENTICATION_ERROR"


def test_refresh_rejects_an_access_token(client):
    """An access token must not be usable where a refresh token is required."""
    client.post(
        "/api/auth/bootstrap", json={"username": _USERNAME, "password": _PASSWORD}
    )
    login = client.post(
        "/api/auth/login", json={"username": _USERNAME, "password": _PASSWORD}
    )
    access_token = login.json()["access_token"]

    resp = client.post("/api/auth/refresh", json={"refresh_token": access_token})
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "AUTHENTICATION_ERROR"


# --- Auth routes are public --------------------------------------------------


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/api/auth/bootstrap", None),
        ("post", "/api/auth/login", {"username": "x", "password": "y"}),
        ("post", "/api/auth/refresh", {"refresh_token": "z"}),
    ],
)
def test_auth_routes_do_not_require_authentication(client, method, path, body):
    """The auth routes never return 401/403 for *missing* admin credentials.

    They may fail for bad input (a wrong password is 401 AUTHENTICATION_ERROR), but
    they are reachable without an ``Authorization`` header, unlike guarded routes.
    """
    kwargs = {"json": body} if body is not None else {}
    resp = getattr(client, method)(path, **kwargs)
    # Not a permission denial, and the route is served (not 404/405).
    assert resp.status_code != 403
    assert resp.status_code not in (404, 405), resp.text
