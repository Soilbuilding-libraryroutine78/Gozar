"""Access-control tests for the admin control-path (``/api``) routers.

Validates the fail-closed RBAC gate on every admin route group (steering 19.4,
Requirements 16.1, 16.3, 16.4):

* Unauthenticated requests are rejected with 401 before any handler logic runs.
* An authenticated ADMIN (holding every permission) can reach every group.
* An authenticated VIEWER (read-only: ``VIEW_TRACES`` + ``VIEW_ANALYTICS``) is denied
  on the manage-only groups (accounts, tokens, chains) with 403, yet may read the
  trace and analytics groups it is explicitly granted.
* The list endpoints (accounts, tokens) never leak secret material -- no Client_Token
  secret and no credential ciphertext appears in the response body (Requirement 16.4).

The tests drive the real app through ``TestClient`` (no mocking of the auth path); the
only injected seam is API-key validation, so seeding an account touches no network.
The ``client``, ``settings``, ``sessionmaker``, and ``auth_header`` fixtures are
provided by :mod:`tests.api.conftest`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from gozar.accounts.service import connect_api_key
from gozar.tokens.service import create_token, list_tokens

# A recognisable API-key secret value the list endpoint must never echo back.
_ACCOUNT_API_KEY = "sk-super-secret-account-key-DO-NOT-LEAK"

# A fixed analytics window so the analytics routes have their required query params.
_RANGE = {
    "start": datetime(2024, 1, 1, tzinfo=timezone.utc).isoformat(),
    "end": datetime(2024, 1, 2, tzinfo=timezone.utc).isoformat(),
}

# Manage-only route groups: guarded by a MANAGE_* permission the VIEWER lacks.
_MANAGE_ONLY_READS = ["/api/accounts", "/api/tokens", "/api/chains"]

# Representative endpoints across every group, used for the unauthenticated sweep.
_ALL_ENDPOINTS = [
    ("get", "/api/accounts", None),
    ("post", "/api/accounts/connect/api-key", {"provider": "openai", "api_key": "x"}),
    ("get", "/api/tokens", None),
    ("post", "/api/tokens", {"label": "x"}),
    (
        "post",
        "/api/tokens/00000000-0000-4000-8000-000000000001/reveal",
        {"password": "x"},
    ),
    ("get", "/api/chains", None),
    ("post", "/api/chains", {"name": "x", "account_ids": []}),
    ("get", "/api/models", None),
    ("put", "/api/models/providers/codex", {"models": ["gpt-5.5"]}),
    ("get", "/api/traces", None),
    ("get", "/api/analytics/system", None),
]


async def _accept_key(entry, api_key):  # noqa: ANN001
    """Injected API-key validation that accepts the key without a network call."""
    return None


@pytest_asyncio.fixture
async def seeded(sessionmaker, settings):
    """Seed one Client_Token and one (encrypted) API-key account; return their secrets."""
    async with sessionmaker() as session:
        issued = await create_token(session, "leak-check-token", None, settings=settings)
        credential = await connect_api_key(
            session,
            "openai",
            _ACCOUNT_API_KEY,
            settings=settings,
            validate=_accept_key,
        )
        await session.commit()
        return {"token_secret": issued.secret, "account_id": credential.id}


# --- 1. Unauthenticated requests are rejected with 401 ------------------------


@pytest.mark.parametrize("method,path,body", _ALL_ENDPOINTS)
def test_unauthenticated_request_is_rejected_with_401(client, method, path, body):
    """Every admin endpoint fails closed with 401 when no credential is presented."""
    kwargs: dict = {}
    if body is not None:
        kwargs["json"] = body
    if path.startswith("/api/analytics"):
        kwargs["params"] = _RANGE
    resp = getattr(client, method)(path, **kwargs)
    assert resp.status_code == 401, (method, path, resp.text)
    assert resp.json()["error"]["code"] == "AUTHENTICATION_ERROR"


def test_unauthenticated_invalid_bearer_is_rejected_with_401(client):
    """A malformed/garbage bearer token is rejected with 401 as well."""
    resp = client.get("/api/accounts", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTHENTICATION_ERROR"


async def test_unauthenticated_mutation_never_reaches_handler(client, sessionmaker):
    """A 401 on a create route means the handler never ran: no token is created."""
    resp = client.post("/api/tokens", json={"label": "should-not-exist"})
    assert resp.status_code == 401

    async with sessionmaker() as session:
        assert len(await list_tokens(session)) == 0


# --- 2. An authenticated ADMIN can reach every route group --------------------


@pytest.mark.parametrize("path", _MANAGE_ONLY_READS + ["/api/traces"])
def test_admin_can_read_every_group(client, auth_header, path):
    """An ADMIN holds every permission and reaches accounts, tokens, chains, traces."""
    resp = client.get(path, headers=auth_header("admin"))
    assert resp.status_code == 200, (path, resp.text)
    assert isinstance(resp.json(), list)


def test_admin_can_read_analytics(client, auth_header):
    """An ADMIN can read the analytics group (which requires query params)."""
    resp = client.get(
        "/api/analytics/system", params=_RANGE, headers=auth_header("admin")
    )
    assert resp.status_code == 200, resp.text


def test_admin_can_read_model_catalog(client, auth_header):
    """An ADMIN can read the grouped model catalog."""
    resp = client.get("/api/models", headers=auth_header("admin"))
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), dict)


def test_admin_can_update_provider_model_catalog(client, auth_header):
    """An ADMIN can update runtime provider fallback models."""
    resp = client.put(
        "/api/models/providers/codex",
        json={"models": ["gpt-5.5"]},
        headers=auth_header("admin"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["source"] == "runtime"


def test_admin_can_perform_manage_mutation(client, auth_header):
    """An ADMIN can perform a manage-only mutation (create a Client_Token)."""
    resp = client.post(
        "/api/tokens", json={"label": "admin-made"}, headers=auth_header("admin")
    )
    assert resp.status_code == 201, resp.text


# --- 3. A VIEWER is denied on the manage-only groups (403) --------------------


@pytest.mark.parametrize("path", _MANAGE_ONLY_READS)
def test_viewer_denied_on_manage_only_reads(client, auth_header, path):
    """A VIEWER lacks MANAGE_* and is denied (403) on accounts/tokens/chains reads."""
    resp = client.get(path, headers=auth_header("viewer"))
    assert resp.status_code == 403, (path, resp.text)
    assert resp.json()["error"]["code"] == "PERMISSION_DENIED"


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/accounts/connect/api-key", {"provider": "openai", "api_key": "x"}),
        ("/api/tokens", {"label": "x"}),
        (
            "/api/tokens/00000000-0000-4000-8000-000000000001/reveal",
            {"password": "x"},
        ),
        ("/api/chains", {"name": "x", "account_ids": []}),
        ("/api/models/providers/codex", {"models": ["gpt-5.5"]}),
    ],
)
def test_viewer_denied_on_manage_only_mutations(client, auth_header, path, body):
    """A VIEWER cannot perform privileged manage-only mutations (403)."""
    method = "put" if path.startswith("/api/models/providers/") else "post"
    resp = getattr(client, method)(path, json=body, headers=auth_header("viewer"))
    assert resp.status_code == 403, (path, resp.text)
    assert resp.json()["error"]["code"] == "PERMISSION_DENIED"


# --- 4. A VIEWER can read the groups it is explicitly granted -----------------


def test_viewer_can_read_traces(client, auth_header):
    """A VIEWER is granted VIEW_TRACES and can list traces."""
    resp = client.get("/api/traces", headers=auth_header("viewer"))
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)


def test_viewer_can_read_analytics(client, auth_header):
    """A VIEWER is granted VIEW_ANALYTICS and can read analytics."""
    resp = client.get(
        "/api/analytics/system", params=_RANGE, headers=auth_header("viewer")
    )
    assert resp.status_code == 200, resp.text


# --- 5. List endpoints never leak secret material (Requirement 16.4) ----------


def test_tokens_list_leaks_no_secret(client, auth_header, seeded):
    """The tokens list carries no secret: neither the value nor a ``secret`` field."""
    resp = client.get("/api/tokens", headers=auth_header("admin"))
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert len(items) == 1
    # The issued secret must never appear in a read-back listing.
    assert seeded["token_secret"] not in resp.text
    assert all("secret" not in item for item in items)


def test_accounts_list_leaks_no_secret(client, auth_header, seeded):
    """The accounts list carries no API key, ciphertext, nonce, or wrapped DEK."""
    resp = client.get("/api/accounts", headers=auth_header("admin"))
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert len(items) == 1
    assert items[0]["account_id"] == str(seeded["account_id"])
    body = resp.text
    # The real secret value and the at-rest encryption fields must never appear.
    # ("api_key" is intentionally NOT checked as a substring: it is the legitimate
    # non-secret credential *kind* value, e.g. "kind":"api_key".)
    assert _ACCOUNT_API_KEY not in body
    for forbidden in ("ciphertext", "nonce", "wrapped_dek"):
        assert forbidden not in body, forbidden


# --- positive control: creation returns the issued secret ---------------------


def test_token_creation_returns_issued_secret(client, auth_header):
    """The create response returns the issued token secret (Req 8.1).

    This anchors the no-leak tests: the secret is present at creation, proving its
    absence from the list endpoint is a real filter, not an empty-dataset artefact.
    """
    resp = client.post(
        "/api/tokens", json={"label": "once"}, headers=auth_header("admin")
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["secret"].startswith("gz-")
