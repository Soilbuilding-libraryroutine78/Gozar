"""Deployment and documentation smoke tests.

These hermetic smoke tests validate the operator-facing deployment surface end to
end without bringing up containers or touching the network:

* **Startup / fail-closed (Req 19.3):** :func:`gozar.app.validate_startup_config`
  refuses to start when the master key (or other required configuration) is missing
  or invalid, and passes with valid settings. The ``GET /health`` liveness probe is
  always ``200``; the ``GET /ready`` readiness probe fails closed with ``503`` while
  unconfigured and returns ``200`` once fully configured.
* **Served docs (Req 18.2, 18.3):** the live app generates an OpenAPI schema and
  serves it at ``/openapi.json`` with the interactive ``/docs`` (Swagger UI) and
  ``/redoc`` views enabled; the schema covers both the ``/v1`` proxy paths and the
  ``/api`` admin paths.
* **License + README disclaimer (Req 20.1, 20.2, 20.3):** a non-commercial
  ``LICENSE`` (PolyForm Noncommercial) exists at the repo root, and ``README.md``
  carries the legal disclaimer (operator responsibility; authors bear no legal
  responsibility for unlawful use / provider-terms violations) and the statement
  that the operator must supply their own subscription accounts and API keys.
* **Compose validity (Req 19.1):** the dev and prod compose files are valid and
  well-formed. When the Docker CLI is available they are validated with
  ``docker compose config``; the files are always parsed as YAML and checked for the
  expected services, health checks, and named volumes. The Docker-dependent check
  skips gracefully when Docker is unavailable so the suite stays hermetic.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from gozar.app import create_app, validate_startup_config
from gozar.core.config import Settings
from gozar.core.errors import ConfigError

# Repository root: tests/smoke/test_deployment_smoke.py -> parents[2].
REPO_ROOT = Path(__file__).resolve().parents[2]

# A deterministic, well-formed 32-byte master key (base64) for envelope encryption,
# matching the convention used by the API test fixtures.
_TEST_MASTER_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()


def _fully_configured_settings(app_env: str = "development") -> Settings:
    """Settings with every runtime requirement satisfied (service is ready)."""
    return Settings(
        app_env=app_env,
        master_key=_TEST_MASTER_KEY,
        jwt_secret="smoke-test-jwt-secret",
        token_pepper="smoke-test-pepper",
        database_url="postgresql+asyncpg://gozar:gozar@localhost:5432/gozar",
        redis_url="redis://localhost:6379/0",
    )


def _secrets_only_settings() -> Settings:
    """Settings with the secret material set but no database/Redis URLs.

    The app can start (the master key and signing secrets are present) but is *not*
    ready to serve traffic, so ``/ready`` must fail closed.
    """
    return Settings(
        app_env="development",
        master_key=_TEST_MASTER_KEY,
        jwt_secret="smoke-test-jwt-secret",
        token_pepper="smoke-test-pepper",
    )


# --------------------------------------------------------------------------- #
# 1. Startup config / key loading + fail-closed probes (Req 19.3)
# --------------------------------------------------------------------------- #


def test_validate_startup_config_passes_with_valid_settings() -> None:
    """A fully configured deployment validates without raising."""
    validate_startup_config(_fully_configured_settings())


def test_validate_startup_config_fails_closed_without_master_key() -> None:
    """A missing master key is fatal at startup (fail closed)."""
    settings = Settings(
        app_env="development",
        master_key=None,
        jwt_secret="smoke-test-jwt-secret",
        token_pepper="smoke-test-pepper",
    )
    with pytest.raises(ConfigError):
        validate_startup_config(settings)


def test_validate_startup_config_fails_closed_with_invalid_master_key() -> None:
    """A malformed (wrong-length) master key is rejected at startup."""
    settings = Settings(
        app_env="development",
        master_key=base64.b64encode(b"too-short").decode(),
        jwt_secret="smoke-test-jwt-secret",
        token_pepper="smoke-test-pepper",
    )
    with pytest.raises(ConfigError):
        validate_startup_config(settings)


def test_validate_startup_config_fails_closed_without_signing_secrets() -> None:
    """The session-signing secret and token pepper are required to start."""
    settings = Settings(
        app_env="development",
        master_key=_TEST_MASTER_KEY,
        jwt_secret=None,
        token_pepper=None,
    )
    with pytest.raises(ConfigError):
        validate_startup_config(settings)


def test_validate_startup_config_production_requires_db_and_redis() -> None:
    """Production refuses to start half-configured (DB/Redis are mandatory)."""
    settings = Settings(
        app_env="production",
        master_key=_TEST_MASTER_KEY,
        jwt_secret="smoke-test-jwt-secret",
        token_pepper="smoke-test-pepper",
        # database_url / redis_url intentionally absent.
    )
    with pytest.raises(ConfigError):
        validate_startup_config(settings)


def test_health_probe_is_ok_when_configured() -> None:
    """The liveness probe reports the process is up."""
    app = create_app(settings=_fully_configured_settings())
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


def test_ready_probe_fails_closed_when_unconfigured() -> None:
    """The readiness probe returns 503 while required configuration is missing."""
    app = create_app(settings=_secrets_only_settings())
    with TestClient(app) as client:
        resp = client.get("/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "not_ready"
        # The missing DB/Redis URLs are reported by name (never their values).
        assert "GOZAR_DATABASE_URL" in body["missing_configuration"]
        assert "GOZAR_REDIS_URL" in body["missing_configuration"]


def test_ready_probe_is_ready_when_fully_configured() -> None:
    """The readiness probe returns 200 once the deployment is fully configured."""
    app = create_app(settings=_fully_configured_settings())
    with TestClient(app) as client:
        resp = client.get("/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"


# --------------------------------------------------------------------------- #
# 2. Served OpenAPI / admin docs (Req 18.2, 18.3)
# --------------------------------------------------------------------------- #


@pytest.fixture
def configured_client() -> Iterator[TestClient]:
    """A TestClient backed by a fully configured app (startup validation passes)."""
    app = create_app(settings=_fully_configured_settings())
    with TestClient(app) as client:
        yield client


def test_openapi_schema_generates() -> None:
    """The application can generate a well-formed OpenAPI schema."""
    app = create_app(settings=_fully_configured_settings())
    schema = app.openapi()
    assert isinstance(schema, dict)
    assert schema.get("openapi")  # version string present
    assert schema.get("info", {}).get("title") == "Gozar"
    assert isinstance(schema.get("paths"), dict) and schema["paths"]


def test_openapi_json_endpoint_served(configured_client: TestClient) -> None:
    """The OpenAPI schema is served as JSON at /openapi.json."""
    resp = configured_client.get("/openapi.json")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json().get("openapi")


def test_interactive_docs_enabled(configured_client: TestClient) -> None:
    """Both Swagger UI (/docs) and ReDoc (/redoc) are enabled."""
    docs = configured_client.get("/docs")
    assert docs.status_code == 200
    assert docs.headers["content-type"].startswith("text/html")

    redoc = configured_client.get("/redoc")
    assert redoc.status_code == 200
    assert redoc.headers["content-type"].startswith("text/html")


def test_schema_documents_proxy_and_admin_paths() -> None:
    """The schema covers both the /v1 proxy paths and the /api admin paths."""
    app = create_app(settings=_fully_configured_settings())
    paths = app.openapi()["paths"]

    assert any(path.startswith("/v1") for path in paths), (
        "OpenAPI schema must document the /v1 proxy data-path"
    )
    assert any(path.startswith("/api") for path in paths), (
        "OpenAPI schema must document the /api admin control-path"
    )
    # The operational probes are documented too.
    assert "/health" in paths
    assert "/ready" in paths


# --------------------------------------------------------------------------- #
# 3. License + README disclaimer presence (Req 20.1, 20.2, 20.3)
# --------------------------------------------------------------------------- #


def test_license_file_is_noncommercial() -> None:
    """A non-commercial open-source LICENSE exists at the repo root (Req 20.1)."""
    license_path = REPO_ROOT / "LICENSE"
    assert license_path.is_file(), "LICENSE file must exist at the repo root"
    text = license_path.read_text(encoding="utf-8")
    assert "PolyForm Noncommercial" in text
    # The license must actually restrict commercial use.
    assert "Noncommercial" in text and "noncommercial purpose" in text.lower()


def test_readme_contains_legal_disclaimer() -> None:
    """README states operator responsibility and authors' non-liability (Req 20.2)."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8").lower()
    # Responsibility for use rests with the Operator.
    assert "responsibility" in readme and "operator" in readme
    # Authors bear no legal responsibility for unlawful use / provider-terms breaches.
    assert "no legal responsibility" in readme
    assert "unlawful use" in readme
    assert "terms of service" in readme or "provider" in readme


def test_readme_states_operator_supplies_own_credentials() -> None:
    """README states the operator must supply their own accounts/keys (Req 20.3)."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8").lower()
    assert "supply their own subscription accounts and api keys" in readme


# --------------------------------------------------------------------------- #
# 4. Compose validity / `docker compose up` reaching healthy (Req 19.1)
# --------------------------------------------------------------------------- #

# Dummy values for the prod compose file's required (${VAR:?...}) variables. These
# are only used to satisfy interpolation during `docker compose config`; no stack is
# ever started.
_PROD_DUMMY_ENV = {
    "POSTGRES_USER": "gozar",
    "POSTGRES_PASSWORD": "dummy",
    "POSTGRES_DB": "gozar",
    "GOZAR_MASTER_KEY": _TEST_MASTER_KEY,
    "GOZAR_JWT_SECRET": "dummy-jwt-secret",
    "GOZAR_TOKEN_PEPPER": "dummy-token-pepper",
}

_EXPECTED_SERVICES = {"backend", "frontend", "postgres", "redis"}
_EXPECTED_VOLUMES = {"gozar_pg_data", "gozar_redis_data"}


@lru_cache
def _docker_compose_available() -> bool:
    """Return True if the Docker CLI with the compose plugin is usable."""
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def test_compose_files_exist() -> None:
    """Both the dev and prod compose files are present."""
    assert (REPO_ROOT / "compose.yml").is_file()
    assert (REPO_ROOT / "compose.prod.yml").is_file()


@pytest.mark.parametrize(
    ("compose_file", "extra_env"),
    [
        ("compose.yml", {}),
        ("compose.prod.yml", _PROD_DUMMY_ENV),
    ],
)
def test_compose_file_well_formed(compose_file: str, extra_env: dict) -> None:
    """Each compose file parses as YAML with the expected services and volumes.

    This runs regardless of Docker availability so the structural guarantees (a
    `docker compose up` would have all four services with health checks and durable
    named volumes) are always verified.
    """
    data = yaml.safe_load((REPO_ROOT / compose_file).read_text(encoding="utf-8"))
    assert isinstance(data, dict)

    services = data.get("services", {})
    assert _EXPECTED_SERVICES.issubset(services.keys()), (
        f"{compose_file} must define services {sorted(_EXPECTED_SERVICES)}"
    )

    # Every long-lived service declares a health check so `up` can reach healthy.
    for name in _EXPECTED_SERVICES:
        assert "healthcheck" in services[name], (
            f"{compose_file}: service '{name}' must declare a healthcheck"
        )
        assert services[name]["healthcheck"].get("test"), (
            f"{compose_file}: service '{name}' healthcheck must define a test"
        )

    # Durable named volumes for Postgres and Redis data.
    volumes = data.get("volumes", {})
    assert _EXPECTED_VOLUMES.issubset(volumes.keys()), (
        f"{compose_file} must declare named volumes {sorted(_EXPECTED_VOLUMES)}"
    )


@pytest.mark.skipif(
    not _docker_compose_available(),
    reason="Docker CLI with the compose plugin is not available",
)
@pytest.mark.parametrize(
    ("compose_file", "extra_env"),
    [
        ("compose.yml", {}),
        ("compose.prod.yml", _PROD_DUMMY_ENV),
    ],
)
def test_docker_compose_config_valid(compose_file: str, extra_env: dict) -> None:
    """`docker compose config -q` accepts each compose file (validates wiring)."""
    import os

    env = {**os.environ, **extra_env}
    result = subprocess.run(
        ["docker", "compose", "-f", compose_file, "config", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        timeout=60,
        check=False,
        env=env,
    )
    assert result.returncode == 0, (
        f"`docker compose -f {compose_file} config` failed:\n"
        f"{result.stderr.decode(errors='replace')}"
    )
