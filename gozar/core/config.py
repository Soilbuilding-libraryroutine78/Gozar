"""Application configuration.

All settings are read from the environment (or a local ``.env`` file for
development). There are no hardcoded provider URLs, model names, ports, TTLs, or
usage limits anywhere in the codebase: every environment-specific value is
sourced here.

Security-sensitive values (database URL, Redis URL, master encryption key, JWT
secret, client-token pepper) intentionally have no defaults. The application is
allowed to *import* and answer the liveness probe (``GET /health``) without them,
but :meth:`Settings.missing_runtime_requirements` reports what is absent so the
readiness probe (``GET /ready``) can fail closed until configuration is complete.
"""

from __future__ import annotations

import json
from functools import lru_cache
import os

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Which dotenv file (if any) Settings loads, resolved at import time. Defaults to a
# local ``.env`` for developer convenience, but is overridable via ``GOZAR_ENV_FILE``
# and disabled entirely when that variable is empty. The test suite sets it to empty
# (see tests/conftest.py) so a developer's local ``.env`` never leaks into tests and
# the suite stays hermetic.
_ENV_FILE = os.environ.get("GOZAR_ENV_FILE", ".env") or None


class Settings(BaseSettings):
    """Environment-driven application settings.

    Every field maps to a ``GOZAR_``-prefixed environment variable (for example
    ``GOZAR_DATABASE_URL``). Provider base URLs and OAuth metadata are supplied as
    JSON maps so new providers can be added purely through configuration.
    """

    model_config = SettingsConfigDict(
        env_prefix="GOZAR_",
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Runtime / observability -------------------------------------------------
    app_env: str = Field(
        default="development",
        description="Deployment environment: development | staging | production.",
    )
    log_level: str = Field(
        default="info",
        description="Logging verbosity: debug | info | warn | error.",
    )

    # --- Network / ports ---------------------------------------------------------
    http_host: str = Field(
        default="0.0.0.0",
        description="Interface the API server binds to.",
    )
    http_port: int = Field(
        default=8000,
        description="Port the API server listens on.",
    )

    # --- Persistence -------------------------------------------------------------
    database_url: str | None = Field(
        default=None,
        description="Async SQLAlchemy database URL "
        "(e.g. postgresql+asyncpg://user:pass@host:5432/gozar).",
    )
    redis_url: str | None = Field(
        default=None,
        description="Redis URL used for counters, locks, and session affinity.",
    )

    # --- Secret material (no defaults; required at runtime) ----------------------
    master_key: str | None = Field(
        default=None,
        description="Base64-encoded 32-byte master key for envelope encryption of "
        "credential material at rest.",
    )
    jwt_secret: str | None = Field(
        default=None,
        description="Secret used to sign operator session (JWT) tokens.",
    )
    token_pepper: str | None = Field(
        default=None,
        description="Server-side pepper mixed into the HMAC of client tokens.",
    )

    # --- Token lifetimes / renewal windows --------------------------------------
    jwt_access_ttl_seconds: int = Field(
        default=900,
        description="Lifetime of a signed operator access token, in seconds.",
    )
    jwt_refresh_ttl_seconds: int = Field(
        default=1_209_600,
        description="Lifetime of an operator refresh token, in seconds.",
    )
    subscription_renewal_window_seconds: int = Field(
        default=300,
        description="How long before expiry a subscription token is eligible for "
        "proactive refresh, in seconds.",
    )
    session_affinity_ttl_seconds: int = Field(
        default=3600,
        ge=1,
        description="How long a session-to-credential affinity binding is retained "
        "in the Redis session map, in seconds.",
    )
    upstream_request_timeout_seconds: float = Field(
        default=60.0,
        description="Per-call timeout for upstream provider HTTP requests.",
    )
    upstream_max_attempts: int = Field(
        default=3,
        ge=1,
        description="Maximum number of attempts for an upstream provider call, "
        "including the first try. 1 disables retries.",
    )
    upstream_backoff_base_seconds: float = Field(
        default=0.5,
        ge=0.0,
        description="Base delay for exponential backoff between upstream retries, "
        "in seconds.",
    )
    upstream_backoff_max_seconds: float = Field(
        default=10.0,
        ge=0.0,
        description="Upper bound (cap) on the exponential backoff delay between "
        "upstream retries, in seconds.",
    )

    # --- Operator credential policy (Req 16.5; config-driven) -------------------
    # The credential policy enforced when an Operator is created or authenticates.
    # Every rule is configurable; there are no magic numbers in the enforcement
    # logic - it reads exclusively from these settings.
    password_min_length: int = Field(
        default=12,
        description="Minimum number of characters required in an operator password.",
    )
    password_require_uppercase: bool = Field(
        default=True,
        description="Require at least one uppercase letter in operator passwords.",
    )
    password_require_lowercase: bool = Field(
        default=True,
        description="Require at least one lowercase letter in operator passwords.",
    )
    password_require_digit: bool = Field(
        default=True,
        description="Require at least one digit in operator passwords.",
    )
    password_require_symbol: bool = Field(
        default=True,
        description="Require at least one non-alphanumeric symbol in operator passwords.",
    )
    username_min_length: int = Field(
        default=3,
        description="Minimum number of characters required in an operator username.",
    )

    # --- Provider configuration (config-driven; no hardcoded URLs) --------------
    provider_base_urls: dict[str, str] = Field(
        default_factory=dict,
        description="JSON map of provider id -> upstream base URL.",
    )
    provider_oauth: dict[str, dict] = Field(
        default_factory=dict,
        description="JSON map of provider id -> OAuth endpoint metadata "
        "(authorize_url, token_url, client_id, redirect_uri, scopes).",
    )
    provider_models: dict[str, list[str]] = Field(
        default_factory=dict,
        description="JSON map of provider id -> list of model identifiers that "
        "provider can serve. For providers with a live model-listing endpoint this "
        "is a fallback used only when that live lookup is unavailable; for "
        "providers without one (e.g. codex) it is the sole source. Drives the "
        "models advertised by GET /v1/models; model names are never hardcoded in "
        "logic.",
    )
    provider_models_cache_ttl_seconds: int = Field(
        default=300,
        ge=0,
        description="How long a Provider's live model listing is cached in Redis "
        "before it is re-fetched, in seconds. 0 disables caching.",
    )

    @field_validator(
        "provider_base_urls", "provider_oauth", "provider_models", mode="before"
    )
    @classmethod
    def _parse_json_maps(cls, value):
        """Allow these maps to be provided as JSON strings via the environment."""
        if value is None or value == "":
            return {}
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:  # pragma: no cover - config error path
                raise ValueError(f"invalid JSON for provider map: {exc}") from exc
            return parsed
        return value

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    def missing_runtime_requirements(self) -> list[str]:
        """Return the names of required settings that are not yet configured.

        Used by the readiness probe to fail closed until the deployment is fully
        configured. An empty list means the service is ready to serve traffic.
        """
        required = {
            "GOZAR_DATABASE_URL": self.database_url,
            "GOZAR_REDIS_URL": self.redis_url,
            "GOZAR_MASTER_KEY": self.master_key,
            "GOZAR_JWT_SECRET": self.jwt_secret,
            "GOZAR_TOKEN_PEPPER": self.token_pepper,
        }
        return [name for name, value in required.items() if not value]


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so the environment is read once. Tests can clear the cache via
    ``get_settings.cache_clear()`` after mutating the environment.
    """
    return Settings()
