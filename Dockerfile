# syntax=docker/dockerfile:1.6
# ============================================================================
# SECURITY-HARDENED Multi-Stage Dockerfile for Copilot Backend (Production)
# ============================================================================
# Compliance: CIS Docker Benchmark v1.8.0, OWASP Docker Security
#
# Targets:
#   - api:              FastAPI application (default)
#   - knowledge-worker: Heavy ML/Document processing worker (~3GB image)
#   - light-worker:     Chat, Deal, Organization, Content workers (~500MB)
#   - integrations-worker: CRM/Third-party sync worker (~600MB)
#   - full:             Development image with all dependencies (~4GB)
#
# Build examples:
#   docker build --target api -t copilot-backend-api:latest .
#   docker build --target knowledge-worker -t copilot-backend-knowledge:latest .
#   docker build --target light-worker -t copilot-backend-workers:latest .
#   docker build --target integrations-worker -t copilot-backend-integrations:latest .
# ============================================================================

# Build args for flexibility
ARG PYTHON_VERSION=3.11.11
ARG UV_VERSION=0.5.13
# Security: Fixed UID/GID for reproducibility and K8s compatibility (CIS 4.1)
ARG APP_UID=10001
ARG APP_GID=10001

# ============================================================================
# Stage 0: UV installer
# ============================================================================
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ============================================================================
# Stage 1: Base Builder - Core dependencies only
# ============================================================================
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder-base

LABEL maintainer="copilotgtm-team@company.com" \
      version="2.2.0" \
      description="Copilot Backend - Security Hardened Build"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_CACHE_DIR=/root/.cache/uv

# Install system dependencies for building Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* \
    && apt-get clean

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./

# ============================================================================
# Stage 2a: API Builder
# ============================================================================
FROM builder-base AS builder-api

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra api \
    && find /app/.venv -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true \
    && find /app/.venv -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete

# ============================================================================
# Stage 2b: Knowledge Worker Builder
# ============================================================================
FROM builder-base AS builder-knowledge

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra knowledge \
    && find /app/.venv -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true \
    && find /app/.venv -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete

# ============================================================================
# Stage 2c: Light Worker Builder
# ============================================================================
FROM builder-base AS builder-light

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra workers-light \
    && find /app/.venv -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true \
    && find /app/.venv -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete

# ============================================================================
# Stage 2d: Integrations Worker Builder
# ============================================================================
FROM builder-base AS builder-integrations

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra integrations \
    && find /app/.venv -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true \
    && find /app/.venv -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete

# ============================================================================
# Stage 2e: Full Builder
# ============================================================================
FROM builder-base AS builder-full

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra full \
    && find /app/.venv -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true \
    && find /app/.venv -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete

# ============================================================================
# Stage 3a: Runtime Base for Workers (minimal - no curl)
# ============================================================================
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime-base-worker

ARG APP_UID
ARG APP_GID

# Security and OCI compliance labels
LABEL org.opencontainers.image.vendor="CopilotGTM" \
      org.opencontainers.image.title="Copilot Backend Worker" \
      org.opencontainers.image.description="Production worker runtime - security hardened" \
      org.opencontainers.image.licenses="Proprietary" \
      org.opencontainers.image.base.name="python:${PYTHON_VERSION}-slim-bookworm" \
      # Security compliance markers
      com.copilotgtm.security.cis-benchmark="1.8.0" \
      com.copilotgtm.security.non-root="true" \
      com.copilotgtm.security.read-only-supported="true" \
      com.copilotgtm.security.no-curl="true"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="/app" \
    PATH="/app/.venv/bin:$PATH" \
    HOME=/home/appuser \
    # Security: Disable pip version check
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Minimal runtime dependencies (no curl - workers use Python HTTP libs)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* \
    && apt-get clean \
    && python -m pip install --no-cache-dir --upgrade "setuptools>=78.1.1" "wheel>=0.46.2" \
    # Security: Create non-root user with fixed UID/GID
    && groupadd -r -g ${APP_GID} appuser \
    && useradd -r -u ${APP_UID} -g appuser -d /home/appuser -s /sbin/nologin appuser \
    # Create required directories with proper ownership
    && mkdir -p /app /home/appuser /tmp /var/log \
    && chown -R appuser:appuser /app /home/appuser /tmp /var/log

WORKDIR /app

# ============================================================================
# Stage 3b: Runtime Base for API (with curl for healthcheck)
# ============================================================================
FROM runtime-base-worker AS runtime-base-api

# API needs curl for HTTP healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* \
    && apt-get clean

# Override label to indicate curl is present
LABEL com.copilotgtm.security.no-curl="false"

# ============================================================================
# Target: API - FastAPI Application
# ============================================================================
FROM runtime-base-api AS api

LABEL component="api" \
      org.opencontainers.image.title="Copilot Backend API" \
      description="FastAPI Application - Production"

# Production Python optimizations
ENV PYTHONOPTIMIZE=1

COPY --from=builder-api --chown=appuser:appuser /app/.venv /app/.venv

COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser migrations/ ./migrations/
COPY --chown=appuser:appuser scripts/ ./scripts/

RUN find ./scripts -type f -name "*.sh" -exec sed -i 's/\r$//' {} +

USER appuser

EXPOSE 8080

# Health check for container orchestration
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

STOPSIGNAL SIGTERM

CMD ["uvicorn", "src.main:app", \
    "--host", "0.0.0.0", \
    "--port", "8080", \
    "--workers", "2", \
    "--timeout-keep-alive", "75", \
    "--timeout-graceful-shutdown", "30"]

# ============================================================================
# Target: Knowledge Worker - Heavy ML/Document Processing
# ============================================================================
FROM runtime-base-worker AS knowledge-worker

LABEL component="knowledge-worker" \
      org.opencontainers.image.title="Copilot Knowledge Worker" \
      description="Knowledge Worker - Document Processing with ML"

# ML model cache directories
ENV HF_HOME=/app/.cache/huggingface \
    TRANSFORMERS_CACHE=/app/.cache/huggingface/transformers \
    SENTENCE_TRANSFORMERS_HOME=/app/.cache/sentence-transformers

COPY --from=builder-knowledge --chown=appuser:appuser /app/.venv /app/.venv

COPY --chown=appuser:appuser src/domain/ ./src/domain/
COPY --chown=appuser:appuser src/infra/ ./src/infra/
COPY --chown=appuser:appuser src/workers/ ./src/workers/
COPY --chown=appuser:appuser src/service/ ./src/service/
COPY --chown=appuser:appuser src/agents/ ./src/agents/
COPY --chown=appuser:appuser src/adapters/ ./src/adapters/
COPY --chown=appuser:appuser src/contracts/ ./src/contracts/
COPY --chown=appuser:appuser migrations/ ./migrations/
COPY --chown=appuser:appuser scripts/ ./scripts/

RUN find ./scripts -type f -name "*.sh" -exec sed -i 's/\r$//' {} + \
    && mkdir -p /app/.cache \
    && chown -R appuser:appuser /app/.cache

USER appuser

# Health check using celery inspect (no curl needed)
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD celery -A src.workers.knowledge_worker.celery_app inspect ping -d celery@$HOSTNAME || exit 1

CMD ["celery", "-A", "src.workers.knowledge_worker.celery_app", "worker", \
    "--loglevel=info", \
    "--pool=prefork"]

# ============================================================================
# Target: Light Worker - Chat, Deal, Organization, Content Workers
# ============================================================================
FROM runtime-base-worker AS light-worker

LABEL component="light-worker" \
      org.opencontainers.image.title="Copilot Light Worker" \
      description="Lightweight Workers - Chat, Deal, Organization, Content"

COPY --from=builder-light --chown=appuser:appuser /app/.venv /app/.venv

COPY --chown=appuser:appuser src/domain/ ./src/domain/
COPY --chown=appuser:appuser src/infra/ ./src/infra/
COPY --chown=appuser:appuser src/workers/ ./src/workers/
COPY --chown=appuser:appuser src/service/ ./src/service/
COPY --chown=appuser:appuser src/agents/ ./src/agents/
COPY --chown=appuser:appuser src/adapters/ ./src/adapters/
COPY --chown=appuser:appuser src/contracts/ ./src/contracts/
COPY --chown=appuser:appuser migrations/ ./migrations/
COPY --chown=appuser:appuser scripts/ ./scripts/

RUN find ./scripts -type f -name "*.sh" -exec sed -i 's/\r$//' {} +

USER appuser

# CELERY_APP can be: chat_worker, deal_worker, organization_worker, content_worker
ENV CELERY_APP=chat_worker

# Using exec for proper signal handling with variable expansion
CMD ["sh", "-c", "exec celery -A src.workers.${CELERY_APP}.celery_app worker --loglevel=info --pool=prefork"]

# ============================================================================
# Target: Integrations Worker - CRM/Third-Party Sync
# ============================================================================
FROM runtime-base-worker AS integrations-worker

LABEL component="integrations-worker" \
      org.opencontainers.image.title="Copilot Integrations Worker" \
      description="Integrations Worker - CRM Sync"

COPY --from=builder-integrations --chown=appuser:appuser /app/.venv /app/.venv

COPY --chown=appuser:appuser src/domain/ ./src/domain/
COPY --chown=appuser:appuser src/infra/ ./src/infra/
COPY --chown=appuser:appuser src/workers/ ./src/workers/
COPY --chown=appuser:appuser src/service/ ./src/service/
COPY --chown=appuser:appuser src/agents/ ./src/agents/
COPY --chown=appuser:appuser src/adapters/ ./src/adapters/
COPY --chown=appuser:appuser src/contracts/ ./src/contracts/
COPY --chown=appuser:appuser migrations/ ./migrations/
COPY --chown=appuser:appuser scripts/ ./scripts/

RUN find ./scripts -type f -name "*.sh" -exec sed -i 's/\r$//' {} +

USER appuser

# Health check using celery inspect (no curl needed)
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD celery -A src.workers.integrations_worker.celery_app inspect ping -d celery@$HOSTNAME || exit 1

CMD ["celery", "-A", "src.workers.integrations_worker.celery_app", "worker", \
    "--loglevel=info", \
    "--pool=prefork"]

# ============================================================================
# Target: Full - All Dependencies (Development/Testing)
# ============================================================================
FROM runtime-base-api AS full

LABEL component="full" \
      org.opencontainers.image.title="Copilot Backend Full" \
      description="Full Image - All Dependencies (Development)"

# ML model cache directories
ENV HF_HOME=/app/.cache/huggingface \
    TRANSFORMERS_CACHE=/app/.cache/huggingface/transformers \
    SENTENCE_TRANSFORMERS_HOME=/app/.cache/sentence-transformers

COPY --from=builder-full --chown=appuser:appuser /app/.venv /app/.venv

COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser migrations/ ./migrations/
COPY --chown=appuser:appuser scripts/ ./scripts/

RUN find ./scripts -type f -name "*.sh" -exec sed -i 's/\r$//' {} + \
    && mkdir -p /app/.cache \
    && chown -R appuser:appuser /app/.cache

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

STOPSIGNAL SIGTERM

CMD ["uvicorn", "src.main:app", \
    "--host", "0.0.0.0", \
    "--port", "8080", \
    "--workers", "2", \
    "--timeout-keep-alive", "75", \
    "--timeout-graceful-shutdown", "30"]

# ============================================================================
# Default Target (backward compatibility)
# ============================================================================
FROM full AS runtime
