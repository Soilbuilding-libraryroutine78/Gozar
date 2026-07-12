# syntax=docker/dockerfile:1

# Multi-stage build for the Gozar backend.
# Stage 1 compiles dependencies into an isolated virtualenv; the runtime stage
# carries only that virtualenv and the application source, runs as a non-root
# user, and ships with a stdlib-only liveness health check.

############################################
# Stage 1: builder
############################################
FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# Build toolchain is needed only to compile any sdist-only wheels here; it is
# deliberately NOT carried into the runtime image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv "$VIRTUAL_ENV"

WORKDIR /build

# Copy only what the install needs so the dependency layer caches well.
COPY pyproject.toml README.md ./
COPY gozar ./gozar

RUN pip install --upgrade pip \
    && pip install .

############################################
# Stage 2: runtime
############################################
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    GOZAR_HTTP_HOST=0.0.0.0 \
    GOZAR_HTTP_PORT=8000

# Non-root runtime user (fixed uid/gid for predictable volume ownership).
RUN groupadd --system --gid 1001 gozar \
    && useradd --system --uid 1001 --gid gozar --no-create-home --home-dir /app gozar

WORKDIR /app

# Bring over the prebuilt virtualenv (dependencies + installed package) and the
# application source. No build tools, caches, or secrets are copied.
COPY --from=builder /opt/venv /opt/venv
COPY --chown=gozar:gozar gozar ./gozar

# Database migrations + Alembic config, so the container can self-migrate on
# startup (see docker-entrypoint.sh). env.py reads GOZAR_DATABASE_URL at runtime.
COPY --chown=gozar:gozar migrations ./migrations
COPY --chown=gozar:gozar alembic.ini ./alembic.ini
COPY --chown=gozar:gozar docker-entrypoint.sh ./docker-entrypoint.sh
RUN chmod +x ./docker-entrypoint.sh

USER gozar

EXPOSE 8000

# Liveness check using only the Python standard library (no extra runtime deps).
# urlopen raises on a non-2xx status, which fails the check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('GOZAR_HTTP_PORT','8000')+'/health', timeout=3)" || exit 1

# Production-style default command (no autoreload). Dev enables --reload via compose.
# The entrypoint runs `alembic upgrade head` first so the deployment self-migrates.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["sh", "-c", "uvicorn gozar.app:app --host \"${GOZAR_HTTP_HOST:-0.0.0.0}\" --port \"${GOZAR_HTTP_PORT:-8000}\""]
