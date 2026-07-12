"""Alembic environment (async).

The database URL is read from :mod:`gozar.core.config` (``GOZAR_DATABASE_URL``) -
never from ``alembic.ini`` - so there are no hardcoded URLs or credentials. The
target metadata is :data:`gozar.core.db.Base.metadata`.

Registering models for migrations
----------------------------------
For Alembic autogenerate to see a table, the module that declares it MUST be
imported below so its mapper registers on ``Base.metadata``. As later tasks add
models (e.g. ``gozar.auth.models``, ``gozar.accounts.models``), add the matching
import to the "model modules" block. The baseline migration is empty; each module
contributes its own migration.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from gozar.core.config import get_settings
from gozar.core.db import Base

# --- model modules (import so their tables register on Base.metadata) ---------
# Later tasks add imports here, for example:
#   import gozar.auth.models  # noqa: F401
#   import gozar.accounts.models  # noqa: F401
# ------------------------------------------------------------------------------
import gozar.auth.models  # noqa: F401  (registers auth_operator on Base.metadata)
import gozar.core.models  # noqa: F401  (registers core_* runtime config tables)
import gozar.tokens.models  # noqa: F401  (registers tok_client_token, tok_usage_limit)
import gozar.accounts.models  # noqa: F401  (registers acct_* tables on Base.metadata)
import gozar.routing.models  # noqa: F401  (registers route_fallback_chain[_entry])
import gozar.usage.models  # noqa: F401  (registers usage_record, trace_log)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Return the configured database URL or fail closed."""
    url = get_settings().database_url
    if not url:
        raise RuntimeError(
            "GOZAR_DATABASE_URL is not configured; cannot run migrations."
        )
    return url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL, no live connection)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    """Create an async engine from settings and run migrations against it."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode using the async engine."""
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
