"""FastAPI application assembly.

Builds the Gozar ASGI app and exposes the operational probes:

* ``GET /health`` - liveness: the process is up and able to answer. Always 200
  while the process is healthy; does not depend on downstream configuration.
* ``GET /ready``  - readiness: the service is fully configured and able to serve
  traffic. Returns 503 (fail closed) while required configuration is missing.

Domain routers (proxy data-path under ``/v1`` and admin control-path under
``/api``, including the public ``/api/auth`` login/refresh/bootstrap routes) are
wired in here. On startup the app fails closed via
:func:`validate_startup_config` if required configuration - notably the
secret-encryption master key - is missing or invalid (Requirement 19.3).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from gozar import __version__
from gozar.api import api_router
from gozar.core.config import Settings, get_settings
from gozar.core.crypto import MasterKeyError, ensure_master_key
from gozar.core.errors import ConfigError, register_exception_handlers
from gozar.gateway.router import router as gateway_router


def validate_startup_config(settings: Settings) -> None:
    """Validate required configuration at startup, failing closed if it is missing.

    Gozar reads its secret-encryption material and other required configuration from
    Operator-controlled configuration at startup (Requirement 19.3) and must refuse
    to start in a degraded state:

    * The **master key** must be present and well-formed (a base64-encoded 32-byte
      value); without it no credential material can be encrypted or decrypted, so a
      missing or invalid key is fatal in every environment.
    * The **session-signing secret** (``GOZAR_JWT_SECRET``) and the **client-token
      pepper** (``GOZAR_TOKEN_PEPPER``) must be present; operator sessions and client
      tokens cannot be issued or verified without them.
    * In **production** every runtime requirement is mandatory, including the database
      and Redis URLs, so a production instance never starts half-configured.

    Raises :class:`gozar.core.errors.ConfigError` (a clear, secret-free message
    listing only the names of what is missing) when validation fails.
    """
    try:
        ensure_master_key(settings)
    except MasterKeyError as exc:
        raise ConfigError(f"refusing to start: {exc}") from exc

    if settings.is_production:
        # In production the full set of runtime requirements is mandatory.
        missing = settings.missing_runtime_requirements()
    else:
        # Outside production the encryption/signing/token secrets are still required;
        # the database and Redis URLs are validated lazily on first use.
        missing = [
            name
            for name, value in {
                "GOZAR_JWT_SECRET": settings.jwt_secret,
                "GOZAR_TOKEN_PEPPER": settings.token_pepper,
            }.items()
            if not value
        ]

    if missing:
        raise ConfigError(
            "refusing to start: missing required configuration: "
            + ", ".join(sorted(set(missing)))
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Construct and return the Gozar FastAPI application.

    Accepts an optional ``settings`` override to make the assembly testable
    without mutating the process environment.
    """
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Fail closed before serving any traffic if required configuration (notably
        # the secret-encryption master key) is missing or invalid (Requirement 19.3).
        validate_startup_config(app.state.settings)
        yield

    app = FastAPI(
        title="Gozar",
        version=__version__,
        lifespan=lifespan,
        description=(
            "Self-hosted, OpenAI-compatible LLM proxy that routes traffic through "
            "your own subscription accounts and API keys."
        ),
    )
    app.state.settings = settings

    @app.get("/health", tags=["ops"], summary="Liveness probe")
    async def health() -> dict:
        """Report that the process is alive."""
        return {"status": "ok", "version": __version__}

    @app.get("/ready", tags=["ops"], summary="Readiness probe")
    async def ready() -> JSONResponse:
        """Report whether the service is configured to serve traffic.

        Fails closed with HTTP 503 while any required configuration is absent,
        listing the missing variable names (never their values).
        """
        missing = app.state.settings.missing_runtime_requirements()
        if missing:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "missing_configuration": missing},
            )
        return JSONResponse(status_code=200, content={"status": "ready"})

    # Render typed domain errors as the correct envelope per surface (OpenAI-style
    # under /v1, admin envelope elsewhere).
    register_exception_handlers(app)

    # Proxy data-path (OpenAI-compatible) under /v1. Admin control-path routers are
    # mounted under /api; every admin route is fail-closed authenticated.
    app.include_router(gateway_router)
    app.include_router(api_router)

    return app


# Module-level ASGI application for servers (e.g. `uvicorn gozar.app:app`).
app = create_app()
