"""Admin API coverage for stable, provider-aware chain resources."""

from __future__ import annotations

import asyncio

from gozar.accounts.models import CredentialKind, CredentialStatus, UpstreamCredential


async def _seed_accounts(sessionmaker) -> tuple[str, str]:
    async with sessionmaker() as session:
        primary = UpstreamCredential(
            provider="openai",
            kind=CredentialKind.API_KEY,
            label="OpenAI primary",
            status=CredentialStatus.ACTIVE,
        )
        fallback = UpstreamCredential(
            provider="openrouter",
            kind=CredentialKind.API_KEY,
            label="OpenRouter fallback",
            status=CredentialStatus.ACTIVE,
        )
        session.add_all([primary, fallback])
        await session.flush()
        ids = str(primary.id), str(fallback.id)
        await session.commit()
        return ids


def test_upsert_chain_by_key_reuses_id_and_updates_nodes(
    client, sessionmaker, auth_header
):
    primary_id, fallback_id = asyncio.run(_seed_accounts(sessionmaker))
    url = "/api/chains/by-key/support-production"
    first = client.put(
        url,
        headers=auth_header("admin"),
        json={
            "name": "Support",
            "entries": [
                {
                    "account_id": primary_id,
                    "model": "gpt-5.4-mini",
                    "fallback_policy": "auth_or_retryable",
                },
                {
                    "account_id": fallback_id,
                    "model": "google/gemini-2.5-flash",
                },
            ],
        },
    )
    assert first.status_code == 200, first.text

    second = client.put(
        url,
        headers=auth_header("admin"),
        json={
            "name": "Support v2",
            "entries": [
                {
                    "account_id": fallback_id,
                    "model": "anthropic/claude-sonnet-4",
                    "fallback_policy": "retryable",
                }
            ],
        },
    )
    assert second.status_code == 200, second.text

    assert second.json()["chain_id"] == first.json()["chain_id"]
    assert second.json()["client_key"] == "support-production"
    assert second.json()["name"] == "Support v2"
    assert second.json()["entries"] == [
        {
            "account_id": fallback_id,
            "position": 0,
            "model": "anthropic/claude-sonnet-4",
            "fallback_policy": "retryable",
        }
    ]


def test_chain_upsert_requires_manage_permission(client, auth_header):
    response = client.put(
        "/api/chains/by-key/restricted",
        headers=auth_header("viewer"),
        json={"name": "Restricted", "entries": []},
    )

    assert response.status_code == 403
