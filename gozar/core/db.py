"""Async database layer.

Provides the SQLAlchemy 2 async engine, session factory, declarative base, and a
FastAPI-friendly session dependency. The database URL is always read from
:mod:`gozar.core.config` (``GOZAR_DATABASE_URL``); there are no hardcoded URLs.

Module-prefixed table naming convention
---------------------------------------
Every table in Gozar is owned by exactly one domain module and is named with that
module's prefix so ownership is obvious in the schema and modules can be added
without colliding (steering 6.1). Later modules MUST follow this convention when
declaring their models:

==========  ==========================  =====================================
Prefix      Owning module               Example tables
==========  ==========================  =====================================
``core_``   ``gozar.core``              shared/system tables
``acct_``   ``gozar.accounts``          ``acct_upstream_credential`` ...
``tok_``    ``gozar.tokens``            ``tok_client_token`` ...
``route_``  ``gozar.routing``           ``route_fallback_chain`` ...
``usage_``  ``gozar.usage`` (metering)  ``usage_record`` ...
``trace_``  ``gozar.usage`` (tracing)   ``trace_log`` ...
``auth_``   ``gozar.auth``              ``auth_operator`` ...
==========  ==========================  =====================================

All ORM models MUST inherit from :class:`Base` and set ``__tablename__`` using the
prefix of their owning module. Model modules MUST be imported in the Alembic
``env.py`` so their tables register on :data:`Base.metadata` for migrations.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from gozar.core.config import Settings, get_settings

# Deterministic constraint/index names so Alembic autogenerate produces stable,
# reviewable migrations across environments.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Module-prefix registry. Later modules should declare table names beginning with
# one of these prefixes (see the module docstring for the convention).
TABLE_PREFIXES: tuple[str, ...] = (
    "core_",
    "acct_",
    "tok_",
    "route_",
    "usage_",
    "trace_",
    "auth_",
)

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    """Declarative base shared by every Gozar ORM model.

    Carries the project-wide :data:`metadata` (with the naming convention) so all
    models and migrations share a single metadata object.
    """

    metadata = metadata


def build_engine(settings: Settings) -> AsyncEngine:
    """Create an async engine from the given settings.

    Raises ``RuntimeError`` (fail closed) when the database URL is not configured,
    rather than silently constructing an unusable engine.
    """
    if not settings.database_url:
        raise RuntimeError(
            "GOZAR_DATABASE_URL is not configured; the database layer cannot start."
        )
    return create_async_engine(
        settings.database_url,
        echo=False,
        pool_pre_ping=True,
        future=True,
    )


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create an async session factory bound to ``engine``."""
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


@lru_cache
def get_engine() -> AsyncEngine:
    """Return the process-wide async engine (lazily created, cached)."""
    return build_engine(get_settings())


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide async session factory (lazily created, cached)."""
    return build_sessionmaker(get_engine())


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a transactional async session.

    Usage::

        @router.get("/things")
        async def list_things(session: AsyncSession = Depends(get_session)):
            ...

    The session is committed on success and rolled back if the handler raises, then
    always closed.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Dispose of the cached engine and clear the cached factories.

    Intended for application shutdown and test teardown so connection pools are
    released cleanly.
    """
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
    get_sessionmaker.cache_clear()
    get_engine.cache_clear()
