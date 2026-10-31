# CopilotGTM Backend

**Project status:** CopilotGTM is no longer actively maintained. This code is shared
as a reference. No ongoing support or security updates are planned.

[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.116.1-009688.svg)](https://fastapi.tiangolo.com)
[![UV](https://img.shields.io/badge/uv-package%20manager-blue)](https://github.com/astral-sh/uv)

CopilotGTM Backend is the enterprise B2B SaaS backend for deal management,
CRM/integration sync, knowledge workflows, and AI-powered sales insights.

The application is built with Python 3.11, FastAPI, PostgreSQL with pgvector,
Redis, Celery workers, SQLModel/SQLAlchemy, Alembic, Supabase auth, Azure
services, and Agno/OpenAI-based agent workflows.

## Quick Start

### Prerequisites

- Python 3.11
- uv
- Docker and Docker Compose
- PostgreSQL 16 with pgvector
- Redis 6+

### Install Dependencies

```bash
uv sync --all-extras
```

Use `uv sync --all-extras` for development so local tools, tests, and worker
dependencies are available.

### Start Local Infrastructure

```bash
docker run -d --name copilot-postgres -p 5432:5432 -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=postgres pgvector/pgvector:pg16
docker run -d --name copilot-redis -p 6379:6379 redis:7-alpine
```

Or start the repository Compose stack:

```bash
docker-compose up -d
```

### Configure Environment

Create `.env` in the project root. Common local variables:

```bash
DATABASE_HOST=localhost
DATABASE_PORT=5432
DATABASE_USER=postgres
DATABASE_PASSWORD=postgres
DATABASE_NAME=postgres

SUPER_ADMIN_EMAIL=admin@yourcompany.com

SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your-anon-key
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
SUPABASE_JWT_SECRET=your-jwt-secret

# Local/dev may temporarily reuse the Google integration OAuth client for login.
GOOGLE_CLIENT_ID=your-google-integration-client-id
GOOGLE_CLIENT_SECRET=your-google-integration-client-secret
GOOGLE_LOGIN_USE_INTEGRATION_CREDENTIALS=true

# When using a dedicated login client outside production, these override the alias.
# GOOGLE_LOGIN_CLIENT_ID=your-google-login-client-id
# GOOGLE_LOGIN_CLIENT_SECRET=your-google-login-client-secret

RESEND_API_KEY=your-resend-api-key
FROM_EMAIL=noreply@yourdomain.com

EXA_API_KEY=your-exa-api-key

APP_NAME=CopilotGTM
APP_URL=https://your-app-domain.com

AZURE_OPENAI_ENDPOINT=https://your-endpoint.openai.azure.com/
AZURE_OPENAI_API_KEY=your-api-key
```

### Run Migrations

```bash
uv run python -m scripts.run_master_migrations
uv run python -m scripts.run_org_migrations
```

Terraform owns the permanent `tenant_data` schema in dev and prod. Run the
existing organisation migration chain against that schema explicitly:

```bash
uv run python -m scripts.run_org_migrations --schema tenant_data --precheck
uv run python -m scripts.run_org_migrations --schema tenant_data
```

For an isolated migration test, create an empty schema and pass its safe,
lowercase name through the same `--schema` option. Schema targets are command
arguments, not environment configuration.

Deploy the configured super admin when needed:

```bash
uv run python -m scripts.admin.deploy_with_super_admin
```

### Run the API

```bash
uv run uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
```

Production-style local run:

```bash
uv run uvicorn src.main:app --host 0.0.0.0 --port 8000 --workers 4
```

## Essential Commands

```bash
uv sync --all-extras
uv run uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
uv run pytest tests/ -v
uv run pytest tests/path/to/test.py -v
uv run pytest tests/ -v -k "test_name"
uv run ruff check src/ tests/
uv run ruff check --fix src/ tests/
uv run mypy src/
```

Only run broad lint/type checks when preparing a PR or when the touched code
makes them relevant. Prefer targeted tests while iterating.

## Architecture

CopilotGTM is a shared-row, RLS-enforced multi-tenant backend.

- `public` stores master/control-plane data such as organisations, users, and
  super admins; access it through master session functions.
- `tenant_data` stores every organisation's rows; access it through
  organisation session functions, which set transaction-local tenant identity.
- PostgreSQL forced row-level security is the database isolation boundary, with
  tenant-qualified constraints providing relational integrity.
- API routes should remain thin and delegate business logic to services.
- Workers own long-running asynchronous jobs.
- Agents are initialized with immutable security boundaries and acquire sessions on demand.

### Source Layout

```text
src/
├── api/              FastAPI routes, dependencies, events, schemas
├── agents/           AI agent orchestration, tools, workflows
├── domain/models/    SQLModel data models
│   ├── master_tables/        Public schema tables
│   └── organisation_tables/  Tenant schema tables
├── service/          Business logic services
├── infra/            Database, auth, config, storage, messaging, observability
└── workers/          Celery worker applications
```

### Workers

The backend has these Celery worker areas:

- `knowledge_worker`
- `chat_worker`
- `deal_worker`
- `integrations_worker`
- `organization_worker`

Each worker follows the local worker pattern with `celery_app.py`, `config.py`,
`client.py`, and task modules.

### Migrations

```text
migrations/
├── master_migrations/    Alembic migrations for public schema
└── org_migrations/       Alembic chain run explicitly against tenant_data or an isolated test schema
```

## Critical Rules

### Organization Isolation

Every tenant data path must enforce organization isolation.

- Always scope tenant queries by the current `organisation_id`.
- Never trust external IDs alone.
- Validate resource ownership before returning or mutating tenant data.
- Agent tools must not access cross-organization data.
- Scope by both `organisation_id` and `user_id` where the domain requires it.

### Database Sessions

Database connections are limited. Acquire, use, and release promptly.

- Do not hold sessions across LLM calls, slow external services, or unpredictable network calls.
- Use the existing fresh-session pattern in agents and workers.
- Extract needed values, release the session, then perform long-running work.

### Agent Boundaries

Agents receive security boundaries at initialization.

```python
agent = SummaryAgent(
    organisation_id=org_uuid,
    deal_id=deal_uuid,
    publisher=publisher,
)
```

Do not pass a long-lived database session into an agent. Tools should acquire
sessions on demand through the agent context.

Agent model configuration lives in `src/infra/config/agent_config.py` and can be
overridden through environment variables, for example:

```bash
AGENT_DEAL_SUMMARY_MODEL=gpt-5-mini
AGENT_DEAL_SUMMARY_DEBUG=false
AGENT_DEAL_SUMMARY_TIMEOUT=180
```

### Agent Tooling

Agno injects tool definitions into model context. Keep tool descriptions as the
primary source of tool behavior and keep workflow prompts strict only where a
tool call is mandatory.

Single source of truth:

- Tool availability: `src/agents/tools/registry.py`
- Tool behavior: tool function docstrings and descriptions
- Workflow constraints: `src/agents/workflows/*/prompts.py`

## Documentation By Task

Read the relevant guide before implementing:

| Task | Documentation |
| --- | --- |
| Building or changing an API endpoint | `docs/Technical Documentation/service-architecture-base.md` |
| Creating a Celery worker | `docs/Technical Documentation/celery-worker-creation-guide.md` |
| Scheduled tasks across organizations | Three-Tier Architecture section in the Celery worker guide |
| Adding or changing endpoints | Relevant file under `docs/API Documentation/` |
| Integration sync behavior | `docs/Technical Documentation/integrations/` |

Agent-calling tasks do not automatically follow the three-tier worker
architecture unless explicitly requested. They usually operate at deal,
meeting, or deal-plus-meeting scope, with business logic extracted to services.

## Code Style

- Python 3.11
- Line length: 100
- Ruff for linting and formatting compatibility
- MyPy for type checking
- Type hints required for new/changed code where practical
- Comments explain why, not what
- Put endpoint request/response docs in the matching `docs/API Documentation/` file

Comment useful business decisions, security constraints, non-obvious
performance choices, and caller context. Avoid comments that restate obvious
code.

## External Services

- Supabase: JWT authentication
- Azure OpenAI: LLM integration
- Azure Blob Storage: file storage
- Azure Key Vault: tenant and infrastructure secrets
- Nango: OAuth provider management
- Redis: broker, caching, SSE streaming
- Resend: email delivery
- Grafana Loki: log querying

## Investigating Data

Use local investigation scripts for debugging:

```bash
uv run python -m scripts.test_local.investigate_transcript_data
uv run python -m scripts.test_local.investigate_timeline_data
```

Use the existing master or organisation session boundary when investigating
database data. Organisation sessions select `tenant_data, extensions` and set
transaction-local `app.current_organisation_id`; do not derive an `org_*`
schema or set a tenant search path manually.

Direct operator SQL is restricted to controlled audits and cutover work. Follow
`docs/Technical Documentation/shared-row-tenancy-operations-runbook.md`, use the
appropriate database role, and never disable or bypass RLS.

## Docker

Build the application image:

```bash
docker build -t copilot-backend:latest .
```

Run with an env file:

```bash
docker run -p 8080:80 --env-file .env copilot-backend:latest
```

Deploy through Helm:

```bash
helm upgrade --install copilot-backend ./charts/application \
  --namespace production \
  --values ./charts/application/values.yaml
```

## Logs

Configure `logcli`:

```powershell
$env:LOKI_ADDR="https://logs-prod-021.grafana.net"
$env:LOKI_USERNAME="1510920"
$env:LOKI_PASSWORD="<grafana-cloud-token-with-logs-read>"
```

Tail backend logs:

```powershell
logcli query '{deployment_environment="dev", service_name=~"copilotgtm-backend-.*"}' --tail --since=5m
logcli query '{deployment_environment="prod", service_name=~"copilotgtm-backend-.*"}' --tail --since=5m
```

Tail backend errors:

```powershell
logcli query '{deployment_environment="dev", service_name=~"copilotgtm-backend-.*", detected_level="error"}' --tail --since=1h
logcli query '{deployment_environment="prod", service_name=~"copilotgtm-backend-.*", detected_level="error"}' --tail --since=1h
```

Stop live tailing with `Ctrl+C`.

## Reference Docs

- `docs/API Documentation/`
- `docs/Technical Documentation/`
- `docs/Functional Documentation/`

## License

This project is licensed under the [MIT License](LICENSE).

## Project team

CopilotGTM was built by Nikhil Singhal, Saksham Bhutani, and Mrinmoy Nath.
The commit history records the work of its contributors.

## Related repositories

- [Backend](https://github.com/CopilotGTM/copilot-backend)
- [App frontend](https://github.com/CopilotGTM/copilot-app-frontend)
- [Admin frontend](https://github.com/CopilotGTM/copilot-admin-frontend)
- [Mobile app](https://github.com/CopilotGTM/gtm-mobile-app)
