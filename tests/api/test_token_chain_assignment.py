"""Admin API tests for token-to-chain routing assignment."""

from __future__ import annotations

import uuid


def test_token_create_list_and_reassign_chain(client, auth_header):
    """A token can be pinned to a chain, listed with that chain, then reset to auto."""
    headers = auth_header("admin")

    chain_resp = client.post(
        "/api/chains",
        json={"name": "Production routing", "account_ids": []},
        headers=headers,
    )
    assert chain_resp.status_code == 201, chain_resp.text
    chain_id = chain_resp.json()["chain_id"]

    create_resp = client.post(
        "/api/tokens",
        json={"label": "worker", "assigned_chain_id": chain_id},
        headers=headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    created = create_resp.json()
    assert created["assigned_chain_id"] == chain_id

    list_resp = client.get("/api/tokens", headers=headers)
    assert list_resp.status_code == 200, list_resp.text
    token = list_resp.json()[0]
    assert token["token_id"] == created["token_id"]
    assert token["assigned_chain_id"] == chain_id
    assert token["assigned_chain_name"] == "Production routing"
    assert "secret" not in token

    clear_resp = client.patch(
        f"/api/tokens/{created['token_id']}/chain",
        json={"assigned_chain_id": None},
        headers=headers,
    )
    assert clear_resp.status_code == 204, clear_resp.text

    list_after_clear = client.get("/api/tokens", headers=headers)
    assert list_after_clear.status_code == 200, list_after_clear.text
    assert list_after_clear.json()[0]["assigned_chain_id"] is None
    assert list_after_clear.json()[0]["assigned_chain_name"] is None


def test_token_chain_assignment_rejects_unknown_chain(client, auth_header):
    """The API validates chain ids instead of storing dangling route references."""
    headers = auth_header("admin")
    create_resp = client.post("/api/tokens", json={"label": "worker"}, headers=headers)
    assert create_resp.status_code == 201, create_resp.text

    resp = client.patch(
        f"/api/tokens/{create_resp.json()['token_id']}/chain",
        json={"assigned_chain_id": str(uuid.uuid4())},
        headers=headers,
    )

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"
