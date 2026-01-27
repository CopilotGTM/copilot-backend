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

**Note on Agent-Calling Tasks**: Tasks that call AI agents (LLM workflows) do NOT follow the three-tier architecture unless specifically requested. These tasks operate at the deal+meeting level or deal level directly, with business logic extracted to service classes.

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

Five workers: `knowledge_worker`, `chat_worker`, `deal_worker`, `integrations_worker`, `organization_worker`. Each has: `celery_app.py`, `config.py`, `client.py`, and `tasks/` directory.

### Automated Pipeline User ID

Jobs created by automated pipelines (not user-triggered) use a sentinel user ID:

```python
AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
```

This distinguishes automated jobs from user-initiated ones. Check `job.user_id == AUTOMATED_PIPELINE_USER_ID` or `job.job_metadata.get("source") == "automated_pipeline"` to identify automated jobs.

### Database Migrations

```
migrations/
├── master_migrations/    # Alembic for public schema
└── org_migrations/       # Alembic for org_* schemas
```

### Investigating Database Data

To debug issues, use investigation scripts in `scripts/test_local/`:

```bash
# Investigate transcript/meeting data
uv run python -m scripts.test_local.investigate_transcript_data

# Investigate timeline data
uv run python -m scripts.test_local.investigate_timeline_data
```

**Multi-Tenant Query Pattern:**

```python
from src.infra.utils.resource_naming import get_schema_name

ORG_ID = "3ed09720-971e-4fb1-a1d9-bd5e57b432f1"
SCHEMA_NAME = get_schema_name(ORG_ID)  # → org_3ed09720_971e_4fb1_a1d9_bd5e57b432f1

# Set search_path to include org schema
connect_args = {"options": f"-csearch_path={SCHEMA_NAME},public"}

# Then query tables directly (no schema prefix needed)
# Org tables: calendar_events, meetings, crm_deals, etc.
# Public tables: organisations, users (access via public.tablename)
```

**Key Tables by Schema:**

| Schema | Tables |
|--------|--------|
| `public` | organisations, users, super_admins |
| `org_*` | crm_deals, calendar_events, meetings, calendar_event_deal_associations, knowledge_integrations, deal_jobs |

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

## CRITICAL: Agent Architecture

**Agents follow a fresh session pattern with immutable security boundaries:**

### Security Boundaries (Immutable)

All agents receive `organisation_id` and optionally `deal_id` at initialization. These are baked into the `AgentContext` and **cannot be modified** by tools or prompts:

```python
# Example: Creating an agent with security boundaries
agent = SummaryAgent(
    organisation_id=org_uuid,  # REQUIRED - security boundary
    deal_id=deal_uuid,         # REQUIRED for deal-scoped agents
    publisher=publisher,       # Optional - for SSE progress updates
)
```

### Agent Parameters

| Agent | organisation_id | deal_id | Notes |
|-------|-----------------|---------|-------|
| `SummaryAgent` | Required | Required | Meeting summary generation |
| `DealSummaryAgent` | Required | Required | Comprehensive deal summary |
| `RiskBlockerExtractionAgent` | Required | Required | Extract risks/blockers from deal |
| `BriefingAgent` | Required | Required | Meeting preparation briefing |
| `StakeholderAnalysisAgent` | N/A | N/A | Stateless - receives context string |
| `GeneralAgent` | Required | Optional | Chat agent - deal_id optional |

### Fresh Session Pattern

Agents do NOT hold database sessions during LLM calls:

- Tools acquire sessions on-demand via `context.acquire_session()`
- Sessions are released before any long-running operations (LLM calls)
- This prevents connection pool exhaustion during slow LLM responses

```python
# WRONG: Passing session to agent
agent = MyAgent(session=db_session)  # DON'T DO THIS

# CORRECT: Agent acquires sessions via context
agent = MyAgent(organisation_id=org_id, deal_id=deal_id)
# Tools use: async for session in context.acquire_session(): ...
```

### Agent Model Configuration

Agent models are configured in `src/infra/config/agent_config.py`. Configuration can be overridden via environment variables:

```bash
# Example: Override model for deal summary agent
AGENT_DEAL_SUMMARY_MODEL=gpt-5-mini
AGENT_DEAL_SUMMARY_DEBUG=false
AGENT_DEAL_SUMMARY_TIMEOUT=180
```

## Code Style

- Line length: 100 characters
- Formatter/Linter: Ruff
- Type hints required; MyPy for type checking
- Pre-commit hooks: `uv run pre-commit run --all-files`

**Note**: Only run `ruff check` and `mypy` when pushing code or creating a PR. Avoid running them after every step as it hampers development flow.

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
