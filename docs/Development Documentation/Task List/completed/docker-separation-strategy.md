# Task: Docker Image Separation Strategy - Workers vs API

**Created:** December 26, 2025  
**Status:** Implemented  
**Priority:** High  
**Stage:** Post-MVP Optimization  
**Completed:** December 26, 2025

---

## Executive Summary

Currently, the entire application (FastAPI API + 6 Celery workers) runs from a single Dockerfile with all dependencies (~381 lines of pyproject.toml). This creates:

1. **Bloated Docker images** - Every worker contains ALL dependencies (including those it doesn't need)
2. **Shared secrets exposure** - All workers receive the same `copilot-backend-secret` ConfigMap
3. **Increased RAM footprint** - Workers load unnecessary modules at startup
4. **Slower CI/CD** - Single large image takes longer to build and push

This document outlines a production-grade strategy to separate concerns while maintaining development velocity.

---

## Current Architecture Analysis

### Single Dockerfile Structure

```
Dockerfile (130 lines)
├── Base: python:3.11-slim-bookworm
├── Dependencies: ALL from pyproject.toml (~50+ packages)
├── Components served:
│   ├── FastAPI API (uvicorn)
│   ├── knowledge_worker (docling, transformers, torch, PyMuPDF)
│   ├── chat_worker (minimal - redis, celery)
│   ├── deal_worker (minimal - redis, celery, pandas)
│   ├── integrations_worker (httpx, aiohttp, CRM adapters)
│   ├── organization_worker (minimal - redis, celery)
│   └── content_worker (minimal - redis, celery)
└── Image Size: ~3-4GB (estimated with torch, transformers, docling)
```

### Workers Dependency Analysis

| Worker | Heavy Dependencies | Estimated Deps Size |
|--------|-------------------|---------------------|
| `knowledge_worker` | torch, transformers, docling, PyMuPDF, Pillow, huggingface-hub | ~2.5GB |
| `chat_worker` | redis, celery, sse-starlette | ~200MB |
| `deal_worker` | redis, celery, pandas, SQLAlchemy | ~300MB |
| `integrations_worker` | httpx, aiohttp, redis, celery | ~250MB |
| `organization_worker` | redis, celery, supabase | ~200MB |
| `content_worker` | redis, celery | ~150MB |
| `API (FastAPI)` | fastapi, uvicorn, all shared infra | ~400MB |

### Current Secrets Distribution

```yaml
# All components reference the same secret:
global:
  secretName: "copilot-backend-secret"

# Contains ALL secrets:
# - DATABASE_URL, REDIS_PASSWORD
# - AZURE_KEYVAULT_*, AZURE_STORAGE_*
# - OPENAI_API_KEY, HF_TOKEN
# - SUPABASE_URL, SUPABASE_KEY
# - NANGO_*, CRM credentials
# - RESEND_API_KEY
```

---

## Proposed Solutions

### Option A: Multi-Stage Dockerfile with Build Targets (Recommended for Post-MVP)

**Concept:** Single Dockerfile with multiple build targets for each component.

```dockerfile
# Base stage with shared dependencies
FROM python:3.11-slim-bookworm AS base
# Install: redis, celery, sqlalchemy, pydantic, httpx (shared by all)

# API stage
FROM base AS api
# Add: fastapi, uvicorn, sse-starlette

# Knowledge worker stage (heavy)
FROM base AS knowledge-worker
# Add: torch, transformers, docling, PyMuPDF, huggingface-hub

# Lightweight worker stage
FROM base AS light-worker
# Minimal additions for chat, deal, organization, content workers

# Integrations worker stage
FROM base AS integrations-worker
# Add: aiohttp, CRM-specific adapters
```

**Build Commands:**

```bash
docker build --target api -t copilot-backend-api:latest .
docker build --target knowledge-worker -t copilot-backend-knowledge:latest .
docker build --target light-worker -t copilot-backend-workers:latest .
```

**Pros:**

- Single source of truth (one Dockerfile)
- Shared base layer for caching
- Easy to maintain
- CI/CD can build all targets in parallel

**Cons:**

- Still requires pyproject.toml modifications for optional deps
- All targets share same Git context

---

### Option B: Monorepo with Separate pyproject.toml Files

**Concept:** Create separate dependency files per worker type.

```
copilot-backend/
├── pyproject.toml           # Base shared dependencies
├── docker/
│   ├── api.Dockerfile
│   ├── knowledge-worker.Dockerfile
│   └── light-worker.Dockerfile
├── requirements/
│   ├── base.txt             # Shared: redis, celery, sqlalchemy, pydantic
│   ├── api.txt              # API-specific: fastapi, uvicorn
│   ├── knowledge.txt        # Heavy: torch, transformers, docling
│   ├── integrations.txt     # CRM adapters, httpx
│   └── workers.txt          # Minimal for chat, deal, org workers
```

**Pros:**

- Maximum control over dependencies per image
- Smallest possible images
- Clear separation of concerns

**Cons:**

- Multiple files to maintain
- Dependency sync between files needed
- More complex CI/CD

---

### Option C: UV Workspace (Recommended for Future Scale)

**Concept:** Use UV's workspace feature to create a monorepo with packages.

```
copilot-backend/
├── pyproject.toml           # Root workspace config
├── packages/
│   ├── core/                # Shared domain, infra, services
│   │   └── pyproject.toml
│   ├── api/                 # FastAPI application
│   │   └── pyproject.toml
│   ├── workers/
│   │   ├── knowledge/       # Knowledge worker
│   │   │   └── pyproject.toml
│   │   └── light/           # All lightweight workers
│   │       └── pyproject.toml
```

**Pros:**

- Native UV support
- Clean dependency isolation
- Easy local development
- Type-safe imports between packages

**Cons:**

- Significant restructure required
- Learning curve for UV workspaces
- Migration effort

---

## Secrets Separation Strategy

### Current Problem

All workers receive ALL secrets via:

```yaml
envFrom:
  - secretRef:
      name: {{ .Values.global.secretName }}
```

### Proposed Solution: Scoped Secrets

Create worker-specific secrets:

```yaml
# charts/application/values.yaml
secrets:
  api:
    - DATABASE_URL
    - REDIS_PASSWORD
    - AZURE_KEYVAULT_URL
    - OPENAI_API_KEY
    - SUPABASE_URL
    - SUPABASE_KEY
    
  knowledge-worker:
    - DATABASE_URL
    - REDIS_PASSWORD
    - AZURE_STORAGE_ACCOUNT
    - AZURE_STORAGE_KEY
    - HF_TOKEN
    
  integrations-worker:
    - DATABASE_URL
    - REDIS_PASSWORD
    - NANGO_SECRET_KEY
    
  light-workers:  # chat, deal, organization, content
    - DATABASE_URL
    - REDIS_PASSWORD
```

**Implementation:**

1. Create separate Kubernetes Secrets per worker type
2. Update worker Helm templates to reference specific secrets
3. Use Azure Key Vault with Workload Identity (already partially implemented)

---

## Development Impact Assessment

### Local Development Concerns

| Concern | Impact | Mitigation |
|---------|--------|------------|
| Multiple Dockerfiles | Need to build correct image | Use docker-compose profiles |
| Missing dependencies | Worker import fails | Create unified dev pyproject.toml |
| Testing across workers | Integration tests need all deps | Keep dev/test dependency group |
| IDE support | Type checking needs all packages | UV workspace handles this |

### Recommended Development Setup

```yaml
# docker-compose.dev.yml
services:
  # For development, use single full image
  api:
    build:
      context: .
      dockerfile: Dockerfile
      target: dev  # Includes ALL dependencies
    
  # Or use profiles for specific worker testing
  knowledge-worker:
    profiles: ["knowledge"]
    build:
      target: knowledge-worker
```

---

## Implementation Roadmap

### Phase 1: Dependency Grouping (Week 1-2)

**Goal:** Organize pyproject.toml with optional dependency groups

**Status:** ✅ COMPLETED

**Tasks:**

- [x] Analyze import dependencies per worker
- [x] Create optional dependency groups in pyproject.toml
- [x] Test workers with minimal dependencies locally
- [x] Document which workers need which groups

**pyproject.toml Changes (IMPLEMENTED):**

```toml
# Base dependencies - shared by all components
[project]
dependencies = [
    # Core: redis, celery, sqlalchemy, pydantic, httpx, azure-identity
]

[project.optional-dependencies]
# API: fastapi, uvicorn, openai, agno, etc.
api = [...]

# Knowledge Worker: torch, transformers, docling, etc.
knowledge = [...]

# Light Workers: pandas, supabase, openai
workers-light = [...]

# Integrations: aiohttp, pandas
integrations = [...]

# Full: All dependencies (development)
full = [...]
```

### Phase 2: Multi-Stage Dockerfile (Week 2-3)

**Goal:** Create build targets for different images

**Status:** ✅ COMPLETED

**Tasks:**

- [x] Create base stage with shared dependencies
- [x] Create API stage
- [x] Create knowledge-worker stage (heavy)
- [x] Create light-workers stage
- [x] Update CI/CD to build multiple images
- [x] Test each image independently

**Build Commands (IMPLEMENTED):**

```bash
# Build specific targets
docker build --target api -t copilot-backend-api:latest .
docker build --target knowledge-worker -t copilot-backend-knowledge:latest .
docker build --target light-worker -t copilot-backend-workers:latest .
docker build --target integrations-worker -t copilot-backend-integrations:latest .
docker build --target full -t copilot-backend-full:latest .

# Use the build script
./scripts/deployment/docker-build.sh api latest
./scripts/deployment/docker-build.sh all latest --push
```

### Phase 3: Helm Chart Updates (Week 3-4)

**Goal:** Update Kubernetes deployments for new images

**Status:** 🔜 PENDING (Optional for production deployment)

**Tasks:**

- [ ] Update values.yaml with image per worker type
- [ ] Create separate secrets per worker type
- [ ] Update KEDA scalers for new image names
- [ ] Test deployments in dev environment

### Phase 4: Secrets Scoping (Week 4-5)

**Goal:** Implement least-privilege secrets access

**Status:** 🔜 PENDING (Optional for security hardening)

**Tasks:**

- [ ] Define secrets required per worker
- [ ] Create SecretProviderClass per worker type
- [ ] Update Helm templates
- [ ] Verify workload identity bindings
- [ ] Audit secret exposure

---

## Local Development Setup (IMPLEMENTED)

### Docker Compose

A `docker-compose.yml` file has been created with two profiles:

```bash
# Default: Full image for all services (development)
docker-compose up

# Separated: Individual images per component type  
docker-compose --profile separated up

# With monitoring (Flower)
docker-compose --profile monitoring up
```

### Build Script

Use `scripts/deployment/docker-build.sh` for building images:

```bash
# Build single target
./scripts/deployment/docker-build.sh api latest

# Build and push
./scripts/deployment/docker-build.sh knowledge-worker v1.0.0 --push

# Build all targets
./scripts/deployment/docker-build.sh all latest
```

---

## Resource Comparison (Estimated)

### Current State (Single Image)

| Component | Image Size | Base RAM | Peak RAM |
|-----------|-----------|----------|----------|
| API | 3.5GB | 1.5GB | 3GB |
| knowledge-extract-worker | 3.5GB | 2GB | 28GB |
| knowledge-upload-worker | 3.5GB | 1GB | 2GB |
| chat-worker | 3.5GB | 800MB | 2GB |
| deal-worker | 3.5GB | 600MB | 1.5GB |
| integrations-worker | 3.5GB | 1GB | 4GB |
| organization-worker | 3.5GB | 500MB | 1GB |

### Target State (Separated Images)

| Component | Image Size | Base RAM | Peak RAM |
|-----------|-----------|----------|----------|
| API | 800MB | 400MB | 1.5GB |
| knowledge-worker | 3GB | 1.5GB | 28GB |
| light-workers | 400MB | 300MB | 1GB |
| integrations-worker | 500MB | 400MB | 2GB |

**Estimated Savings:**

- **Total Image Storage:** ~15GB → ~5GB (66% reduction)
- **Base RAM per light worker:** ~1GB → ~300MB (70% reduction)
- **CI/CD Build Time:** Single 15min → Parallel 5-8min each

---

## Risks and Mitigations

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| Shared code import failures | Medium | High | Comprehensive testing, gradual rollout |
| CI/CD complexity increase | Medium | Medium | Parallel builds, GitHub Actions matrix |
| Dev environment divergence | Low | High | Maintain unified dev Dockerfile |
| Secrets sync issues | Low | High | Infrastructure as Code, audit scripts |

---

## Success Criteria

1. **Image Size:** Each worker image < 1GB (except knowledge-worker)
2. **Startup Time:** Light workers start in < 30 seconds
3. **RAM Usage:** Light workers use < 500MB at idle
4. **Secrets:** Each worker only receives required secrets
5. **CI/CD:** Build time < 10 minutes for all images
6. **Development:** Single command starts all services locally

---

## Recommendation

**For Post-MVP Stage:** Start with **Option A (Multi-Stage Dockerfile)** because:

1. **Minimal disruption** - Single file to manage
2. **Quick wins** - Immediate image size reduction
3. **Reversible** - Easy to consolidate if needed
4. **Progressive** - Can evolve to Option C later

**Next Steps:**

1. Approve this plan
2. Start Phase 1: Dependency analysis and grouping
3. Create proof-of-concept for knowledge-worker separate image

---

## Questions for Discussion

1. Should we prioritize image size reduction or secrets separation first?
2. What's the acceptable build time for CI/CD?
3. Should knowledge-worker remain a separate image even in MVP?
4. Do we need separate images for chat vs deal vs organization workers, or can they share?

---

## Appendix: Current Workers Summary

| Worker | Queue(s) | Celery App | Key Dependencies |
|--------|----------|------------|------------------|
| knowledge-extract-worker | knowledge_extract | knowledge_worker | torch, docling, transformers |
| knowledge-upload-worker | knowledge_upload | knowledge_worker | torch, docling, transformers |
| chat-worker | attachments, chat_processing, chat_messages | chat_worker | sse-starlette |
| deal-worker | deal, deal_jobs | deal_worker | pandas |
| deal-beat | deal (scheduler) | deal_worker | pandas |
| integrations-worker | integrations, integrations_priority | integrations_worker | aiohttp, httpx |
| organization-worker | organization | organization_worker | supabase |
