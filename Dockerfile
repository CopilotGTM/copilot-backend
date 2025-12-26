# syntax=docker/dockerfile:1.6
# ============================================================================
# Multi-Stage Dockerfile for Copilot Backend
# ============================================================================
# This Dockerfile supports multiple build targets for different components:
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
#   docker build --target full -t copilot-backend-full:latest .  # Dev/testing
# ============================================================================

# Build args for flexibility
ARG PYTHON_VERSION=3.11.10
ARG UV_VERSION=0.5.13
ARG WORKERS=4

# ============================================================================
# Stage 0: UV installer (workaround for --from variable expansion)
# ============================================================================
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ============================================================================
# Stage 1: Base Builder - Core dependencies only
# ============================================================================
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder-base

LABEL maintainer="copilotgtm-team@company.com" \
      version="2.0.0" \
      description="Copilot Backend - Multi-Stage Build"

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
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock .python-version ./

# ============================================================================
# Stage 2a: API Builder - Install API dependencies
# ============================================================================
FROM builder-base AS builder-api

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra api --verbose \
    && find /app/.venv -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true \
    && find /app/.venv -type f -name "*.pyc" -delete \
    && find /app/.venv -type f -name "*.pyo" -delete

# ============================================================================
# Stage 2b: Knowledge Worker Builder - Install heavy ML dependencies
# ============================================================================
FROM builder-base AS builder-knowledge

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra knowledge --verbose \
    && find /app/.venv -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true \
    && find /app/.venv -type f -name "*.pyc" -delete \
    && find /app/.venv -type f -name "*.pyo" -delete

# ============================================================================
# Stage 2c: Light Worker Builder - Install lightweight worker dependencies
# ============================================================================
FROM builder-base AS builder-light

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra workers-light --verbose \
    && find /app/.venv -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true \
    && find /app/.venv -type f -name "*.pyc" -delete \
    && find /app/.venv -type f -name "*.pyo" -delete

# ============================================================================
# Stage 2d: Integrations Worker Builder
# ============================================================================
FROM builder-base AS builder-integrations

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra integrations --verbose \
    && find /app/.venv -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true \
    && find /app/.venv -type f -name "*.pyc" -delete \
    && find /app/.venv -type f -name "*.pyo" -delete

# ============================================================================
# Stage 2e: Full Builder - All dependencies (Development/Testing)
# ============================================================================
FROM builder-base AS builder-full

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra full --verbose \
    && find /app/.venv -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true \
    && find /app/.venv -type f -name "*.pyc" -delete \
    && find /app/.venv -type f -name "*.pyo" -delete

# ============================================================================
# Stage 3: Runtime Base - Common runtime configuration
# ============================================================================
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime-base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app:$PYTHONPATH" \
    UV_LINK_MODE=copy \
    HOME=/home/appuser

# Install runtime system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /app

# ============================================================================
# Target: API - FastAPI Application
# ============================================================================
FROM runtime-base AS api

LABEL component="api" \
      description="FastAPI Application"

COPY --from=builder-api --chown=appuser:appuser /app/.venv /app/.venv

# Copy application code
COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser migrations/ ./migrations/
COPY --chown=appuser:appuser scripts/ ./scripts/

RUN find ./scripts -type f -name "*.sh" -exec sed -i 's/\r$//' {} +

RUN mkdir -p /var/log /tmp && \
    chown -R appuser:appuser /var/log /tmp

USER appuser

EXPOSE 8080

CMD uvicorn src.main:app \
    --host 0.0.0.0 \
    --port 8080 \
    --workers 2

# ============================================================================
# Target: Knowledge Worker - Heavy ML/Document Processing
# ============================================================================
FROM runtime-base AS knowledge-worker

LABEL component="knowledge-worker" \
      description="Knowledge Worker - Document Processing with ML"

# Additional environment for ML models
ENV HF_HOME=/app/.cache/huggingface \
    TRANSFORMERS_CACHE=/app/.cache/huggingface/transformers \
    SENTENCE_TRANSFORMERS_HOME=/app/.cache/sentence-transformers

COPY --from=builder-knowledge --chown=appuser:appuser /app/.venv /app/.venv

# Copy application code
COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser migrations/ ./migrations/
COPY --chown=appuser:appuser scripts/ ./scripts/

RUN find ./scripts -type f -name "*.sh" -exec sed -i 's/\r$//' {} +

# Create directories with proper permissions including ML cache
RUN mkdir -p /var/log /tmp /app/.cache /home/appuser && \
    chown -R appuser:appuser /var/log /tmp /app/.cache /home/appuser

USER appuser

# Knowledge worker runs Celery
CMD celery -A src.workers.knowledge_worker.celery_app worker \
    --loglevel=info \
    --pool=prefork

# ============================================================================
# Target: Light Worker - Chat, Deal, Organization, Content Workers
# ============================================================================
FROM runtime-base AS light-worker

LABEL component="light-worker" \
      description="Lightweight Workers - Chat, Deal, Organization, Content"

COPY --from=builder-light --chown=appuser:appuser /app/.venv /app/.venv

# Copy application code
COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser migrations/ ./migrations/
COPY --chown=appuser:appuser scripts/ ./scripts/

RUN find ./scripts -type f -name "*.sh" -exec sed -i 's/\r$//' {} +

RUN mkdir -p /var/log /tmp && \
    chown -R appuser:appuser /var/log /tmp

USER appuser

# Default: chat worker (override with environment or command args)
# CELERY_APP can be: chat_worker, deal_worker, organization_worker, content_worker
ENV CELERY_APP=chat_worker

CMD celery -A src.workers.${CELERY_APP}.celery_app worker \
    --loglevel=info \
    --pool=prefork

# ============================================================================
# Target: Integrations Worker - CRM/Third-Party Sync
# ============================================================================
FROM runtime-base AS integrations-worker

LABEL component="integrations-worker" \
      description="Integrations Worker - CRM Sync"

COPY --from=builder-integrations --chown=appuser:appuser /app/.venv /app/.venv

# Copy application code
COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser migrations/ ./migrations/
COPY --chown=appuser:appuser scripts/ ./scripts/

RUN find ./scripts -type f -name "*.sh" -exec sed -i 's/\r$//' {} +

RUN mkdir -p /var/log /tmp && \
    chown -R appuser:appuser /var/log /tmp

USER appuser

CMD celery -A src.workers.integrations_worker.celery_app worker \
    --loglevel=info \
    --pool=prefork

# ============================================================================
# Target: Full - All Dependencies (Development/Testing/Default)
# ============================================================================
FROM runtime-base AS full

LABEL component="full" \
      description="Full Image - All Dependencies (Development)"

# Additional environment for ML models
ENV HF_HOME=/app/.cache/huggingface \
    TRANSFORMERS_CACHE=/app/.cache/huggingface/transformers \
    SENTENCE_TRANSFORMERS_HOME=/app/.cache/sentence-transformers

COPY --from=builder-full --chown=appuser:appuser /app/.venv /app/.venv

# Copy application code
COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser migrations/ ./migrations/
COPY --chown=appuser:appuser scripts/ ./scripts/

RUN find ./scripts -type f -name "*.sh" -exec sed -i 's/\r$//' {} +

RUN mkdir -p /var/log /tmp /app/.cache /home/appuser && \
    chown -R appuser:appuser /var/log /tmp /app/.cache /home/appuser

USER appuser

EXPOSE 8080

CMD uvicorn src.main:app \
    --host 0.0.0.0 \
    --port 8080 \
    --workers 2

# ============================================================================
# Default Target (for backward compatibility)
# ============================================================================
FROM full AS runtime
