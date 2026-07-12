"""Admin API tests for password-confirmed Gozar API key rotation."""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

from gozar.auth.models import Operator
from gozar.auth.rbac import Role
from gozar.auth.service import hash_password
from gozar.auth.session import issue_session_tokens
from gozar.tokens import service as tokens_service
from gozar.tokens.models import ClientToken, TokenStatus, TokenUsageLimit
from gozar.usage.limits import LimitMetric, LimitWindow


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


async def _verify_secret(sessionmaker, secret, settings):
    async with sessionmaker() as session:
        return await tokens_service.verify(session, secret, settings=settings)


async def _token_status(sessionmaker, token_id: str) -> str:
    async with sessionmaker() as session:
        token = await session.get(ClientToken, uuid.UUID(token_id))
        assert token is not None
        return token.status


async def _token_limit_count(sessionmaker, token_id: str) -> int:
    async with sessionmaker() as session:
        rows = (
            await session.scalars(
                select(TokenUsageLimit).where(
                    TokenUsageLimit.subject_id == uuid.UUID(token_id)
                )
            )
        ).all()
        return len(rows)


def test_rotate_token_requires_password_and_returns_replacement_once(
    client,
    sessionmaker,
    settings,
):
    operator_id = asyncio.run(_seed_operator(sessionmaker))
    headers = {
        "Authorization": (
            "Bearer "
            + issue_session_tokens(
                operator_id,
                Role.ADMIN.value,
                settings=settings,
            ).access_token
        )
    }

    created_resp = client.post(
        "/api/tokens",
        json={
            "label": "Worker",
            "limit": {
                "metric": LimitMetric.REQUEST_COUNT.value,
                "limit_value": 100,
                "capacity": None,
                "window": LimitWindow.MONTHLY.value,
            },
        },
        headers=headers,
    )
    assert created_resp.status_code == 201, created_resp.text
    created = created_resp.json()

    wrong_password_resp = client.post(
        f"/api/tokens/{created['token_id']}/rotate",
        json={"password": "wrong-password"},
        headers=headers,
    )
    assert wrong_password_resp.status_code == 401, wrong_password_resp.text
    still_valid = asyncio.run(_verify_secret(sessionmaker, created["secret"], settings))
    assert still_valid is not None

    rotated_resp = client.post(
        f"/api/tokens/{created['token_id']}/rotate",
        json={"password": _PASSWORD},
        headers=headers,
    )
    assert rotated_resp.status_code == 201, rotated_resp.text
    rotated = rotated_resp.json()

    assert rotated["token_id"] != created["token_id"]
    assert rotated["label"] == "Worker"
    assert rotated["secret"].startswith(f"gz-{rotated['id_prefix']}-")
    assert "..." not in rotated["secret"]

    old_status = asyncio.run(_token_status(sessionmaker, created["token_id"]))
    assert old_status == TokenStatus.REVOKED.value
    assert asyncio.run(_verify_secret(sessionmaker, created["secret"], settings)) is None
    replacement_valid = asyncio.run(
        _verify_secret(sessionmaker, rotated["secret"], settings)
    )
    assert replacement_valid is not None
    assert asyncio.run(_token_limit_count(sessionmaker, rotated["token_id"])) == 1
