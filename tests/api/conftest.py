"""Shared fixtures for the admin control-path (``/api``) access-control tests.

These tests drive the real FastAPI app from :func:`gozar.app.create_app` with
``TestClient`` to verify the fail-closed RBAC gate (:func:`gozar.auth.rbac.require`)
on every admin router. They run against an in-memory SQLite database (the project's
test convention) and never touch the network or a real database/Redis.

The admin routers bake ``require(<permission>)`` with ``settings=None`` at import
time, so the guard verifies session tokens against the *process* settings via
:func:`gozar.auth.session.get_settings`. The :func:`client` fixture therefore pins
that function to the test settings so issued operator tokens verify deterministically,
and overrides :func:`gozar.core.db.get_session` to use the in-memory database.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from gozar.auth import session as session_module
from gozar.auth.session import issue_session_tokens
from gozar.tokens import service as tokens_service
from gozar.app import create_app
from gozar.core.config import Settings
from gozar.core.db import get_session

# Import every ORM model module so all tables register on Base.metadata before
# create_all (the admin routers touch accounts, tokens, routing, and usage tables).
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
    """Test settings with the JWT secret and provider URLs the admin API needs."""
    return Settings(
        master_key=_TEST_MASTER_KEY,
        token_pepper="test-pepper",
        jwt_secret="api-access-control-test-jwt-secret",
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


@pytest.fixture
def client(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    monkeypatch,
) -> AsyncIterator[TestClient]:
    """A TestClient for the real app with the DB session and JWT settings pinned."""
    app = create_app(settings=settings)

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_session] = _override_session

    # The admin guards verify tokens against the process settings (settings=None at
    # import time); pin those to the test settings so issued tokens verify. The token
    # routes likewise create tokens with the process settings (the HMAC pepper), so
    # pin the Token_Authority's settings accessor too.
    monkeypatch.setattr(session_module, "get_settings", lambda: settings)
    monkeypatch.setattr(tokens_service, "get_settings", lambda: settings)

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_header(settings: Settings):
    """Factory fixture: build an ``Authorization`` header for a given operator role.

    Returns a callable ``auth_header(role) -> {"Authorization": "Bearer <jwt>"}`` so
    tests can mint a valid access token for ``"admin"`` or ``"viewer"`` on demand.
    """

    def _make(role: str) -> dict[str, str]:
        token = issue_session_tokens(
            uuid.uuid4(), role, settings=settings
        ).access_token
        return {"Authorization": f"Bearer {token}"}

    return _make
