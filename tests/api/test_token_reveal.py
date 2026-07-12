"""Admin API tests for password-confirmed Gozar API key reveal."""

from __future__ import annotations

import asyncio
import uuid

from gozar.auth.models import Operator
from gozar.auth.rbac import Role
from gozar.auth.service import hash_password
from gozar.auth.session import issue_session_tokens
from gozar.tokens import service as tokens_service
from gozar.tokens.models import ClientToken, TokenStatus


_PASSWORD = "CorrectHorseBattery1!"


async def _seed_operator(sessionmaker) -> uuid.UUID:
    async with sessionmaker() as session:
        operator = Operator(
            username="admin",
            password_hash=hash_password(_PASSWORD),
            role=Role.ADMIN.value,
        )
        session.add(operator)
        await session.commit()
        return operator.id


async def _verify_secret(sessionmaker, secret: str, settings):
    async with sessionmaker() as session:
        return await tokens_service.verify(session, secret, settings=settings)


async def _token_status(sessionmaker, token_id: str) -> str:
    async with sessionmaker() as session:
        token = await session.get(ClientToken, uuid.UUID(token_id))
        assert token is not None
        return token.status


async def _clear_reveal_secret(sessionmaker, token_id: str) -> None:
    async with sessionmaker() as session:
        token = await session.get(ClientToken, uuid.UUID(token_id))
        assert token is not None
        token.secret_ciphertext = None
        token.secret_nonce = None
        token.secret_wrapped_dek = None
        await session.commit()


def _headers(operator_id: uuid.UUID, settings) -> dict[str, str]:
    return {
        "Authorization": (
            "Bearer "
            + issue_session_tokens(
                operator_id,
                Role.ADMIN.value,
                settings=settings,
            ).access_token
        )
    }


def test_reveal_token_requires_password_and_does_not_rotate_or_revoke(
    client,
    sessionmaker,
    settings,
):
    operator_id = asyncio.run(_seed_operator(sessionmaker))
    headers = _headers(operator_id, settings)

    created_resp = client.post(
        "/api/tokens",
        json={"label": "Worker"},
        headers=headers,
    )
    assert created_resp.status_code == 201, created_resp.text
    created = created_resp.json()

    wrong_password_resp = client.post(
        f"/api/tokens/{created['token_id']}/reveal",
        json={"password": "wrong-password"},
        headers=headers,
    )
    assert wrong_password_resp.status_code == 401, wrong_password_resp.text
    still_valid = asyncio.run(_verify_secret(sessionmaker, created["secret"], settings))
    assert still_valid is not None

    reveal_resp = client.post(
        f"/api/tokens/{created['token_id']}/reveal",
        json={"password": _PASSWORD},
        headers=headers,
    )
    assert reveal_resp.status_code == 200, reveal_resp.text
    revealed = reveal_resp.json()

    assert revealed["token_id"] == created["token_id"]
    assert revealed["id_prefix"] == created["id_prefix"]
    assert revealed["secret"] == created["secret"]

    assert asyncio.run(_token_status(sessionmaker, created["token_id"])) == (
        TokenStatus.ACTIVE.value
    )
    assert asyncio.run(_verify_secret(sessionmaker, created["secret"], settings)) is not None


def test_legacy_token_without_encrypted_secret_is_not_auto_rotated(
    client,
    sessionmaker,
    settings,
):
    operator_id = asyncio.run(_seed_operator(sessionmaker))
    headers = _headers(operator_id, settings)

    created_resp = client.post(
        "/api/tokens",
        json={"label": "Legacy-shaped"},
        headers=headers,
    )
    assert created_resp.status_code == 201, created_resp.text
    created = created_resp.json()
    asyncio.run(_clear_reveal_secret(sessionmaker, created["token_id"]))

    reveal_resp = client.post(
        f"/api/tokens/{created['token_id']}/reveal",
        json={"password": _PASSWORD},
        headers=headers,
    )

    assert reveal_resp.status_code == 400, reveal_resp.text
    assert "paste the existing API key once" in reveal_resp.text
    assert asyncio.run(_token_status(sessionmaker, created["token_id"])) == (
        TokenStatus.ACTIVE.value
    )
    assert asyncio.run(_verify_secret(sessionmaker, created["secret"], settings)) is not None

    wrong_existing_resp = client.post(
        f"/api/tokens/{created['token_id']}/reveal",
        json={
            "password": _PASSWORD,
            "existing_api_key": "gz-wrongprefix-this-is-not-the-key",
        },
        headers=headers,
    )
    assert wrong_existing_resp.status_code == 400, wrong_existing_resp.text
    assert "does not match" in wrong_existing_resp.text

    recover_resp = client.post(
        f"/api/tokens/{created['token_id']}/reveal",
        json={"password": _PASSWORD, "existing_api_key": created["secret"]},
        headers=headers,
    )
    assert recover_resp.status_code == 200, recover_resp.text
    recovered = recover_resp.json()
    assert recovered["token_id"] == created["token_id"]
    assert recovered["id_prefix"] == created["id_prefix"]
    assert recovered["secret"] == created["secret"]
    assert asyncio.run(_token_status(sessionmaker, created["token_id"])) == (
        TokenStatus.ACTIVE.value
    )
    assert asyncio.run(_verify_secret(sessionmaker, created["secret"], settings)) is not None

    reveal_again_resp = client.post(
        f"/api/tokens/{created['token_id']}/reveal",
        json={"password": _PASSWORD},
        headers=headers,
    )
    assert reveal_again_resp.status_code == 200, reveal_again_resp.text
    assert reveal_again_resp.json()["secret"] == created["secret"]
