# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

CopilotGTM Backend is an enterprise B2B SaaS application for deal management and AI-powered sales insights. Built with Python 3.11, FastAPI, PostgreSQL (with pgvector), Redis, and Celery.

## Essential Commands

```bash
# Install dependencies
uv sync --all-extras

# Run development server
uv run uvicorn src.main:app --reload --host 0.0.0.0 --port 8000

# Code quality
uv run ruff check src/ tests/              # Lint
uv run ruff check --fix src/ tests/        # Fix lint issues
uv run mypy src/                           # Type check

# Testing
uv run pytest tests/ -v                    # All tests
uv run pytest tests/path/to/test.py -v    # Single file
uv run pytest tests/ -v -k "test_name"    # Single test by name

# Database migrations
uv run python -m scripts.run_master_migrations      # Master schema (public)
uv run python -m scripts.run_org_migrations         # Organization schemas

# Docker development
docker-compose up -d                       # Start all services
docker-compose up api redis deal-worker   # Start specific services
```

## Documentation by Task

**Before implementing, read the relevant guide:**

| Task | Documentation File |
|------|-------------------|
| Building an API endpoint | `docs/Technical Documentation/service-architecture-base.md` |
| Creating a Celery worker | `docs/Technical Documentation/celery-worker-creation-guide.md` |
| Scheduled tasks (all orgs) | See **Three-Tier Architecture** in celery guide (Section 6.5) |
| Adding/modifying endpoints | Update relevant file in `docs/API Documentation/` |

## Architecture

### Multi-Tenant Design
Schema-based multi-tenancy where each organization gets an isolated PostgreSQL schema (`org_*`). The `public` schema contains global data (organisations, users, super_admins).

### Directory Structure
```
src/
├── api/              # FastAPI routes, controllers, schemas
├── agents/           # AI agent orchestration and tools
├── domain/models/    # SQLModel data models
│   ├── master_tables/       # Global tables (public schema)
│   └── organisation_tables/ # Tenant tables (org_* schemas)
├── service/          # Business logic services
├── infra/            # Infrastructure (DB, auth, storage, messaging)
└── workers/          # Celery worker applications
```

### Celery Workers
Six workers: `knowledge_worker`, `chat_worker`, `deal_worker`, `integrations_worker`, `organization_worker`, `content_worker`. Each has: `celery_app.py`, `config.py`, `client.py`, and `tasks/` directory.

### Database Migrations
```
migrations/
├── master_migrations/    # Alembic for public schema
└── org_migrations/       # Alembic for org_* schemas
```

## CRITICAL: Organization Data Isolation

**All code MUST enforce strict organization isolation:**

1. **Always filter by `organisation_id`** - Every database query must include organization filter
2. **Never trust external IDs alone** - Even if an agent or API receives IDs from another organization, queries must verify ownership
3. **Agents cannot access cross-org data** - Tools must validate that requested resources belong to the current organization context
4. **Scope queries properly** - Filter by both `organisation_id` and `user_id` where applicable

## CRITICAL: Database Connection Management

**Database connections are a limited resource. Acquire, use, and release promptly:**

1. **Release before long-running external calls** - For LLM API calls, slow external services, or unpredictable latency operations, release the session first. Short/quick API calls (< 1-2 seconds) are fine to hold.
2. **Use fresh session pattern in workers** - Especially for batch operations or when making multiple external calls
3. **Don't let sessions span long operations** - Close sessions when work is done

## Code Style

- Line length: 100 characters
- Formatter/Linter: Ruff
- Type hints required; MyPy for type checking
- Pre-commit hooks: `uv run pre-commit run --all-files`

## External Services

- **Supabase**: JWT authentication
- **Azure OpenAI**: LLM integration
- **Azure Blob Storage**: File storage (per-org containers)
- **Azure Key Vault**: Tenant secrets (DB passwords)
- **Nango**: OAuth provider management
- **Redis**: Message broker, caching, SSE streaming

## API Documentation Files

When adding/modifying endpoints, update the relevant file in `docs/API Documentation/`:
- `chat-api.md`, `chat-attachments-api.md`
- `knowledge-base-api.md`
- `integrations-api.md`
- `users-auth-api.md`
- `deals/` (deal-related endpoints)
- `admin/` (admin endpoints)
