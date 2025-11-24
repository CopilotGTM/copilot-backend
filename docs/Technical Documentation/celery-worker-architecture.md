# Celery Worker Architecture: Reference Guide

**Purpose**: Unified reference for creating and maintaining Celery workers in the CopilotGTM backend.

---

## Setup Checklist

### 1. Create Worker Config (`src/workers/my_worker/config.py`)

```python
from kombu import Exchange, Queue
from src.workers.base.worker_config import BaseWorkerConfig

class MyWorkerConfig(BaseWorkerConfig):
    def __init__(self):
        super().__init__()
        self.task_time_limit = 300  # 5 min (1800 for long tasks)
        self.task_soft_time_limit = 240
        
        exchange = Exchange("my_worker", type="direct")
        self.task_default_queue = "my_processing"
        self.task_queues = (
            Queue("my_processing", exchange=exchange, routing_key="my_processing"),
        )
        self.task_routes = {
            "my_worker.process": {"queue": "my_processing", "routing_key": "my_processing"},
        }
```

### 2. Create Celery App (`src/workers/my_worker/celery_app.py`)

```python
import os
os.environ["MAX_TENANT_ENGINES"] = "1"
os.environ["TENANT_POOL_MULTIPLIER"] = "1.0"

from dotenv import load_dotenv
load_dotenv(override=True)

from celery.signals import worker_ready, worker_shutdown
from src.infra.messaging.redis_client import close_redis_pool
from src.workers.base.celery_base import create_celery_app
from src.workers.my_worker.config import MyWorkerConfig

config = MyWorkerConfig()
app = create_celery_app("my_worker", config, "src.workers.my_worker.tasks")
app.conf.update(worker_max_tasks_per_child=50, result_expires=7200)

@worker_ready.connect
def on_ready(sender=None, **kwargs): pass

@worker_shutdown.connect
def on_shutdown(sender=None, **kwargs):
    close_redis_pool()
```

### 3. Create Task (`src/workers/my_worker/tasks/process.py`)

```python
import asyncio, contextlib
from src.api.dependencies.organisation_dependencies import acquire_org_session
from src.workers.my_worker.celery_app import app
from src.infra.utils.logger import get_logger

logger = get_logger("my_worker")

@app.task(name="my_worker.process", bind=True, acks_late=True, max_retries=3)
def process_task(self, org_id: str, item_id: str):
    session_gen = acquire_org_session(org_id)
    session = next(session_gen)
    loop = asyncio.new_event_loop()
    
    try:
        item = session.get(MyModel, item_id)
        if not item or item.status != Status.PENDING:
            return
        
        # Do work
        item.status = Status.COMPLETED
        session.commit()
        
    except self.MaxRetriesExceededError:
        logger.warning(f"Max retries: {item_id}")
        item.status = Status.FAILED
        session.commit()
    except Exception as e:
        logger.error(f"Task failed: {e}")
        raise self.retry(exc=e, countdown=60)
    finally:
        with contextlib.suppress(Exception):
            session.close()
            session_gen.close()
        loop.close()
```

### 4. Enqueue from API

```python
from src.workers.my_worker.celery_app import app as my_app

try:
    my_app.send_task(
        "my_worker.process",
        args=[str(org_id), str(item_id)],
        queue="my_processing",
        routing_key="my_processing"
    )
except Exception as e:
    logger.error(f"Enqueue failed: {e}")
```

---

## Core Components

### Base Configuration (`src/workers/base/worker_config.py`)

Provides shared Redis/broker configuration, SSL settings, and connection URL building.

```python
class BaseWorkerConfig:
    def __init__(self):
        self.config = Config()
        self.redis_host, self.redis_port, self.redis_password = self.config.get_redis_client_details
        self.broker_url = self._get_broker_url()  # redis:// or rediss://
        self.ssl_config = self._get_ssl_config()  # SSL for remote Redis
```

### Celery App Factory (`src/workers/base/celery_base.py`)

Factory function with consistent configuration:

```python
def create_celery_app(worker_name, config, tasks_module):
    celery_app = Celery(worker_name, broker=config.broker_url, include=[tasks_module])
    celery_app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        worker_prefetch_multiplier=1,  # Fair distribution
        task_acks_late=True,  # Prevent task loss on crash
        task_track_started=True,
        task_time_limit=config.task_time_limit,
        task_soft_time_limit=config.task_soft_time_limit,
    )
    return celery_app
```

---

## Task Implementation

### Task Structure Pattern

```python
@app.task(name="worker.action", bind=True, acks_late=True, max_retries=3)
def task_function(self, org_id: str, item_id: str):
    # 1. Initialize resources
    session_gen = acquire_org_session(org_id)
    session = next(session_gen)
    storage = AzureOrganisationBlobStorage(organisation_id=org_id)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        # 2. Load and validate
        item = session.get(Model, item_id)
        if not item or item.status in [Status.COMPLETED, Status.FAILED]:
            return
        
        # 3. Update job status
        item.status = Status.IN_PROGRESS
        session.commit()
        
        # 4. Perform work
        result = loop.run_until_complete(storage.process(item))
        
        # 5. Update and publish
        item.status = Status.COMPLETED
        session.commit()
        publisher.publish(status=Status.COMPLETED.value)
        
        # 6. Enqueue next task (optional)
        app.send_task("worker.next_step", args=[org_id, item_id], queue="next_queue")
        
    except self.MaxRetriesExceededError:
        item.status = Status.FAILED
        session.commit()
    except Exception as e:
        logger.exception(f"Task failed: {e}")
        raise self.retry(exc=e, countdown=60)
    finally:
        # 7. Cleanup
        with contextlib.suppress(Exception):
            session.close()
            session_gen.close()
        try:
            loop.run_until_complete(storage.close())
        except Exception:
            pass
        finally:
            loop.close()
```

### Database Access

**Sync (Preferred)**:

```python
session_gen = acquire_org_session(org_id)
session = next(session_gen)
try:
    record = session.get(Model, record_id)
    session.commit()
finally:
    with contextlib.suppress(Exception):
        session.close()
        session_gen.close()
```

**Async (When Needed)**:

```python
from src.infra.database.factory import get_async_provider

async def main():
    provider = get_async_provider()
    async with provider.tenant_session(org_id) as session:
        result = await session.execute(query)
        await session.commit()
        return result

result = asyncio.run(main())
```

### Status Publishing

**Attachment Status**:

```python
from src.infra.messaging.attachment_status_publish import publish_attachment_status
from src.api.schemas.upload_models import AttachmentEventUpdate

publish_attachment_status(org_id, AttachmentEventUpdate(
    attachment_id=str(attachment_id),
    status=AttachmentStatus.COMPLETED.value,
    updated_at=datetime.now(UTC).isoformat(),
))
```

**Knowledge Source**:

```python
from src.workers.knowledge_worker.services.progress import ProgressPublisher

publisher = ProgressPublisher(organisation_id=org_id, source_id=source_id)
publisher.publish(status=Status.EXTRACTING.value, progress=50)
```

**Chat Frames**:

```python
from src.infra.messaging.redis_streams import xadd_json_frame
from src.infra.messaging.redis_channels import channel_for_chat_job

stream_key = channel_for_chat_job(org_id, job_id)
xadd_json_frame(stream_key, {
    "type": "answer.delta",
    "content": "text chunk",
    "timestamp": datetime.now(UTC).isoformat(),
}, maxlen=500)
```

### Error Handling

```python
try:
    # Work
except self.MaxRetriesExceededError:
    # Mark as FAILED, don't retry
    item.status = Status.FAILED
    session.commit()
except TransientError as e:
    # Retry with backoff
    raise self.retry(exc=e, countdown=60)
except ValidationError as e:
    # Don't retry validation errors
    item.status = Status.FAILED
    item.error_message = str(e)[:500]
    session.commit()
```

---

## Integration Worker

### Architecture

```text
Nango Webhook → API Handler → Celery Task → Dispatcher → Fetcher → Adapter → Database
```

### Webhook Flow

1. **Webhook Reception** (`/webhooks/nango`)
   - Verify signature: `X-Nango-Signature`
   - Types: `auth` (new connection) or `sync` (data update)

2. **Task Enqueue** (`NangoWebhookService`)
   - Resolve `org_id` and `integration_id` from payload
   - Enqueue `sync_integration_data_task` with category/provider

3. **Sync Execution** (`sync_tasks.py`)
   - Fetch integration record
   - Create `NangoClient` for API proxy
   - Acquire sync lock (prevent duplicates)
   - Call provider-specific fetcher

4. **Fetcher** (e.g., `google_calendar.py`)
   - Use `NangoClient.proxy_request()` to call provider API via Nango
   - Return list of records

5. **Adapter** (e.g., `calendar_adapter.py`)
   - Delegate to `SyncService.upsert_calendar_events()`
   - Create/fetch `OrganisationSource` (type=INTEGRATION)
   - Process records and create `OrganisationDocument` records

6. **Database Persistence** (`sync_service.py`)
   - **Idempotency**: `content_hash` = SHA256(`calendar|{provider}|{external_id}`)
   - **Upsert**: Check existing by hash, update/insert
   - **Batch Commits**: Every 50 records
   - **Final Commit**: Update `integration.last_synced_at`

### Key Components

**Nango Client** (`nango_client.py`):

```python
async def proxy_request(provider_config_key, connection_id, endpoint, params):
    # Proxies API calls to provider via Nango
    # Headers: Provider-Config-Key, Connection-Id
```

**Provider Registry** (`providers/registry.py`):

```python
_REGISTRY = {
    ("calendar", "google"): {"events": google_calendar_fetch},
}
```

**Fetcher Signature**:

```python
async def fetch_events(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,  # Full sync vs incremental
    last_synced_at: datetime | None,
    data_type: str | None,
    provider_config_key: str | None,
) -> list[dict]:
```

### Adding New Integration

1. **Define type**: `class IntegrationType(str, Enum): HUBSPOT = "hubspot"`
2. **Map provider**: `NANGO_TO_INTEGRATION_TYPE = {"hubspot": IntegrationType.HUBSPOT}`
3. **Add category**: `PROVIDER_CATEGORIES = {IntegrationType.HUBSPOT: ("crm", "hubspot")}`
4. **Create fetcher**: `providers/crm/hubspot_contacts.py`
5. **Register**: `_REGISTRY[("crm", "hubspot")] = {"contacts": hubspot_contacts_fetch}`
6. **Create adapter**: `adapters/crm_adapter.py`
7. **Update dispatcher**: Add `elif request.category == "crm"` case
8. **Implement upsert**: `sync_service.upsert_crm_contacts()`

### Debugging

```bash
# Check webhook
grep "NANGO WEBHOOK RECEIVED" logs/app.log

# Check task execution
grep "Task received: integrations.sync_integration_data" logs/celery_integrations.log

# Check database
SELECT COUNT(*) FROM organisation_documents WHERE source_id IN (
    SELECT source_id FROM organisation_sources WHERE source_type = 'INTEGRATION'
);
```

---

## Quick Reference

### Common Imports

```python
from src.workers.my_worker.celery_app import app
from src.api.dependencies.organisation_dependencies import acquire_org_session
from src.infra.database.factory import get_async_provider
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.messaging.redis_client import get_redis_client, close_redis_pool
from src.infra.utils.logger import get_logger
```

### Worker Comparison

| Worker | Queues | Time Limit | Key Operations |
|--------|--------|------------|----------------|
| **Chat** | `attachments`, `chat_processing`, `chat_messages` | 5 min | Checksum, thumbnails, streaming |
| **Knowledge** | `knowledge_upload`, `knowledge_extract` | 30 min | Extraction, embedding |
| **Organization** | `organization`, `admin` | 5 min | Schema creation, role setup |
| **Integrations** | `integrations`, `token_refresh` | 5 min | OAuth, data sync |

### Channel Naming

| Purpose | Pattern | Example |
|---------|---------|---------|
| Attachments | `att:{org_id}:attachments` | `att:123e4567:attachments` |
| Knowledge | `kb:{org_id}:sources` | `kb:123e4567:sources` |
| Chat Stream | `stream:{org_id}:chat:{job_id}` | `stream:123e4567:chat:abc123` |

### Troubleshooting

| Issue | Check |
|-------|-------|
| Task not received | Worker running? Queue/routing key correct? Task route defined? |
| Task stuck PENDING | Worker crashed? Args JSON-serializable? |
| Memory leaks | Set `worker_max_tasks_per_child=50`. Close resources in `finally`. |
| Redis connection | SSL enabled for remote? Pool closed on shutdown? |

### Core Principles

- **Idempotency**: Check state before processing
- **Resilience**: Retry transient errors, fail fast on validation
- **Observability**: Log milestones, publish status updates
- **Resource Cleanup**: Always close sessions, storage, loops
- **Isolation**: Minimal DB pools, dedicated event loops
