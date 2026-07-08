# Service Architecture Base: Tenancy, Auth, Storage, Messaging, Workers, Responses (Updated)

Purpose

- Single reference to understand how to add APIs quickly and correctly.
- Reflects current code: new DB providers, Azure Key Vault secrets, Redis (pub/sub + streams), Celery workers.

Quickstart (Checklist)

1. **Scope**: Determine if user routes (tenant) or admin routes (master).
2. **Service**: Create service class with `__init__(session, org_id)` and `close()` method.
3. **Dependency**: Create a dependency function (`get_my_service`) that yields the service with `try/finally` cleanup.
4. **Endpoints**: Inject service via `Depends(get_my_service)`; no direct session handling.
5. **Auth**: Use `get_current_user` dependency to get `UserIDs(user_id, organisation_id)`.
6. **Response**: Wrap all responses with `GenericResponse[T]` and `Response.*` helpers.
7. **Scope queries**: Always filter by `organisation_id` and, where applicable, `user_id`.
8. **Background jobs**: Enqueue Celery tasks; publish updates over Redis; expose SSE if needed.

What To Use (by Concern)

- Tenancy/DB sessions: `src/api/dependencies/organisation_dependencies.py:1` → `get_org_session()` (sync) or `get_org_async_session()` (async). Master scope: `src/api/dependencies/master_dependencies.py:1`.
- DB providers (do not import directly for APIs): `src/infra/database/factory.py:1` (creates singletons for sync/async providers) backed by `provider_sync.py:1` / `provider_async.py:1`.
- Secrets (Azure Key Vault): `src/infra/secrets/manager.py:1` with `src/infra/secrets/azure_key_vault.py:1`. Secret names: `src/infra/utils/resource_naming.py:1`.
- Auth: Users via Supabase in `src/infra/auth/supabase_user_client.py:1` and admin helpers in `src/infra/auth/admin_auth.py:1`. Dependencies surface a `UserIDs` object via `get_current_user()`.
- Storage: `src/infra/storage/azure_org_blob_storage.py:1` → `AzureOrganisationBlobStorage` (per‑org containers + SAS).
- Messaging: Redis client `src/infra/messaging/redis_client.py:1`; Redis Pub/Sub `src/infra/messaging/redis_pubsub.py:1`; Redis Streams `src/infra/messaging/redis_streams.py:1`; channel helpers `src/infra/messaging/redis_channels.py:1`.
- SSE services: Knowledge `src/service/knowledge_base/knowledge_sse_service.py:1`; Attachments `src/service/chat/chat_attachments_sse_service.py:1`; Chat message frames (Streams) `src/service/chat/chat_message_sse_service.py:1`.
- Workers: Celery base `src/workers/base/celery_base.py:1` and config `src/workers/base/worker_config.py:1`; worker apps in `src/workers/*_worker/celery_app.py:1` with routing in each `config.py:1`.
- Response envelope: `src/api/utils/http_response.py:1` and `src/api/schemas/base_models.py:1`.

System Overview (ASCII)

```text
 +--------+    +-----------+    +---------+    +-------------------+
 | Client | -> |   API     | -> |  Auth   | -> | Org/Master Session |
 +--------+    +-----------+    +---------+    +-------------------+
      |                                         |
      |                                         v
      |                                   +-----------+
      |                                   |  Service  |
      |                                   +-----------+
      |                                         |
      |    Presign: generate_write_sas           v
      |   Finalize: get_blob_properties      +--------------+
      |                                      |  Azure Blob  |
      |                                      +--------------+
      |                                             ^
      |                                             |
      |      Celery: post-upload/extract            |
      |                                             |
      |                                    +-----------------------+
      +---- SSE (status/events) <----------| Redis (PubSub/Streams) |
                                           +-----------------------+
```

Tenancy & Database Access

- Master DB (public schema) stores organisations, users, admins.
- Each organisation has its own schema (multi‑tenant) for business tables.
- Providers build per‑tenant connections using per‑tenant DB roles and search_path.
- Use dependencies, not engines, from APIs:
  - Sync: `get_org_session()` / `get_master_session()` return SQLAlchemy sessions.
  - Async: `get_org_async_session()` / `get_master_async_session()` return AsyncSession.
- Providers: `src/infra/database/provider_sync.py:1`, `src/infra/database/provider_async.py:1` with a simple interface (`master_session()`, `tenant_session(org_id)`, `invalidate_tenant()`), instantiated via `src/infra/database/factory.py:1`.

Secrets & Credentials

- Per‑tenant DB passwords are stored in Azure Key Vault and retrieved via `secrets_manager`.
- Azure credential strategy:
  - Local: `DefaultAzureCredential`.
  - Cloud: `WorkloadIdentityCredential` when `AZURE_TENANT_ID` + `AZURE_CLIENT_ID` are set; otherwise `ManagedIdentityCredential`.
- Secret naming: `get_key_vault_secret_name(org_id)` and role/schema naming in `src/infra/utils/resource_naming.py:1`.

Redis, Channels, and Streams

- Configuration: `src/infra/config/configs.py:1` (ENV‑aware host/port/password; TLS in non‑local).
- Sync client: `src/infra/messaging/redis_client.py:1` (`get_redis_client()` returns pooled client).
- Async Pub/Sub: `src/infra/messaging/redis_pubsub.py:1` (`AsyncRedisPubSub` for SSE services).
- Redis Streams: `src/infra/messaging/redis_streams.py:1` (replayable frames for chat).
- Channel helpers: `src/infra/messaging/redis_channels.py:1`.

Redis Client Usage

```python
from src.infra.messaging.redis_client import get_redis_client

client = get_redis_client()  # Always use this, never construct raw clients

# Increment with TTL (rate limiting)
count = client.incr(key)
if count == 1:
    client.expire(key, 60)

# Publish event (sync)
import json
client.publish(channel, json.dumps(payload))
```

Key conventions:

- Rate limits: `rl:{subject}:{id}:...`
- Pub/Sub: `sse:{org}:{topic}:...`
- Streams: `stream:{org}:{type}:{id}`

Connection notes:

- Lazy singleton pool with auto-TLS in non‑local environments.
- Wrap calls in try/except; fail‑open for non‑critical paths, fail‑closed for critical locks.
- `client.ttl(key)`: returns -2 (missing) or -1 (no expiry).

SSE Patterns

- Knowledge sources and attachments: use Redis Pub/Sub to stream `status` updates with heartbeats and completion detection.
  - Knowledge: `src/service/knowledge_base/knowledge_sse_service.py:1`.
  - Attachments: `src/service/chat/chat_attachments_sse_service.py:1`.
- Chat message generation: use Redis Streams for structured frames + consumer groups for consume‑once semantics.
  - Chat frames: `src/service/chat/chat_message_sse_service.py:1`.

Workers (Celery)

- Base: `src/workers/base/celery_base.py:1` creates a Celery app per worker using broker/backend from `BaseWorkerConfig`.
- Redis is the broker + result backend; TLS is enabled automatically in non‑local environments.
- Worker configs define queues/exchanges/routing:
  - Chat: `src/workers/chat_worker/config.py:1`.
  - Knowledge: `src/workers/knowledge_worker/config.py:1`.
  - Organization: `src/workers/organization_worker/config.py:1`.
- Enqueue from API using `send_task(name, args, queue, routing_key)` against the appropriate worker app.

Celery Enqueue Example

```python
from src.workers.chat_worker.celery_app import app as chat_app
chat_app.send_task(
    "attachments.post_upload",
    args=[str(org_id), str(item_id)],
    queue="attachments",
    routing_key="attachments",
)
```

AuthN/AuthZ

- User routes: verify Supabase JWT and surface `UserIDs(user_id, organisation_id)` via `get_current_user()` in `src/api/dependencies/organisation_dependencies.py:1`.
- Admin routes: use master session dependencies in `src/api/dependencies/master_dependencies.py:1` and enforce admin permissions.

Storage Model

- One storage account per environment; one container per organisation.
- Deterministic blob paths (examples):
  - Chat: `chat_uploads/{user_id}/{id}/file.ext`
  - Knowledge: `knowledgebase/{user_id}/{id}/file.ext`
- SAS URLs:
  - Write SAS (client upload): short TTL (≈10–15 min).
  - Read SAS (download): TTL 30–120 min; set Content‑Disposition filename when needed.

Response Envelope

- Shape: `{ status: SUCCESS|ERROR, data, message }`.
- Helpers: `Response.success_response`, `Response.accepted_response`, `Response.error_response`, etc. in `src/api/utils/http_response.py:1`.
- Type your responses with `GenericResponse[T]` in `src/api/schemas/base_models.py:1`.

Service Dependency Pattern (IMPORTANT)

**Always create services as FastAPI dependencies with proper cleanup.** This ensures database connections are properly released and prevents connection pool exhaustion.

Why this pattern:

- Prevents DB connection leaks across requests.
- Ensures `session.close()` is called even if the endpoint raises.
- Keeps endpoint code clean and focused on business logic.

Service class requirement:

```python
# src/service/my_domain/my_service.py
class MyService:
    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id

    async def close(self) -> None:
        """Close the database session."""
        await self.session.close()

    async def do_something(self, payload) -> MyResult:
        # business logic here
        ...
```

Handler pattern (create dependency, use in endpoints):

```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs, get_current_user, get_org_async_session,
)
from src.api.schemas.base_models import GenericResponse
from src.api.utils.http_response import Response
from src.infra.utils.logger import get_logger
from src.service.my_domain.my_service import MyService, MyServiceError

router = APIRouter()
logger = get_logger(__name__)


# 1. Define service dependency with cleanup
async def get_my_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields a MyService instance for the current org."""
    service = MyService(session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("MyService close failed", exc_info=True)


# 2. Use service dependency in endpoints (clean, no session management)
@router.post("/example", response_model=GenericResponse[MyResponseModel])
async def example(
    payload: MyRequestModel,
    service: MyService = Depends(get_my_service),
):
    """Endpoint uses injected service; cleanup handled by dependency."""
    try:
        result = await service.do_something(payload)
        return Response.success_response("Operation successful", result)
    except MyServiceError as e:
        return Response.error_response(e.status_code, e.message)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in example endpoint")
        return Response.internal_server_error_response("An unexpected error occurred")
```

Key points:

- Service dependency uses `try/finally` to guarantee cleanup.
- Endpoint receives a ready-to-use service instance.
- Multiple endpoints can share the same service dependency.
- For sync routes, use `get_org_session()` and a sync `close()` method.

CRM Service Package Rule

- New CRM domain services must live in domain packages under `src/service/crm/<domain>/`, for example `src/service/crm/leads/`, `src/service/crm/deals/`, and `src/service/crm/notes/`.
- `src/service/crm/access/` is deprecated for new CRM code. It may contain temporary compatibility imports during migrations, but routers and tests should import the real domain package.
- Routers own FastAPI wiring only: route declarations, dependencies, rate limits, auth/scope checks, service calls, rollbacks, and returning response helpers.
- API presenters under `src/api/presenters/crm/` own response DTO construction, public/private error body construction, URL construction, and structured log payload construction.
- Services own domain behavior, database reads/writes, validation at service trust boundaries, idempotency, and cross-domain orchestration.
- Lead-specific target architecture is documented in `docs/Technical Documentation/deal/leads-service-architecture.md`.

Reusable Permission Rules

- Always filter by `organisation_id` and `user_id` when data is user‑owned.
- Exclude soft‑deleted records by default.
- Downloads require READY/COMPLETED status and a valid `storage_path`.
- SSE restricts to caller’s items or explicit ID lists.
- Admin routes: master session only; never write tenant data.

Publish/Consume Contracts (Examples)

```python
# Publish attachment status from a worker
from src.infra.messaging.attachment_status_publish import publish_attachment_status
publish_attachment_status(org_id, AttachmentEventUpdate(...))

# Stream knowledge status in API using pub/sub
from src.infra.messaging.redis_pubsub import AsyncRedisPubSub
async for message in AsyncRedisPubSub().subscribe_to_channel(
    channel_for_knowledge_update_status(org_id)
):
    yield {"event": "status", "data": json.dumps([message])}

# Produce chat frames using Redis Streams (worker)
from src.infra.messaging.redis_streams import xadd_json_frame
xadd_json_frame(stream_key, {"type": "delta", "data": {"content": "..."}})
```

Local Dev Notes

- ENV=local prefers non‑TLS Redis and `DefaultAzureCredential` for secrets.
- Run: Postgres + Redis + Celery workers + FastAPI app (`src/api/fastapi_app.py:1`).
- Common env vars (see `.env`): DB host/port/name/user/pass, Redis host/port/password, `AZURE_KEY_VAULT_URL`, storage names.
- If Azure auth fails locally, ensure you’re logged in (e.g., az login) or configure env secrets.

Observability

- Loggers to tail: `azure_org_blob_storage`, `redis_pubsub`, workers (`attachments_post_upload_task`, `knowledge_*`), routers (`upload_handler`, `knowledge_*`).
- SSE issues: watch server logs for heartbeat/timeout events; verify Redis channel names per org.

API Surface (FastAPI)

- Public: `GET /health` (load balancer probe) in `src/api/routers/healthcheck.py:25`.
- Internal: `GET /internal/healthcheck`, `GET /internal/health/detailed`, `GET /internal/health/secrets` in `src/api/routers/healthcheck.py:46`.
- User Auth: `POST /users/auth/login`, `POST /users/auth/reset-password`, `POST /users/auth/refresh-token`, `POST /users/auth/forgot-password` in `src/api/routers/users/auth_handler.py:1`.
- Chats (sessions): `POST /users/chats/create_chat`, `DELETE /users/chats/delete_chat`, `PATCH /users/chats/rename_chat`, `GET /users/chats/list_chats`, `GET /users/chats/search_chats`, `PATCH /users/chats/favorite_chat` in `src/api/routers/chat/sessions_handler.py:1`.
- Chats (messages): `POST /users/chats/send_message`, `GET /users/chats/message_events`, `GET /users/chats/messages/list` in `src/api/routers/chat/messages_handler.py:1`.
- Chat Attachments (upload): `POST /users/chats/upload/presign`, `POST /users/chats/upload/finalize`, batch variants under `/presign/batch` and `/finalize/batch`, `GET /users/chats/upload/attachments/stream`, `GET /users/chats/upload/attachment/{id}` in `src/api/routers/chat_attachments/upload_handler.py:1`.
- Chat Attachments (download): `GET /users/chats/upload/attachment/{id}/download` in `src/api/routers/chat_attachments/access_handler.py:1`.
- Knowledge Base (upload + SSE): `POST /users/knowledge/presign/batch`, `POST /users/knowledge/finalize/batch`, `GET /users/knowledge/source/{id}`, `GET /users/knowledge/sources/stream` in `src/api/routers/knowledge_base/upload_handler.py:1`.
- Knowledge Base (access): `GET /users/knowledge/sources`, `GET /users/knowledge/source/{id}/download`, `GET /users/knowledge/sources/stats`, `DELETE /users/knowledge/source/{id}` in `src/api/routers/knowledge_base/access_handler.py:1`.
- Integrations (management): `GET /users/integrations/available`, `GET /users/integrations/{integration_id}` in `src/api/routers/integrations/integration_routes.py:1`.
- Integrations (OAuth): `POST /users/integrations/oauth/authorize`, `DELETE /users/integrations/oauth/disconnect/{integration_id}`, `POST /users/integrations/oauth/webhooks/nango` in `src/api/routers/integrations/oauth_routes.py:1`.
- Admin Auth: `POST /admin/auth/login`, `POST /admin/auth/reset-password`, `POST /admin/auth/refresh-token`, `POST /admin/auth/forgot-password`, `POST /admin/auth/logout`, `POST /admin/auth/change-password` in `src/api/routers/admin/admin_auth_handler.py:1`.
- Admin Organisations: `POST /admin/organisations/create_organisation`, `GET /admin/organisations/list_organisations`, `GET /admin/organisations/countries`, `GET /admin/organisations/all_organisations_history`, `GET /admin/organisations/{organisation_id}`, `PUT /admin/organisations/{organisation_id}/update`, `PUT /admin/organisations/{organisation_id}/status`, `GET /admin/organisations/{organisation_id}/history` in `src/api/routers/admin/organizations_handler.py:1`.
- Admin Users: `POST /admin/users/create_user`, `GET /admin/users/{user_id}/profile`, `GET /admin/users/list_users`, `PUT /admin/users/{user_id}/admin_status`, `GET /admin/users/history/organisation/{organisation_id}`, `PUT /admin/users/{user_id}/update` in `src/api/routers/admin/users_handler.py:1`.
- CRM Deals: `GET /users/crm/deals`, `GET /users/crm/deals/{deal_id}`, `GET /users/crm/deals/stats`, `POST /users/crm/deals/{deal_id}/summary` in `src/api/routers/crm/deals_handler.py:1`.
- CRM Stakeholders: `GET /users/crm/deals/{deal_id}/stakeholders` (list), `GET /users/crm/deals/{deal_id}/stakeholders/{stakeholder_id}` (detail with engagement history), `POST /users/crm/deals/{deal_id}/stakeholders/{stakeholder_id}/approve`, `POST /users/crm/deals/{deal_id}/stakeholders/{stakeholder_id}/reject`, `GET /users/crm/deals/{deal_id}/stakeholders/available-contacts` (compatibility alias over contact search), and `POST /users/crm/deals/{deal_id}/stakeholders` with `contact_id` for selected contacts in `src/api/routers/crm/stakeholders_handler.py:1`.
- CRM Stakeholder Insights: `GET /users/crm/deals/{deal_id}/stakeholder-insights`, `POST /users/crm/deals/{deal_id}/stakeholder-insights/{insight_id}/acknowledge`, `POST /users/crm/deals/{deal_id}/stakeholder-insights/{insight_id}/dismiss`, `POST /users/crm/deals/{deal_id}/stakeholder-insights/{insight_id}/resolve` in `src/api/routers/crm/stakeholder_insights_handler.py:1`.
- CRM Meetings: `GET /users/crm/deals/{deal_id}/meetings`, `PATCH /users/crm/deals/{deal_id}/meetings/{event_id}/status`, `POST /users/crm/deals/{deal_id}/meetings/{event_id}/briefing`, `POST /users/crm/deals/{deal_id}/meetings/{event_id}/summary`, `POST /users/crm/deals/{deal_id}/meetings/{event_id}/followup` in `src/api/routers/crm/meetings_handler.py:1`.
- CRM Deal Notes: `GET /users/crm/deals/{deal_id}/notes`, `POST /users/crm/deals/{deal_id}/notes` in `src/api/routers/crm/deal_notes_handler.py:1`.
- CRM Risks/Blockers: `GET /users/crm/deals/{deal_id}/risks-blockers`, `GET /users/crm/risks-blockers/{risk_blocker_id}`, `POST /users/crm/deals/{deal_id}/risks-blockers`, `PUT /users/crm/risks-blockers/{risk_blocker_id}`, `POST /users/crm/risks-blockers/{risk_blocker_id}/confirm`, `POST /users/crm/risks-blockers/{risk_blocker_id}/dismiss`, `POST /users/crm/risks-blockers/{risk_blocker_id}/resolve`, `POST /users/crm/risks-blockers/{risk_blocker_id}/reopen`, `DELETE /users/crm/risks-blockers/{risk_blocker_id}` in `src/api/routers/crm/risks_blockers_handler.py:1`.

Workers & Tasks (Celery)

- **Base**: `src/workers/base/celery_base.py:1` creates Celery apps; configs in `src/workers/base/worker_config.py:1`.
- **Chat Worker** (`src/workers/chat_worker/`): Exchange `chat`, queues `attachments`, `chat_messages`.
  - `attachments.post_upload` → queue `attachments`
  - `chat.process_message` → queue `chat_messages`
- **Knowledge Worker** (`src/workers/knowledge_worker/`): Exchange `knowledge`, queues `knowledge_upload`, `knowledge_extract`.
  - `knowledge.post_upload_process`, `knowledge.delete_source` → queue `knowledge_upload`
  - `knowledge.document_extraction` → queue `knowledge_extract`
- **Organization Worker** (`src/workers/organization_worker/`): Exchange `organization`, queue `organization`.
  - `organization.create` → queue `organization`
- **Deal Worker** (`src/workers/deal_worker/`): Exchange `deal`, queues `deal`, `deal_jobs`, `deal_mappers`.
  - `risk_blocker.extract_deal_risks_blockers_task` → queue `deal`
  - `stakeholder_tasks.sync_deal_stakeholders_task`, `stakeholder_tasks.sync_org_stakeholders_task` → queue `deal_mappers`
  - `email_matching_tasks.match_emails_to_deal_task`, `email_matching_tasks.match_org_emails_task` → queue `deal_mappers`
  - `calendar_matching_tasks.match_calendar_events_to_deal_task`, `calendar_matching_tasks.match_org_calendar_events_task` → queue `deal_mappers`

Core Utilities (Quick Index)

- Logging: `src/infra/utils/logger.py:1#get_logger(name, ...)` — consistent logger setup.
- Cursors: `src/infra/utils/cursor.py:1#encode_cursor/decode_cursor` — opaque pagination tokens.
- Responses: `src/api/utils/http_response.py:1#Response.*` — uniform API responses.
- Constants (errors/app): `src/infra/config/constant.py:1` — central error/message strings (`GeneralErrors`, `UserAuthErrors`, `AdminAuthErrors`, `AdminOrganizationErrors`, `AdminUserErrors`, `KnowledgeBaseErrors`) and app constants (e.g., `APP_NAME`). Prefer these over literals in routers/services.
- Naming: `src/infra/utils/resource_naming.py:1#get_schema_name/get_role_name/...` — org resource names.
- DB URL: `src/infra/utils/get_master_db_url.py:1#get_master_db_url()` — builds master DB URL.
- Redis: `src/infra/messaging/redis_client.py:1#get_redis_client()` — shared client/pool.
- Streams: `src/infra/messaging/redis_streams.py:1#xadd_json_frame()` — append JSON frames.
- Attach Events: `src/infra/messaging/attachment_status_publish.py:1#publish_attachment_status()` — Pub/Sub.
- KB Events: `src/infra/messaging/knowledge_source_status_publish.py:1#publish_source_status()` — Pub/Sub.
- Storage: `src/infra/storage/azure_org_blob_storage.py:1#AzureOrganisationBlobStorage` — SAS read/write helpers.

Operational Scripts (Selected)

- Database: `scripts/run_master_migrations.py:1`, `scripts/run_org_migrations.py:1` — Alembic migrations (master/tenant).
- Admin: `scripts/admin/init_super_admin.py:1`, `scripts/admin/deploy_with_super_admin.py:1` — bootstrap super admin.
- Monitoring: `scripts/monitoring/db_pool_monitor.py:1` — DB pool health; Celery hooks `scripts/monitoring/celery_integration.py:1`.
- Security: `scripts/security/validate_health_security.py:1` — validates health endpoint access.
- Deployment: `scripts/deployment/local-deploy.ps1:1`, `scripts/startup.sh:1` — local/container startup.

Service Layer Index

- Chat Sessions: `src/service/chat/chat_session_service.py:1` — create, rename, list, favorite, delete sessions.
- Chat Messages: `src/service/chat/chat_message_service.py:1` — persist user messages, enqueue jobs, list/enrich history.
- Chat Message SSE: `src/service/chat/chat_message_sse_service.py:1` — consume Redis Streams per message job.
- Attachments: `src/service/chat/chat_attachments_service.py:1` — presign/finalize uploads, download SAS, batch ops.
- Attachments SSE: `src/service/chat/chat_attachments_sse_service.py:1` — pub/sub stream of attachment events.
- Knowledge Upload: `src/service/knowledge_base/knowledge_upload_service.py:1` — presign/finalize, enqueue KB jobs, status.
- Knowledge Access: `src/service/knowledge_base/knowledge_access_service.py:1` — list/filter, stats, download SAS, delete.
- Knowledge SSE: `src/service/knowledge_base/knowledge_sse_service.py:1` — pub/sub stream of KB source events.
- Admin Auth: `src/service/admin/admin_auth_service.py:1` — super admin login flows and token handling.
- Org/User Mgmt: `src/service/orgnisation/organization_management.py:1`, `src/service/orgnisation/user_management.py:1` — org and user lifecycle.
- CRM Deals: `src/service/crm/deals_access_service.py:1` — list/filter deals, get deal detail, stats.
- CRM Deal Jobs: `src/service/crm/deal_job_service.py:1` — create/manage deal summary jobs.
- CRM Stakeholders: `src/service/crm/stakeholders_service.py:1` — list stakeholders, approve/reject, add from contacts, get detail with engagement history.
- CRM Stakeholder Insights: `src/service/crm/stakeholder_insight_service.py:1` — list/filter insights, acknowledge/dismiss/resolve.
- CRM Meetings: `src/service/crm/meetings_service.py:1` — list meetings, update status.
- CRM Meeting Jobs: `src/service/crm/deal_meeting_job_service.py:1` — create/manage meeting briefing/summary/followup jobs.
- CRM Deal Notes: `src/service/crm/deal_notes_service.py:1` — list notes, create user notes.
- CRM Risks/Blockers: `src/service/crm/risk_blocker_service.py:1` — list/create/update risks and blockers, confirm/dismiss/resolve/reopen.
- CRM Deal Sync: `src/service/integrations/crm_deal_sync_service.py:1` — sync deals from CRM integrations.
- Stakeholder Sync: `src/workers/deal_worker/services/stakeholder_sync_service.py:1` — sync stakeholders from CRM participants.

Schemas (Primary Models)

- Base envelope: `src/api/schemas/base_models.py:1#GenericResponse[T]`.
- Chat: `src/api/schemas/chat_models.py:1` — sessions, messages, list/search/favorite DTOs.
- Chat streaming: `src/api/schemas/chat_stream_models.py:1`, `src/api/schemas/chat_socket_models.py:1`.
- Uploads (attachments): `src/api/schemas/upload_models.py:1` — `PresignRequest/Response`, `FinalizeRequest`, batch models, `AttachmentEventUpdate`.
- Knowledge upload/access: `src/api/schemas/knowledge_upload_models.py:1`, `src/api/schemas/knowledge_access_models.py:1` — presign/finalize/status/list/stats, `KnowledgeSourceEventUpdate`.
- CRM: `src/api/schemas/crm_models.py:1` — `DealListItem`, `DealDetail`, `DealsPage`, `DealsStats`, `StakeholderDetail`, `StakeholderFullDetail` (with engagement history), `StakeholderEngagementSnapshot`, `StakeholderInsightDetail`, `DealMeetingResponse`, `TimelineEvent`, `TimelinePage`, `RiskBlockerDetail`, `RiskBlockerListItem`.

Event Contracts (Streams/PubSub)

- Chat Redis Stream frames (produced by worker):
  - `answer.delta`, `answer.replace`, `answer.metrics`, `suggestions.replace`, `sources.replace`, `agent.switch`, `question.prompt`, `progress.update`, `tool.usage`, `thinking.update`, `finalize` (see `src/workers/chat_worker/tasks/message_processing.py:1`).
- Attachments Pub/Sub event: `AttachmentEventUpdate` with `attachment_id`, `status`, `updated_at`, optional `error_message`, `thumbnail_small_url` (publish via `src/infra/messaging/attachment_status_publish.py:1#publish_attachment_status`).
- Knowledge Pub/Sub event: `KnowledgeSourceEventUpdate` with `source_id`, `status`, optional `updated_at`, `error_message`, `progress`, `thumbnail_small_url`, `thumbnail_medium_url` (publish via `src/infra/messaging/knowledge_source_status_publish.py:1#publish_source_status`).

Environment (Minimal to Run E2E)

- App: `ENV` (`local|dev|prod`), `SESSION_SECRET` (FastAPI session middleware).
- Database: either `DATABASE_URL` or `DATABASE_HOST`, `DATABASE_PORT`, `DATABASE_NAME`, `DATABASE_USER`, `DATABASE_PASSWORD`, optional `DATABASE_SSL_MODE`.
- Redis: `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD` (if set).
- Storage: `CLOUD_PROVIDER`, `APPLICATION_STORAGE_NAME`, `ORGANISATION_STORAGE_NAME`, `APPLICATION_REGION`.
- Secrets/Key Vault: `AZURE_KEY_VAULT_URL`; for cloud auth either workload identity (`AZURE_TENANT_ID`, `AZURE_CLIENT_ID` [+ optionally `AZURE_ADDITIONALLY_ALLOWED_TENANTS`]) or Managed Identity (no vars required). Local uses DefaultAzureCredential.
- Email/Supabase (if auth enabled): `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_JWT_SECRET`, and `RESEND_API_KEY`/`FROM_EMAIL` if emailing.
- File limits: `MAX_UPLOAD_FILE_SIZE_BYTES`, `ALLOWED_MIMETYPES`, and knowledge equivalents `KNOWLEDGE_MAX_UPLOAD_FILE_SIZE_BYTES`, `KNOWLEDGE_ALLOWED_MIMETYPES`.
