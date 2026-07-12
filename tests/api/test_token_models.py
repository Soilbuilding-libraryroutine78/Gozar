"""Admin API tests for token-specific model discovery."""

from __future__ import annotations

import asyncio

from gozar.accounts.models import CredentialKind, CredentialStatus, UpstreamCredential
from gozar.routing.service import create_chain
from gozar.tokens.service import create_token


async def _seed_pinned_token(sessionmaker, settings) -> str:
    async with sessionmaker() as session:
        codex = UpstreamCredential(
            provider="codex",
            kind=CredentialKind.SUBSCRIPTION,
            label="Codex subscription",
            status=CredentialStatus.ACTIVE,
        )
        openai = UpstreamCredential(
            provider="openai",
            kind=CredentialKind.API_KEY,
            label="OpenAI key",
            status=CredentialStatus.ACTIVE,
        )
        session.add_all([codex, openai])
        await session.flush()
        chain = await create_chain(session, "codex-only", [codex.id])
        issued = await create_token(
            session,
            "Worker",
            assigned_chain_id=chain.chain_id,
            settings=settings,
        )
        await session.commit()
        return str(issued.token_id)


def test_token_models_follow_assigned_chain(
    client,
    sessionmaker,
    settings,
    auth_header,
):
    settings.provider_models.update(
        {
            "codex": ["gpt-5.5", "gpt-5.4-mini"],
            "openai": ["gpt-4o"],
        }
    )
    token_id = asyncio.run(_seed_pinned_token(sessionmaker, settings))

    resp = client.get(
        f"/api/tokens/{token_id}/models",
        headers=auth_header("admin"),
    )

    assert resp.status_code == 200, resp.text
    assert [card["id"] for card in resp.json()["data"]] == [
        "gpt-5.5",
        "gpt-5.4-mini",
    ]
