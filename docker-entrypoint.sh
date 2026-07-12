#!/bin/sh
# Gozar backend container entrypoint.
#
# Makes a self-hosted deployment self-migrating: before the API server starts it
# brings the database schema up to head with Alembic, so an operator only has to
# run `docker compose up` -- there is no separate migration step to remember.
#
# Migrations run only when a database URL is configured (GOZAR_DATABASE_URL); the
# application itself fails closed at startup if required configuration is missing,
# so this script does not duplicate that validation. `alembic upgrade head` is
# idempotent: on an already-migrated database it is a no-op.
set -eu

if [ -n "${GOZAR_DATABASE_URL:-}" ]; then
    echo "[entrypoint] applying database migrations (alembic upgrade head)..."
    alembic upgrade head
    echo "[entrypoint] migrations are up to date."
else
    echo "[entrypoint] GOZAR_DATABASE_URL not set; skipping migrations."
fi

# Hand off to the image command (uvicorn by default) as PID 1.
exec "$@"
