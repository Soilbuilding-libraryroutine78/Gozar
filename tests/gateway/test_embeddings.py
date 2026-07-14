"""Capability-aware routing tests for ``POST /v1/embeddings``."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from sqlalchemy import select

from gozar.accounts.models import (
    CredentialKind,
    CredentialStatus,
    UpstreamCredential,
)
from gozar.core.errors import NoAvailableAccount, UpstreamError
from gozar.gateway.embeddings import complete_embedding
from gozar.routing.chains import RouteKind
from gozar.routing.service import ChainEntryInput, create_chain
from gozar.tokens.service import create_token
from gozar.translation.types import OpenAIEmbeddingRequest
from gozar.usage.models import TraceLog, UsageRecord

from conftest import material_for


def _request() -> OpenAIEmbeddingRequest:
    return OpenAIEmbeddingRequest.model_validate(
        {
            "model": "text-embedding-3-small",
            "input": ["first", "second"],
            "encoding_format": "float",
        }
    )


async def _seed_route(session, settings, providers: Sequence[str]):
    accounts: list[UpstreamCredential] = []
    for index, provider in enumerate(providers):
        kind = (
            CredentialKind.SUBSCRIPTION
            if provider in {"codex", "anthropic"}
            else CredentialKind.API_KEY
        )
        account = UpstreamCredential(
            provider=provider,
            kind=kind,
            label=f"{provider}-{index}",
            status=CredentialStatus.ACTIVE,
        )
        session.add(account)
        accounts.append(account)
    await session.flush()
    chain = await create_chain(
        session,
        "embedding-route",
        [
            ChainEntryInput(account.id, route_kind=RouteKind.EMBEDDINGS)
            for account in accounts
        ],
    )
    issued = await create_token(
        session,
        "embedding-key",
        None,
        assigned_chain_id=chain.chain_id,
        settings=settings,
    )
    return issued, accounts


def _response(model: str = "text-embedding-3-small") -> dict:
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": [0.1, -0.2], "index": 0},
            {"object": "embedding", "embedding": [0.3, -0.4], "index": 1},
        ],
        "model": model,
        "usage": {"prompt_tokens": 6, "total_tokens": 6},
    }


@pytest.mark.asyncio
async def test_skips_unsupported_subscription_provider_and_uses_api_key_route(
    session, redis, settings
):
    issued, accounts = await _seed_route(session, settings, ["codex", "openai"])
    acquired: list = []

    async def acquire(_session, account_id):
        acquired.append(account_id)
        if account_id == accounts[0].id:
            raise AssertionError("Codex must be skipped for embeddings")
        return material_for(account_id, "openai")

    async def upstream(entry, material, request):
        assert entry.provider_id.value == "openai"
        assert request.model == "text-embedding-3-small"
        return _response(request.model)

    response = await complete_embedding(
        session,
        presented_token=issued.secret,
        request=_request(),
        redis=redis,
        settings=settings,
        acquire_material=acquire,
        upstream=upstream,
    )

    assert response.data[0].embedding == [0.1, -0.2]
    assert acquired == [accounts[1].id]


@pytest.mark.asyncio
async def test_falls_back_between_embedding_capable_providers(
    session, redis, settings
):
    issued, accounts = await _seed_route(
        session,
        settings,
        ["openai", "openrouter"],
    )
    calls: list[str] = []

    async def acquire(_session, account_id):
        provider = "openai" if account_id == accounts[0].id else "openrouter"
        return material_for(account_id, provider)

    async def upstream(entry, material, request):
        calls.append(entry.provider_id.value)
        assert request.model == "text-embedding-3-small"
        if entry.provider_id.value == "openai":
            raise UpstreamError(
                "upstream provider 'openai' returned status 503",
                details=[{"upstream_status": 503}],
            )
        return _response(request.model)

    response = await complete_embedding(
        session,
        presented_token=issued.secret,
        request=_request(),
        redis=redis,
        settings=settings,
        acquire_material=acquire,
        upstream=upstream,
    )

    assert response.model == "text-embedding-3-small"
    assert calls == ["openai", "openrouter"]

    usage = (await session.scalars(select(UsageRecord))).one()
    assert usage.account_id == accounts[1].id
    assert usage.prompt_tokens == 6
    assert usage.completion_tokens == 0
    assert usage.total_tokens == 6

    trace = (await session.scalars(select(TraceLog))).one()
    assert trace.inbound_meta["endpoint"] == "embeddings"
    attempts = trace.outbound_meta["routing"]["attempts"]
    assert [attempt["outcome"] for attempt in attempts] == ["error", "success"]
    assert attempts[0]["fallback_taken"] is True


@pytest.mark.asyncio
async def test_embedding_lane_applies_each_provider_model(
    session, redis, settings
):
    accounts: list[UpstreamCredential] = []
    for provider in ("openai", "openrouter"):
        account = UpstreamCredential(
            provider=provider,
            kind=CredentialKind.API_KEY,
            label=provider,
            status=CredentialStatus.ACTIVE,
        )
        session.add(account)
        accounts.append(account)
    await session.flush()
    chain = await create_chain(
        session,
        "provider-models",
        [
            ChainEntryInput(
                accounts[0].id,
                "text-embedding-3-small",
                route_kind=RouteKind.EMBEDDINGS,
            ),
            ChainEntryInput(
                accounts[1].id,
                "openai/text-embedding-3-small",
                route_kind=RouteKind.EMBEDDINGS,
            ),
        ],
    )
    issued = await create_token(
        session,
        "embedding-key",
        None,
        assigned_chain_id=chain.chain_id,
        settings=settings,
    )
    calls: list[tuple[str, str]] = []

    async def acquire(_session, account_id):
        provider = "openai" if account_id == accounts[0].id else "openrouter"
        return material_for(account_id, provider)

    async def upstream(entry, material, request):
        calls.append((entry.provider_id.value, request.model))
        if entry.provider_id.value == "openai":
            raise UpstreamError(
                "upstream provider 'openai' returned status 503",
                details=[{"upstream_status": 503}],
            )
        return _response(request.model)

    response = await complete_embedding(
        session,
        presented_token=issued.secret,
        request=_request(),
        redis=redis,
        settings=settings,
        acquire_material=acquire,
        upstream=upstream,
    )

    assert response.model == "openai/text-embedding-3-small"
    assert calls == [
        ("openai", "text-embedding-3-small"),
        ("openrouter", "openai/text-embedding-3-small"),
    ]


@pytest.mark.asyncio
async def test_auto_routing_skips_chains_without_an_embedding_lane(
    session, redis, settings
):
    codex = UpstreamCredential(
        provider="codex",
        kind=CredentialKind.SUBSCRIPTION,
        label="chat-only",
        status=CredentialStatus.ACTIVE,
    )
    openai = UpstreamCredential(
        provider="openai",
        kind=CredentialKind.API_KEY,
        label="embeddings",
        status=CredentialStatus.ACTIVE,
    )
    session.add_all([codex, openai])
    await session.flush()
    await create_chain(session, "chat-default", [codex.id])
    embedding_chain = await create_chain(
        session,
        "embedding-default",
        [ChainEntryInput(openai.id, route_kind=RouteKind.EMBEDDINGS)],
    )
    issued = await create_token(session, "auto-route", None, settings=settings)

    async def acquire(_session, account_id):
        assert account_id == openai.id
        return material_for(account_id, "openai")

    async def upstream(_entry, _material, request):
        return _response(request.model)

    response = await complete_embedding(
        session,
        presented_token=issued.secret,
        request=_request(),
        redis=redis,
        settings=settings,
        acquire_material=acquire,
        upstream=upstream,
    )

    assert response.model == "text-embedding-3-small"
    trace = (await session.scalars(select(TraceLog))).one()
    assert trace.inbound_meta["chain_id"] == str(embedding_chain.chain_id)
    assert trace.outbound_meta["routing"]["route"] == RouteKind.EMBEDDINGS.value


@pytest.mark.asyncio
async def test_fails_closed_when_chain_has_no_embedding_capable_account(
    session, redis, settings
):
    issued, _accounts = await _seed_route(session, settings, ["codex", "anthropic"])

    with pytest.raises(NoAvailableAccount, match="OpenAI or OpenRouter"):
        await complete_embedding(
            session,
            presented_token=issued.secret,
            request=_request(),
            redis=redis,
            settings=settings,
            acquire_material=lambda *_args: pytest.fail(
                "unsupported accounts must not be acquired"
            ),
            upstream=lambda *_args: pytest.fail(
                "unsupported accounts must not be called"
            ),
        )

    trace = (await session.scalars(select(TraceLog))).one()
    assert trace.outcome == "no_account"
    assert trace.status_code == 503


def test_private_routing_extension_is_excluded_from_provider_body():
    request = OpenAIEmbeddingRequest.model_validate(
        {
            "model": "text-embedding-3-small",
            "input": "hello",
            "gozar": {
                "chain_id": "11111111-1111-4111-8111-111111111111",
                "include_metadata": True,
            },
        }
    )

    assert "gozar" not in request.model_dump(mode="json")
