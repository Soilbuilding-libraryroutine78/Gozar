"""Admin route tests that execute a selected API key without revealing its secret."""

from __future__ import annotations

import asyncio
import uuid

from gozar.api import tokens as tokens_router
from gozar.tokens.service import create_token
from gozar.translation.types import OpenAIChatResponse


async def _seed_token(sessionmaker, settings) -> uuid.UUID:
    async with sessionmaker() as session:
        issued = await create_token(session, "Console test", settings=settings)
        await session.commit()
        return issued.token_id


def test_selected_api_key_route_is_tested_without_secret_input(
    client,
    sessionmaker,
    settings,
    auth_header,
    monkeypatch,
):
    token_id = asyncio.run(_seed_token(sessionmaker, settings))
    captured: dict[str, object] = {}

    async def _fake_complete(session, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return OpenAIChatResponse(
            id="chatcmpl-console-test",
            created=1,
            model=kwargs["request"].model,
            choices=[],
        )

    monkeypatch.setattr(tokens_router, "complete_chat_completion", _fake_complete)

    response = client.post(
        f"/api/tokens/{token_id}/test",
        headers=auth_header("admin"),
        json={"model": "gpt-5.4-mini", "prompt": "ping"},
    )

    assert response.status_code == 200, response.text
    assert captured["presented_token"] is None
    assert captured["trusted_token_id"] == token_id
    assert captured["request"].messages[0].content == "ping"


def test_selected_api_key_route_test_requires_manage_permission(
    client, auth_header
):
    response = client.post(
        f"/api/tokens/{uuid.uuid4()}/test",
        headers=auth_header("viewer"),
        json={"model": "gpt-5.4-mini", "prompt": "ping"},
    )

    assert response.status_code == 403
