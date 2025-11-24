# Service Architecture Base: Tenancy, Auth, Storage, Messaging, Workers, Responses (Updated)

Purpose

- Single reference to understand how to add APIs quickly and correctly.
- Reflects current code: new DB providers, Azure Key Vault secrets, Redis (pub/sub + streams), Celery workers.

Quickstart (Checklist)

- Scope: user routes (tenant) vs admin routes (master).
- Import: `get_current_user`, `get_org_session` or async variants, `Response`, `GenericResponse[T]`.
- Bind services to an org session and user/org IDs.
- For uploads/downloads: use `AzureOrganisationBlobStorage` to issue SAS.
- For long jobs: enqueue Celery; publish updates over Redis; expose SSE if needed.
- Wrap responses using the standard envelope `GenericResponse[T]`.
- Always scope queries by `organisation_id` and, where applicable, `user_id`.

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

- Configuration: `src/infra/config/constant.py:1` and `src/infra/config/configs.py:1` (ENV‑aware host/port/password; TLS in non‑local).
 Sync connections via `src/infra/messaging/redis_client.py:1` (connection pool).
- Async Pub/Sub for real‑time broadcast: `src/infra/messaging/redis_pubsub.py:1`.
- Redis Streams for replayable frames (chat): `src/infra/messaging/redis_streams.py:1`.
- Channel helpers (import where needed): `src/infra/messaging/redis_channels.py:1`.

Redis Client (how to use it effectively)
Entry points:

- get a pooled, sync client with `from src.infra.messaging.redis_client import get_redis_client`.
- close pool during shutdown/tests with `close_redis_pool()`.
Connection model:
- Lazy, module‑level singleton `RedisConnectionManager` creates a redis‑py `ConnectionPool`.
- Pool is initialized on first call; a `PING` verifies connectivity. On failure, the pool is discarded and error is logged.
- TLS is automatically enabled when `ENV != local` using `SSLConnection`. Cert requirements can be set with `REDIS_SSL_CERT_REQS` (CERT_NONE|CERT_OPTIONAL|CERT_REQUIRED).
- Timeouts set for resilience: `socket_timeout=30`, `socket_connect_timeout=30`, `retry_on_timeout=True`, `health_check_interval=30`, `socket_keepalive=True`, `max_connections=20`.
Usage patterns:
- Counters/TTL (rate limiting, locks):
  - Use `client.incr(key)` and set TTL only on first hit (`expire(key, seconds)` when result is 1).
  - Always wrap calls with try/except; on infrastructure issues, prefer fail‑open for non‑critical paths (e.g., rate limits) and fail‑closed for critical locks.
- TTL/readiness:
  - Use `client.ttl(key)`; remember -2 means missing and -1 means no expiry.
- Pub/Sub:
  - Prefer async pub/sub helper `AsyncRedisPubSub` for SSE services; for simple sync publications, keep payloads small and JSON‑serializable.
- Streams:
  - Use `redis_streams.py` helpers (xadd_json_frame, consumer groups) for ordered frames and replay.
Best practices:
- Do not construct raw clients; always use `get_redis_client()` to reuse the pool.
- Namespace keys consistently: `rl:{subject}:{id}:...` for rate limits, `sse:{org}:{topic}:...` for pub/sub, `stream:{org}:{type}:{id}` for streams.
- Keep keys short but descriptive; include time bucket suffixes when implementing fixed windows.
- Validate retry logic in workers where operations are idempotent; avoid retry storms from the API layer.
- In cloud, validate TLS handshake by setting `REDIS_SSL_CERT_REQS=CERT_REQUIRED` where CA trust is configured.
Examples:
- Get a client and increment with TTL:

    ```python
    from src.infra.messaging.redis_client import get_redis_client
    client = get_redis_client()
    count = client.incr(key)
    if count == 1:
        client.expire(key, 60)
    ```

- Check TTL safely:

    ```python
    ttl = client.ttl(key)
    ttl = None if ttl is None or ttl < 0 else int(ttl)
    ```

- Publish a small JSON event (sync):

    ```python
    import json
    client.publish(channel, json.dumps(payload))
    ```

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
  - Content: `src/workers/content_worker/config.py:1`.
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

Router Skeleton (Tenant route)

```python
from fastapi import APIRouter, Depends, Body
from sqlalchemy.orm import Session
from src.api.utils.http_response import Response
from src.api.schemas.base_models import GenericResponse
from src.api.dependencies.organisation_dependencies import (
    get_current_user, get_org_session, UserIDs
)

router = APIRouter()

@router.post("/example", response_model=GenericResponse[MyResponseModel])
def example(
    payload: MyRequestModel = Body(...),
    user: UserIDs = Depends(get_current_user),
    db: Session = Depends(get_org_session),
):
    try:
        result = do_something(db, user.organisation_id, user.user_id, payload)
        return Response.success_response("ok", result)
    except DomainError as e:
        return Response.error_response(e.status_code, e.message)
    except Exception:
        return Response.internal_server_error_response("An unexpected error occurred")
```

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
- Admin Organisations: `POST /admin/organisations/create_organisation`, `GET /admin/organisations/list_organisations`, `GET /admin/organisations/all_organisations_history`, `GET /admin/organisations/{organisation_id}`, `PUT /admin/organisations/{organisation_id}/update`, `PUT /admin/organisations/{organisation_id}/status`, `GET /admin/organisations/{organisation_id}/history` in `src/api/routers/admin/organizations_handler.py:1`.
- Admin Users: `POST /admin/users/create_user`, `GET /admin/users/{user_id}/profile`, `GET /admin/users/list_users`, `PUT /admin/users/{user_id}/admin_status`, `GET /admin/users/history/organisation/{organisation_id}`, `PUT /admin/users/{user_id}/update` in `src/api/routers/admin/users_handler.py:1`.

Workers & Tasks

- Chat Worker: `src/workers/chat_worker/celery_app.py:1`; tasks — `attachments.post_upload` (`src/workers/chat_worker/tasks/post_upload.py:1`), `chat.process_message` (`src/workers/chat_worker/tasks/message_processing.py:1`).
- Knowledge Worker: `src/workers/knowledge_worker/celery_app.py:1`; tasks — `knowledge.post_upload_process`, `knowledge.document_extraction`, `knowledge.delete_source` in `src/workers/knowledge_worker/tasks/*:1`.
- Content Worker: `src/workers/content_worker/celery_app.py:1`; tasks — slide/technical deck generation in `src/workers/content_worker/tasks/*:1`.
- Organization Worker: `src/workers/organization_worker/celery_app.py:1`; tasks — `organization.create` in `src/workers/organization_worker/tasks/organization.py:1`.
- Shared Base: Celery app factory and routing in `src/workers/base/celery_base.py:1`, `src/workers/base/worker_config.py:1`.
- Messaging Contracts: Redis Pub/Sub channels and Streams utilities in `src/infra/messaging/*:1` (used by tasks and SSE services).

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

Schemas (Primary Models)

- Base envelope: `src/api/schemas/base_models.py:1#GenericResponse[T]`.
- Chat: `src/api/schemas/chat_models.py:1` — sessions, messages, list/search/favorite DTOs.
- Chat streaming: `src/api/schemas/chat_stream_models.py:1`, `src/api/schemas/chat_socket_models.py:1`.
- Uploads (attachments): `src/api/schemas/upload_models.py:1` — `PresignRequest/Response`, `FinalizeRequest`, batch models, `AttachmentEventUpdate`.
- Knowledge upload/access: `src/api/schemas/knowledge_upload_models.py:1`, `src/api/schemas/knowledge_access_models.py:1` — presign/finalize/status/list/stats, `KnowledgeSourceEventUpdate`.
- Technical content: `src/api/schemas/technical_deck_models.py:1`.

Queues & Routing (Celery)

- Chat Worker exchange `chat`: queues `attachments`, `chat_processing`, `chat_messages`.
  - Tasks: `attachments.post_upload` → `attachments`; `chat.process_message` → `chat_messages`.
- Knowledge Worker exchange `knowledge`: queues `knowledge_upload`, `knowledge_extract`.
  - Tasks: `knowledge.post_upload_process` → `knowledge_upload`; `knowledge.document_extraction` → `knowledge_extract`; `knowledge.delete_source` → `knowledge_upload`.
- Content Worker: queues `content_generation`, `deck_generation`, `slides` (default `content_generation`).
- Organization Worker exchange `organization`: queue `organization`.
  - Tasks: `organization.create` → `organization`.

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
