# Dockerfile Security Hardening Plan

## Executive Summary

This plan outlines production-ready security hardening for our Docker images based on:
- [CIS Docker Benchmark v1.8.0](https://www.cisecurity.org/benchmark/docker)
- [OWASP Docker Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html)
- [Snyk Best Practices](https://snyk.io/blog/best-practices-containerizing-python-docker/)
- [Docker Official Security Guidelines](https://docs.docker.com/dhi/core-concepts/cis/)

---

## Current State Assessment

| Security Control | Current Status | Risk Level |
|------------------|----------------|------------|
| Non-root user | ✅ Implemented | Low |
| Multi-stage builds | ✅ Implemented | Low |
| Minimal base image (slim) | ✅ Implemented | Low |
| HEALTHCHECK | ✅ Implemented | Low |
| Read-only filesystem | ❌ Not implemented | Medium |
| no-new-privileges | ❌ Not implemented | Medium |
| Capability dropping | ❌ Not implemented | Medium |
| Security scanning in CI | ❌ Not implemented | High |
| Image signing | ❌ Not implemented | Medium |
| Fixed user UID/GID | ❌ Not implemented | Low |
| tmpfs for temp files | ❌ Not implemented | Low |

---

## Phase 1: Dockerfile Hardening (Immediate)

### 1.1 Fixed User UID/GID (CIS 4.1)

**Why:** Consistent user IDs prevent permission issues and improve audit traceability.

```dockerfile
# Use fixed UID/GID for reproducibility across environments
ARG APP_UID=10001
ARG APP_GID=10001

RUN groupadd -r -g ${APP_GID} appuser \
    && useradd -r -u ${APP_UID} -g appuser -d /home/appuser -s /sbin/nologin appuser
```

### 1.2 Minimal Runtime Dependencies

**Why:** Reduce attack surface by removing unnecessary packages.

**API image:** Needs curl for healthcheck
**Worker images:** Don't need curl - use celery inspect

```dockerfile
# API runtime base
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime-base-api
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Worker runtime base (no curl)
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime-base-worker
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*
```

### 1.3 Read-Only Filesystem Support (CIS 5.12)

**Why:** Prevents attackers from modifying container filesystem, enforces immutable infrastructure.

```dockerfile
# Ensure all writable paths use tmpfs or volumes
# Application should write only to:
# - /tmp (tmpfs)
# - /var/log (tmpfs or volume)
# - /app/.cache (volume for ML models)

# In Dockerfile - ensure directories exist
RUN mkdir -p /tmp /var/log /app/.cache \
    && chown -R appuser:appuser /tmp /var/log /app/.cache
```

**docker-compose.yml / K8s deployment:**
```yaml
# Docker run example
docker run --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,size=100m \
    --tmpfs /var/log:rw,noexec,nosuid,size=50m \
    -v model-cache:/app/.cache \
    copilot-api:latest
```

### 1.4 Security Labels (OCI Standard)

```dockerfile
LABEL org.opencontainers.image.vendor="CopilotGTM" \
      org.opencontainers.image.title="Copilot Backend API" \
      org.opencontainers.image.description="Production API server" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.licenses="Proprietary" \
      org.opencontainers.image.source="https://dev.azure.com/copilotgtm/app-copilotgtm/_git/copilot-backend"
```

---

## Phase 2: Runtime Security (Docker Compose / Kubernetes)

### 2.1 no-new-privileges (OWASP Recommended)

**Why:** Prevents privilege escalation via setuid/setgid binaries.

```yaml
# docker-compose.yml
services:
  api:
    security_opt:
      - no-new-privileges:true
```

```yaml
# Kubernetes securityContext
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
```

### 2.2 Capability Dropping (CIS 5.3)

**Why:** Linux capabilities allow fine-grained permissions. Drop all, add only what's needed.

```yaml
# docker-compose.yml
services:
  api:
    cap_drop:
      - ALL
    cap_add:
      - NET_BIND_SERVICE  # Only if binding to ports < 1024
```

```yaml
# Kubernetes
securityContext:
  capabilities:
    drop:
      - ALL
```

### 2.3 Resource Limits (CIS 5.10, 5.11)

**Why:** Prevents DoS attacks and resource exhaustion.

```yaml
# docker-compose.yml
services:
  api:
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 2G
        reservations:
          cpus: '0.5'
          memory: 512M

  knowledge-worker:
    deploy:
      resources:
        limits:
          cpus: '4'
          memory: 8G
```

### 2.4 Network Security

```yaml
# docker-compose.yml - Disable inter-container communication by default
networks:
  backend:
    driver: bridge
    driver_opts:
      com.docker.network.bridge.enable_icc: "false"
```

---

## Phase 3: CI/CD Security Scanning

### 3.1 Trivy Integration (Azure Pipelines)

**Why:** Detect vulnerabilities before deployment.

```yaml
# azure-pipelines-dev.yml addition
- task: Bash@3
  displayName: 'Scan image with Trivy'
  inputs:
    targetType: 'inline'
    script: |
      # Install Trivy
      curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | sh -s -- -b /usr/local/bin

      # Scan for CRITICAL and HIGH vulnerabilities
      trivy image \
        --severity CRITICAL,HIGH \
        --exit-code 1 \
        --ignore-unfixed \
        --no-progress \
        $(ACR_LOGIN_SERVER)/copilot-api:$(Build.BuildId)
  continueOnError: false  # Fail pipeline on vulnerabilities
```

### 3.2 Dockerfile Linting (Hadolint)

```yaml
- task: Bash@3
  displayName: 'Lint Dockerfile with Hadolint'
  inputs:
    targetType: 'inline'
    script: |
      docker run --rm -i hadolint/hadolint < Dockerfile
```

### 3.3 Secret Scanning

```yaml
- task: Bash@3
  displayName: 'Scan for secrets with Trivy'
  inputs:
    targetType: 'inline'
    script: |
      trivy fs --scanners secret --exit-code 1 .
```

---

## Phase 4: Complete Hardened Dockerfile

### Production Dockerfile Changes

```dockerfile
# syntax=docker/dockerfile:1.6
# ============================================================================
# SECURITY-HARDENED Multi-Stage Dockerfile for Copilot Backend
# ============================================================================
# Compliance: CIS Docker Benchmark v1.8.0, OWASP Docker Security
# ============================================================================

ARG PYTHON_VERSION=3.11.10
ARG UV_VERSION=0.5.13
ARG APP_UID=10001
ARG APP_GID=10001

# ... (builder stages remain the same) ...

# ============================================================================
# Runtime Base - Security Hardened
# ============================================================================
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime-base

ARG APP_UID
ARG APP_GID

# Security and compliance labels
LABEL org.opencontainers.image.vendor="CopilotGTM" \
      org.opencontainers.image.title="Copilot Backend" \
      org.opencontainers.image.licenses="Proprietary" \
      org.opencontainers.image.base.name="python:${PYTHON_VERSION}-slim-bookworm" \
      # CIS compliance markers
      com.copilotgtm.security.cis-benchmark="1.8.0" \
      com.copilotgtm.security.non-root="true" \
      com.copilotgtm.security.read-only-supported="true"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="/app" \
    PATH="/app/.venv/bin:$PATH" \
    HOME=/home/appuser \
    # Security: Disable pip version check (reduces network calls)
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install minimal runtime dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpq5 \
        ca-certificates \
    # Security: Remove apt cache
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* \
    && apt-get clean \
    # Security: Remove unnecessary system users
    && rm -rf /var/log/* /tmp/* \
    # Create non-root user with fixed UID/GID
    && groupadd -r -g ${APP_GID} appuser \
    && useradd -r -u ${APP_UID} -g appuser -d /home/appuser -s /sbin/nologin appuser \
    # Create required directories
    && mkdir -p /app /home/appuser /tmp /var/log \
    && chown -R appuser:appuser /app /home/appuser /tmp /var/log

WORKDIR /app

# ============================================================================
# API Target - Security Hardened
# ============================================================================
FROM runtime-base AS api

# API needs curl for healthcheck
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

LABEL component="api"

ENV PYTHONOPTIMIZE=1

COPY --from=builder-api --chown=appuser:appuser /app/.venv /app/.venv
COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser migrations/ ./migrations/
COPY --chown=appuser:appuser scripts/ ./scripts/

RUN find ./scripts -type f -name "*.sh" -exec sed -i 's/\r$//' {} +

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
```

---

## Phase 5: Kubernetes Security Context

```yaml
# Helm values.yaml additions
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  runAsGroup: 10001
  fsGroup: 10001
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop:
      - ALL
  seccompProfile:
    type: RuntimeDefault

volumeMounts:
  - name: tmp
    mountPath: /tmp
  - name: logs
    mountPath: /var/log
  - name: cache
    mountPath: /app/.cache

volumes:
  - name: tmp
    emptyDir:
      sizeLimit: 100Mi
  - name: logs
    emptyDir:
      sizeLimit: 50Mi
  - name: cache
    emptyDir:
      sizeLimit: 1Gi
```

---

## Implementation Checklist

### Dockerfile Changes
- [ ] Add fixed UID/GID (10001:10001)
- [ ] Separate runtime bases for API vs Workers
- [ ] Add OCI security labels
- [ ] Remove curl from worker images
- [ ] Ensure all writable paths are documented

### docker-compose.yml Changes
- [ ] Add `security_opt: no-new-privileges:true`
- [ ] Add `cap_drop: ALL`
- [ ] Add `read_only: true` with appropriate tmpfs mounts
- [ ] Add resource limits

### CI/CD Pipeline Changes
- [ ] Add Trivy vulnerability scanning
- [ ] Add Hadolint Dockerfile linting
- [ ] Add secret scanning
- [ ] Fail pipeline on CRITICAL vulnerabilities

### Kubernetes/Helm Changes
- [ ] Add securityContext to all deployments
- [ ] Add PodSecurityPolicy/PodSecurityStandard
- [ ] Configure NetworkPolicies
- [ ] Add resource limits

---

## Risk Assessment

| Change | Security Benefit | Implementation Risk | Priority |
|--------|------------------|---------------------|----------|
| Fixed UID/GID | Audit trail, consistency | Low | High |
| no-new-privileges | Prevent privilege escalation | Low | High |
| Read-only filesystem | Immutable infrastructure | Medium (needs testing) | High |
| Capability dropping | Reduce attack surface | Low | High |
| Trivy scanning | Vulnerability detection | Low | Critical |
| Remove curl from workers | Smaller attack surface | Low | Medium |

---

## Sources

- [CIS Docker Benchmarks](https://www.cisecurity.org/benchmark/docker)
- [OWASP Docker Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html)
- [Docker Bench for Security](https://github.com/docker/docker-bench-security)
- [Snyk Container Best Practices](https://snyk.io/blog/best-practices-containerizing-python-docker/)
- [Trivy Documentation](https://www.virtualizationhowto.com/2024/08/trivy-scanner-for-docker-image-vulnerability-scanning/)
- [Read-Only Filesystems](https://www.thorsten-hans.com/read-only-filesystems-in-docker-and-kubernetes/)
- [no-new-privileges](https://www.mendelowski.com/til/2024/07/no-new-privileges-docker)
