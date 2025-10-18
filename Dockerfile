# ============================================================================
# Production-Grade Dockerfile with UV (Enhanced Version)
# ============================================================================
# Build args for flexibility
ARG PYTHON_VERSION=3.11.10
ARG UV_VERSION=0.5.13
ARG WORKERS=4

# ============================================================================
# Stage 0: UV installer (workaround for --from variable expansion)
# ============================================================================
FROM ghcr.io/astral-sh/uv:0.5.13 AS uv

# ============================================================================
# Stage 1: Builder - Install dependencies
# ============================================================================
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

# Labels for image metadata
LABEL maintainer="copilotgtm-team@company.com" \
      version="1.0.0" \
      description="Copilot Backend - FastAPI with UV" \
      python.version="${PYTHON_VERSION}"

# Set environment variables for Python optimization
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy

# Install system dependencies required for building Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Install UV - pinned version for reproducibility
COPY --from=uv /uv /usr/local/bin/uv

# Set working directory
WORKDIR /app

# Copy dependency files (changes less frequently - better caching)
COPY pyproject.toml uv.lock .python-version ./

# Create virtual environment and install dependencies
# Using --frozen ensures we install exactly what's in the lockfile
# --no-dev: Skip dev dependencies (black, pytest, etc.) for production
# --no-install-project: Don't install the project itself yet (only dependencies)
RUN uv sync --frozen --no-dev --no-install-project --verbose \
    && find /app/.venv -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true \
    && find /app/.venv -type f -name "*.pyc" -delete \
    && find /app/.venv -type f -name "*.pyo" -delete \
    && rm -rf /root/.cache

# ============================================================================
# Stage 2: Runtime - Minimal production image
# ============================================================================
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

# Labels
LABEL stage="runtime"

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app:$PYTHONPATH" \
    UV_LINK_MODE=copy

# Install only runtime system dependencies
# Note: curl is only for healthcheck, ca-certificates for HTTPS
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean \
    && rm -rf /tmp/* /var/tmp/*

# Create non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Set working directory
WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv

# Copy application code (changes frequently - do last)
COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser migrations/ ./migrations/
COPY --chown=appuser:appuser scripts/ ./scripts/

# Remove any __pycache__ from source code
RUN find ./src -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true \
    && find ./src -type f -name "*.pyc" -delete

# Create log directory with proper permissions
RUN mkdir -p /var/log /tmp && \
    chown -R appuser:appuser /var/log /tmp

# Switch to non-root user
USER appuser

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:80/health || exit 1

# Expose application port
EXPOSE 80

# Start Uvicorn with production settings
ARG WORKERS
CMD uvicorn src.main:app \
    --host 0.0.0.0 \
    --port 80 \
    --workers ${WORKERS} \
    --log-level info \
    --access-log \
    --proxy-headers \
    --forwarded-allow-ips "*" \
    --timeout-keep-alive 5
