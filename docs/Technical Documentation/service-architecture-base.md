Service Architecture Base: Tenancy, Auth, Storage, Messaging, Responses (Concise)

Purpose
- Reusable foundation for all services (knowledge base, chat attachments, etc.).
- Compact map so engineers and LLM tools can pick the right building blocks quickly.

Quickstart (checklist)
- Decide audience: user routes (tenant scope) vs admin routes (master scope).
- Import dependencies: `get_current_user`, `get_org_session`, `Response`, `GenericResponse[T]`.
- Create service bound to `org_session` (+ user/org IDs).
- If uploading/downloading files: use `AzureOrganisationBlobStorage` to issue SAS.
- If long work: enqueue Celery task; publish updates over Redis; expose SSE if needed.
- Wrap every endpoint in the standard response envelope; return typed `GenericResponse`.
- Enforce permissions: scope by `organisation_id` AND `user_id`.

What To Use (by Concern)
- Tenancy/DB session: `src/infra/utils/database.py:1` → `get_org_session(org_id)`; dependency: `src/api/dependencies/organisation_dependencies.py:1`.
- User auth: `src/infra/auth/user_auth.py:1` → `user_auth`; dependency: `get_current_user()` returns `UserIDs(user_id, organisation_id)`.
- Admin auth: `src/infra/auth/admin_auth.py:1` → `admin_auth` for master‑scope routes.
- Storage: `src/infra/storage/azure_org_blob_storage.py:1` → `AzureOrganisationBlobStorage` (per‑org container, SAS read/write).
- Pub/Sub + SSE: `src/infra/messaging/redis_pubsub.py:1` + SSE services:
  - Knowledge: `src/service/knowledge_base/knowledge_sse_service.py:1`
  - Attachments: `src/service/chat/chat_attachments_sse_service.py:1`
- Background: Celery apps `src/workers/knowledge_worker/celery_app.py:1`, `src/workers/chat_worker/celery_app.py:1`.
- Response envelope: `src/api/utils/http_response.py:1` + `GenericResponse[T]`.

System Overview (ASCII)

```
 +--------+    +-----------+    +---------+    +--------------+
 | Client | -> |   API     | -> |  Auth   | -> | Org Session  |
 +--------+    +-----------+    +---------+    +--------------+
      |                                   |
      |                                   v
      |                             +-----------+
      |                             |  Service  |
      |                             +-----------+
      |                                   |
      |    Presign: generate_write_sas     v
      |   Finalize: get_blob_properties  +--------------+
      |                                  |  Azure Blob  |
      |                                  +--------------+
      |                                          ^
      |                                          |
      |      Celery: post-upload/extract         |
      |                                          |
      |                                  +--------------+
      +---- SSE (status/events) <--------| Redis PubSub |
                                         +--------------+
```

Tenancy & DB Access
- Master DB (public schema) holds organisations, users, super admins.
- Tenant schemas (one per org) hold business tables (attachments, sources, chunks).
- Use `get_org_session(org_id)` which sets `SET search_path TO "{tenant}", public` and resets on exit.
- Strategy: prefer direct tenant engine (role+password from Key Vault), fallback to `SET ROLE` on master.
- SSE streams keep the tenant session open for the stream lifetime (manual generator pattern).

Credentials & Connections (ASCII)

```
Azure Credentials
-----------------
[ENV=local] -> DefaultAzureCredential
[ENV=prod]  -> if Workload Identity (tenant_id + client_id) -> WorkloadIdentityCredential
               else -> ManagedIdentityCredential

Database (per request / stream)
--------------------------------
get_org_session(org_id):
  try Direct tenant engine (role password from Key Vault) -> search_path tenant,public
  else Master engine + SET ROLE <org_role> -> search_path tenant,public
```

AuthN/AuthZ
- User routes: verify Supabase JWT, then load `public.users`; require ACTIVE + APPROVED; dependency yields `UserIDs`.
- Admin routes: verify admin JWT, use master session, enforce `AdminRole` per endpoint.

Auth snippets
```python
# User context (tenant routes)
from fastapi import Depends
from sqlmodel import Session
from src.api.dependencies.organisation_dependencies import get_current_user, get_org_session, UserIDs

async def get_service(user: UserIDs = Depends(get_current_user), db: Session = Depends(get_org_session)):
    svc = Service(db, user.organisation_id, getattr(user, 'user_id', None))
    try:
        yield svc
    finally:
        await svc.close()

# Admin context (master routes)
from src.infra.utils.database import get_master_session
from src.infra.auth.admin_auth import admin_auth
```

Storage Model
- Single storage account per environment; one container per organisation.
- Deterministic blob paths: `chat_uploads/{user}/{id}/file.ext` or `knowledgebase/{user}/{id}/file.ext`.
- SAS URLs:
  - Write SAS (client upload) short TTL (~10–15 min).
  - Read SAS (download) TTL (~30–120 min), optional Content‑Disposition filename.

Messaging & SSE
- Channels per org: knowledge `kb:{org_id}:sources`, attachments `att:{org_id}:attachments`.
- Async Redis client (redis>=5). SSE services:
  - Emit initial non‑terminal snapshot as `status`.
  - Stream `status` on updates; send `ping` heartbeats; `complete` when all watched IDs are terminal; `timeout` on deadline.

Background Jobs
- API enqueues Celery tasks (e.g., post‑upload processing, extraction). Workers update DB and publish Redis events.

Celery snippets
```python
# Enqueue from API
from src.workers.chat_worker.celery_app import app as chat_app
chat_app.send_task("attachments.post_upload", args=[str(org_id), str(item_id)], queue="attachments", routing_key="attachments")

# Define a task (worker)
from src.workers.chat_worker.celery_app import app
@app.task(name="attachments.post_upload", bind=True, acks_late=True, max_retries=3)
def post_upload(self, organisation_id: str, attachment_id: str):
    ...  # update DB, publish Redis event
```

Response Envelope (Generic)
- Shape: `{ status: SUCCESS|ERROR, data, message }`.
- Helpers: `Response.success_response`, `Response.accepted_response`, `Response.error_response`.
- Annotate route responses with `GenericResponse[T]` for typing.

Router skeleton
```python
from fastapi import APIRouter, Depends, Body
from src.api.utils.http_response import Response
from src.api.schemas.base_models import GenericResponse

router = APIRouter()

@router.post("/example", response_model=GenericResponse[MyResponseModel])
async def example(payload: MyRequestModel = Body(...), svc: Service = Depends(get_service)):
    try:
        result = await svc.do_something(payload)
        return Response.success_response("ok", result)
    except DomainError as e:
        return Response.error_response(e.status_code, e.message)
    except Exception:
        return Response.internal_server_error_response("An unexpected error occurred")
```

Reusable Permission Rules
- Always scope by `organisation_id` AND `user_id`.
- Exclude soft‑deleted by default.
- Downloads require READY/COMPLETED + valid `storage_path`.
- SSE restricts to caller’s items or explicit IDs.
- Admin routes: master session only; never write tenant data.

Tenancy snippet
```python
# Read with key‑set pagination (example idea)
stmt = select(Model).where(Model.organisation_id==org_id, Model.user_id==user_id, getattr(Model,'is_deleted',False)==False)
```

Minimal Router Template (pseudocode)
```text
async def get_service(user_ids=Depends(get_current_user), org_session=Depends(get_org_session)):
    svc = Service(org_session, user_ids.organisation_id[, user_ids.user_id])
    try: yield svc
    finally: await svc.close()

@router.post(...)
async def handler(payload: Model, svc=Depends(get_service), user_ids=Depends(get_current_user)):
    try:
        # Use svc; enqueue Celery if needed
        return Response.success_response("ok", data)
    except DomainError as e:
        return Response.error_response(e.status_code, e.message)
    except Exception:
        return Response.internal_server_error_response("An unexpected error occurred")
```

Where This Applies
- Knowledge base uploads/access: reuse tenancy, SAS, Celery, Redis/SSE, envelope.
- Chat attachments: same patterns; different DB models + channels.

Minimal SSE publish/consume contract
```python
# Publish (worker)
from src.infra.messaging.attachment_status_publish import publish_attachment_status
publish_attachment_status(org_id, AttachmentEventUpdate(...))

# Consume (API SSE service)
async for message in AsyncRedisPubSub().subscribe_to_channel(channel):
    yield {"event": "status", "data": json.dumps([message])}
```

Local Dev Notes
- ENV=local uses DefaultAzureCredential and favors SET ROLE fallback.
- Run: Postgres + Redis + Celery worker + FastAPI app.
- Common env vars: `ORGANISATION_STORAGE_NAME`, DB host/port/name/user/pass, Redis host/port/password.
- If Azure auth fails locally, ensure you’re logged in (e.g., az login) or configure env secrets.

Observability
- Loggers to tail: `azure_org_blob_storage`, `redis_pubsub`, workers (`attachments_post_upload_task`, `knowledge_*`), routers (`upload_handler`, `knowledge_*`).
- SSE issues: watch server logs for heartbeat/timeout events; verify Redis channel names per org.
