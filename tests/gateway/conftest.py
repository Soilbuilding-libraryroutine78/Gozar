"""Shared fixtures for Proxy_Gateway pipeline tests.

These tests exercise the non-streaming ``/v1/chat/completions`` pipeline against an
in-memory SQLite database (the project's test convention; the ORM models already
declare SQLite variants) and an in-memory Redis fake, mocking the upstream provider
call. No test touches the network or a real database/Redis.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from gozar.core.config import Settings

# Import every ORM model module so all tables register on Base.metadata before
# create_all (the pipeline touches accounts, tokens, routing, and usage tables).
from gozar.accounts import models as _accounts_models  # noqa: F401
from gozar.core import models as _core_models  # noqa: F401
from gozar.routing import models as _routing_models  # noqa: F401
from gozar.tokens import models as _tokens_models  # noqa: F401
from gozar.usage import models as _usage_models  # noqa: F401
from gozar.core.db import Base

# A deterministic, well-formed 32-byte master key (base64) for envelope encryption.
_TEST_MASTER_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()


@pytest.fixture
def settings() -> Settings:
    """Test settings with the secret material and provider URLs the pipeline needs."""
    return Settings(
        master_key=_TEST_MASTER_KEY,
        token_pepper="test-pepper",
        jwt_secret="test-jwt-secret",
        redis_url="redis://localhost:6379/0",
        provider_base_urls={
            "openai": "https://api.openai.com/v1",
            "openrouter": "https://openrouter.ai/api/v1",
        },
    )


@pytest_asyncio.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """An in-memory SQLite async session factory with all tables created."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """A single async session for direct-call pipeline tests."""
    async with sessionmaker() as session:
        yield session
        await session.commit()


class FakePipeline:
    """Minimal in-memory stand-in for a redis.asyncio transactional pipeline."""

    def __init__(self, store: "FakeRedis") -> None:
        self._store = store
        self._ops: list[tuple] = []

    def incrby(self, key: str, amount: int) -> "FakePipeline":
        self._ops.append(("incrby", key, amount))
        return self

    def expire(self, key: str, ttl: int) -> "FakePipeline":
        self._ops.append(("expire", key, ttl))
        return self

    async def execute(self) -> list:
        results: list = []
        for op in self._ops:
            if op[0] == "incrby":
                _, key, amount = op
                current = int(self._store._data.get(key, "0"))
                current += amount
                self._store._data[key] = str(current)
                results.append(current)
            else:  # expire is a no-op for the fake
                results.append(True)
        self._ops.clear()
        return results


class FakeRedis:
    """Minimal in-memory async Redis fake covering the calls the pipeline makes.

    Implements ``get``/``set``/``delete``/``pipeline`` with string values, mirroring
    ``redis.asyncio`` semantics closely enough for the consumption counters and the
    session-affinity map used by the gateway pipeline.
    """

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def set(self, key: str, value, ex: int | None = None) -> None:
        self._data[key] = str(value)

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self._data.pop(key, None)

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)


@pytest.fixture
def redis() -> FakeRedis:
    """A fresh in-memory Redis fake per test."""
    return FakeRedis()


# Canonical, well-formed OpenAI chat completion response the fake upstream returns.
def openai_response(content: str = "hello there", model: str = "gpt-4o") -> dict:
    """Build a valid OpenAI Chat Completions response body (with usage)."""
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
        },
    }


def material_for(account_id: uuid.UUID, provider: str = "openai"):
    """Build :class:`ProviderCredentialMaterial` for an API-key account (no DB I/O)."""
    from gozar.accounts.models import CredentialKind
    from gozar.accounts.service import ProviderCredentialMaterial

    return ProviderCredentialMaterial(
        account_id=account_id,
        provider=provider,
        kind=CredentialKind.API_KEY,
        access_token=None,
        api_key="sk-test-key",
        provider_account_ref=None,
        expires_at=None,
    )
