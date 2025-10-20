# Celery Worker Architecture: Complete Reference

**Purpose**: Single reference for creating and maintaining Celery workers. Covers configuration, task patterns, database access, messaging, error handling, and operational patterns used across chat, knowledge, and organization workers.

**Quick Navigation**:
- [Quickstart Checklist](#quickstart-checklist)
- [Worker Components](#worker-components)
- [Task Patterns](#task-patterns)
- [Database Access](#database-access)
- [Messaging & Events](#messaging--events)
- [Error Handling & Retries](#error-handling--retries)
- [Resource Management](#resource-management)
- [Worker Skeleton Templates](#worker-skeleton-templates)

---

## Quickstart Checklist

When creating a new Celery worker:

1. **Create worker config** extending `BaseWorkerConfig`
   - Define exchanges, queues, and routing keys
   - Set time limits for long-running tasks
   - Map task names to queues via `task_routes`

2. **Create celery_app.py** using `create_celery_app` factory
   - Import tasks module for auto-discovery
   - Add signal handlers (worker_ready, worker_shutdown, task lifecycle)
   - Set minimal DB pool settings (`MAX_TENANT_ENGINES=1`, `TENANT_POOL_MULTIPLIER=1.0`)

3. **Create tasks** with proper decorators
   - Use `@app.task(name="...", bind=True, acks_late=True, max_retries=3)`
   - Access DB via `acquire_org_session` (sync) or get async provider
   - Use dedicated event loop for async operations (`asyncio.new_event_loop()`)
   - Always clean up resources in finally blocks

4. **Publish status updates** via Redis
   - Use dedicated publisher classes (`ProgressPublisher`, `publish_attachment_status`, etc.)
   - Publish at key lifecycle points: processing, completion, failure
   - Include timestamps and error messages

5. **Enqueue from API** using worker-specific apps
   - Import worker app: `from src.workers.chat_worker.celery_app import app as chat_app`
   - Use `send_task(name, args, queue, routing_key)`
   - Handle enqueue errors gracefully (log, don't fail request)

---

## Worker Components

### 1. Base Configuration (`src/workers/base/worker_config.py`)

**Purpose**: Shared configuration for all workers—broker URL, SSL, Redis connection details.

**Key Features**:
- Automatically builds broker URL from Redis config (environment-aware)
- SSL enabled for remote Redis (not localhost)
- Configurable SSL cert requirements via `REDIS_SSL_CERT_REQS` (CERT_NONE, CERT_OPTIONAL, CERT_REQUIRED)

```python
from src.workers.base.worker_config import BaseWorkerConfig

class BaseWorkerConfig:
    def __init__(self):
        self.config = Config()
        self.redis_host, self.redis_port, self.redis_password = self.config.get_redis_client_details
        self.broker_url = self._get_broker_url()
        self.backend_url = self.broker_url
        self.ssl_config = self._get_ssl_config()
```

**What it provides**:
- `broker_url`: Redis connection string (redis:// or rediss://)
- `backend_url`: Same as broker (Redis as result backend)
- `ssl_config`: Dict with `ssl_cert_reqs` and `ssl_check_hostname` (or None for local)

### 2. Worker-Specific Config

**Purpose**: Define queues, exchanges, routing, and time limits per worker.

**Pattern**:
```python
from kombu import Exchange, Queue
from src.workers.base.worker_config import BaseWorkerConfig

class KnowledgeWorkerConfig(BaseWorkerConfig):
    def __init__(self):
        super().__init__()
        
        # Time limits (30 min hard, 28 min soft for knowledge extraction)
        self.task_time_limit = 1800
        self.task_soft_time_limit = 1680
        
        # Exchange definition
        knowledge_exchange = Exchange("knowledge", type="direct")
        
        # Default queue/routing
        self.task_default_exchange = "knowledge"
        self.task_default_exchange_type = "direct"
        self.task_default_queue = "knowledge_upload"
        self.task_default_routing_key = "knowledge_upload"
        
        # Queue definitions
        self.task_queues = (
            Queue("knowledge_extract", exchange=knowledge_exchange, routing_key="knowledge_extract"),
            Queue("knowledge_upload", exchange=knowledge_exchange, routing_key="knowledge_upload"),
        )
        
        # Task routing (map task names to queues)
        self.task_routes = {
            "knowledge.document_extraction": {
                "queue": "knowledge_extract",
                "routing_key": "knowledge_extract",
            },
            "knowledge.post_upload_process": {
                "queue": "knowledge_upload",
                "routing_key": "knowledge_upload",
            },
        }
```

**Key Principles**:
- Use **direct exchange** with explicit routing keys
- Each queue gets its own routing key (typically matches queue name)
- Always define `task_routes` to ensure correct message routing
- Set appropriate time limits based on task duration (uploads < extractions < generation)

### 3. Celery App Factory (`src/workers/base/celery_base.py`)

**Purpose**: Factory function to create a Celery app with consistent configuration.

```python
from src.workers.base.celery_base import create_celery_app

def create_celery_app(worker_name, config, tasks_module):
    celery_app = Celery(
        worker_name,
        broker=config.broker_url,
        backend=config.backend_url,
        include=[tasks_module],
    )
    
    # Apply SSL config
    if config.ssl_config:
        celery_app.conf.broker_use_ssl = config.ssl_config
        celery_app.conf.redis_backend_use_ssl = config.ssl_config
    
    # Apply worker-specific queue config
    celery_app.conf.task_queues = config.task_queues
    celery_app.conf.task_default_queue = config.task_default_queue
    celery_app.conf.task_routes = config.task_routes
    
    # Essential Celery configuration
    celery_app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        worker_prefetch_multiplier=1,  # Fair task distribution
        task_acks_late=True,  # Prevent task loss on worker crash
        worker_disable_rate_limits=True,
        broker_connection_retry_on_startup=True,
        task_track_started=True,
        task_time_limit=config.task_time_limit,
        task_soft_time_limit=config.task_soft_time_limit,
    )
    
    return celery_app
```

**Configuration Highlights**:
- `task_acks_late=True`: Task requeued if worker crashes before completion
- `worker_prefetch_multiplier=1`: Prevents one worker from hogging all tasks
- `task_track_started=True`: Enables task state tracking
- Time limits: soft limit for graceful shutdown, hard limit for forced termination

### 4. Worker Celery App (`src/workers/*/celery_app.py`)

**Purpose**: Worker entrypoint—creates app, registers signal handlers, manages lifecycle.

**Standard Pattern**:
```python
import os
os.environ["MAX_TENANT_ENGINES"] = "1"  # Minimize DB pool per worker
os.environ["TENANT_POOL_MULTIPLIER"] = "1.0"

from dotenv import load_dotenv
load_dotenv(override=True)

from celery.signals import worker_ready, worker_shutdown, task_prerun, task_postrun, task_failure
from src.infra.messaging.redis_client import close_redis_pool
from src.infra.utils.logger import get_logger
from src.workers.base.celery_base import create_celery_app
from src.workers.knowledge_worker.config import KnowledgeWorkerConfig

logger = get_logger("knowledge_worker")

config = KnowledgeWorkerConfig()
tasks_module = "src.workers.knowledge_worker.tasks"
app = create_celery_app("knowledge_worker", config, tasks_module)

# Worker-specific settings
app.conf.update(
    worker_log_format="[%(asctime)s: %(levelname)s/%(processName)s] %(message)s",
    worker_task_log_format="[%(asctime)s: %(levelname)s/%(processName)s][%(task_name)s(%(task_id)s)] %(message)s",
    worker_pool_restarts=True,
    worker_max_tasks_per_child=50,  # Prevent memory leaks
    result_expires=7200,  # 2 hours
    result_persistent=True,
)

@worker_ready.connect
def worker_ready_handler(sender=None, **kwargs):
    logger.info("Knowledge worker is ready. Redis connection pool initialized.")

@worker_shutdown.connect
def worker_shutdown_handler(sender=None, **kwargs):
    logger.info("Knowledge worker shutting down. Closing Redis connection pool.")
    try:
        close_redis_pool()
        logger.info("Redis connection pool closed successfully")
    except Exception as e:
        logger.error(f"Error closing Redis connection pool: {e}")

@task_prerun.connect
def task_prerun_handler(task_id=None, task=None, args=None, kwargs=None, **kwds):
    logger.debug(f"Task starting: {task.name}({task_id})")

@task_postrun.connect
def task_postrun_handler(task_id=None, task=None, **kwargs):
    logger.debug(f"Task completed: {task.name}({task_id})")

@task_failure.connect
def task_failure_handler(task_id=None, exception=None, **kwargs):
    logger.error(f"Task failed: {task_id} with exception: {exception}")
```

**Key Practices**:
- Set `MAX_TENANT_ENGINES=1` **before** importing DB code to minimize connection pool
- Load environment variables first with `override=True`
- Register signal handlers for lifecycle management
- Close Redis pool on shutdown to avoid leaked connections
- Set `worker_max_tasks_per_child` to restart workers periodically (prevents memory leaks)

---

## Task Patterns

### Task Decorator Standard

```python
@app.task(name="knowledge.post_upload_process", bind=True, acks_late=True, max_retries=3)
def post_upload_process(self, organisation_id: UUID, source_id: UUID):
    ...
```

**Decorator Parameters**:
- `name`: Explicit task name (use `worker.action` convention)
- `bind=True`: Binds task instance to `self` (access retry, MaxRetriesExceededError)
- `acks_late=True`: Acknowledge task only after completion (safe failure recovery)
- `max_retries=3`: Maximum retry attempts on failure

### Task Structure Template

```python
@app.task(name="knowledge.post_upload_process", bind=True, acks_late=True, max_retries=3)
def post_upload_process(self, organisation_id: UUID, source_id: UUID):
    logger.info("Task started org=%s source=%s", organisation_id, source_id)
    
    # 1. Initialize resources
    organisation_id, source_id = str(organisation_id), str(source_id)
    session_gen = acquire_org_session(organisation_id)
    session = next(session_gen)
    storage = AzureOrganisationBlobStorage(organisation_id=organisation_id)
    publisher = ProgressPublisher(organisation_id=organisation_id, source_id=source_id)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        # 2. Load record and validate state
        source = session.get(OrganisationSource, source_id)
        if not source:
            logger.warning("Source %s not found", source_id)
            return
        
        if source.status == OrganisationSourceStatus.PENDING_UPLOAD:
            logger.warning("Source still pending upload; skipping")
            return
        
        # 3. Update job status to IN_PROGRESS
        job = session.query(KnowledgeProcessingJob).filter_by(
            source_id=source_id, job_type="post_upload_process"
        ).first()
        if job:
            job.status = JobStatus.IN_PROGRESS
            session.add(job)
            session.commit()
        
        # 4. Perform work (checksum, thumbnails, extraction, etc.)
        _compute_and_persist_checksum(loop, storage, source, session)
        
        # 5. Update status and publish event
        source.status = OrganisationSourceStatus.EXTRACTING
        session.merge(source)
        session.commit()
        
        publisher.publish(status=OrganisationSourceStatus.EXTRACTING.value)
        
        # 6. Enqueue next task (if applicable)
        app.send_task(
            "knowledge.document_extraction",
            args=[UUID(organisation_id), UUID(source_id)],
            queue="knowledge_extract",
        )
        
    except self.MaxRetriesExceededError:
        logger.warning("Max retries exceeded for source=%s", source_id)
        # Handle max retries (mark as FAILED)
    except Exception as e:
        logger.exception("Task failed org=%s source=%s error=%s", organisation_id, source_id, e)
    finally:
        # 7. Clean up resources
        with contextlib.suppress(Exception):
            session.close()
        with contextlib.suppress(Exception):
            session_gen.close()
        try:
            loop.run_until_complete(storage.close())
        except Exception:
            pass
        finally:
            loop.close()
```

**Task Lifecycle Steps**:
1. **Initialize**: Session, storage, publisher, event loop
2. **Load & Validate**: Check record exists and is in valid state
3. **Update Job**: Mark as IN_PROGRESS early
4. **Perform Work**: Core task logic (checksum, thumbnails, extraction, etc.)
5. **Update & Publish**: Persist status changes, publish events
6. **Enqueue Next**: Chain to next task if needed
7. **Clean Up**: Close all resources (session, storage, loop)

---

## Database Access

### Sync Database Access (Preferred for Workers)

```python
from src.api.dependencies.organisation_dependencies import acquire_org_session

# In task
session_gen = acquire_org_session(organisation_id)
session = next(session_gen)

try:
    # Use session for queries
    source = session.get(OrganisationSource, source_id)
    session.merge(source)
    session.commit()
finally:
    with contextlib.suppress(Exception):
        session.close()
    with contextlib.suppress(Exception):
        session_gen.close()
```

**Why Sync**:
- Simpler than mixing async with Celery's sync execution model
- No need to manage nested event loops
- Direct compatibility with SQLAlchemy ORM patterns

### Async Database Access (For Complex Services)

```python
from src.infra.database.factory import get_async_provider

async def main():
    provider = get_async_provider()
    async with provider.tenant_session(org_id) as session:
        result = await session.execute(query)
        await session.commit()
        return result

# In task
result = asyncio.run(main())
```

**When to Use**:
- Calling async service layer functions
- Need transaction management across multiple operations
- Complex queries requiring async context

### Connection Pool Management

**Environment Variables** (set at worker startup):
```python
os.environ["MAX_TENANT_ENGINES"] = "1"
os.environ["TENANT_POOL_MULTIPLIER"] = "1.0"
```

**Why Minimal Pools**:
- Workers process one task at a time (no concurrency benefit from large pools)
- Reduces memory footprint per worker
- Prevents connection exhaustion in Postgres
- Easier to scale horizontally (more workers, not bigger pools)

**Pool Monitoring**:
```python
# Optionally register DB pool monitor
try:
    import scripts.monitoring.celery_integration  # noqa: F401
except Exception:
    pass
```

---

## Messaging & Events

### Redis Client Access

```python
from src.infra.messaging.redis_client import get_redis_client

client = get_redis_client()  # Reuses connection pool
count = client.incr(f"rl:{org_id}:{endpoint}")
if count == 1:
    client.expire(f"rl:{org_id}:{endpoint}", 60)
```

**Key Patterns**:
- Always use `get_redis_client()` (never create raw clients)
- Namespace keys: `rl:*` (rate limits), `sse:*` (pub/sub), `stream:*` (streams)
- Set TTLs for transient data (counters, locks, cache)
- TLS enabled automatically for non-local environments

### Publishing Status Updates

#### Attachment Status (Chat Worker)

```python
from src.infra.messaging.attachment_status_publish import publish_attachment_status
from src.api.schemas.upload_models import AttachmentEventUpdate

event = AttachmentEventUpdate(
    attachment_id=str(attachment_id),
    status=AttachmentStatus.COMPLETED.value,
    updated_at=attachment.updated_at.isoformat(),
    error_message=None,
    thumbnail_small_url=thumbnail_url,
)
publish_attachment_status(organisation_id, event)
```

**Channel**: `att:{organisation_id}:attachments`

#### Knowledge Source Status (Knowledge Worker)

```python
from src.workers.knowledge_worker.services.progress import ProgressPublisher

publisher = ProgressPublisher(organisation_id=org_id, source_id=source_id)
publisher.publish(
    status=OrganisationSourceStatus.EXTRACTING.value,
    progress=50,
    error_message=None,
    thumbnail_small_url=url,
    thumbnail_medium_url=url,
)
```

**Channel**: `kb:{organisation_id}:sources`

#### Chat Message Frames (Chat Worker)

```python
from src.infra.messaging.redis_streams import xadd_json_frame
from src.infra.messaging.redis_channels import channel_for_chat_job

stream_key = channel_for_chat_job(organisation_id, job_id)
frame = {
    "type": "answer.delta",
    "content": "chunk of text",
    "timestamp": datetime.now(UTC).isoformat(),
}
xadd_json_frame(stream_key, frame, maxlen=500, approximate=True)
```

**Stream Key**: `stream:{organisation_id}:chat:{job_id}`

**Frame Types**:
- `answer.delta`: Incremental content chunk
- `answer.replace`: Full answer replacement
- `answer.metrics`: Confidence, citations
- `suggestions.replace`: Follow-up suggestions
- `sources.replace`: Source documents
- `agent.switch`: Agent routing change
- `progress.update`: Stage/ETA updates
- `tool.usage`: Tool execution details
- `thinking.update`: Agent reasoning
- `finalize`: Completion marker

### Channel Naming Conventions

| Purpose | Pattern | Example |
|---------|---------|---------|
| Attachment status | `att:{org_id}:attachments` | `att:123e4567-e89b-12d3-a456-426614174000:attachments` |
| Knowledge status | `kb:{org_id}:sources` | `kb:123e4567-e89b-12d3-a456-426614174000:sources` |
| Chat job stream | `stream:{org_id}:chat:{job_id}` | `stream:123e4567-e89b-12d3-a456-426614174000:chat:abc123` |
| Rate limits | `rl:{subject}:{id}:{endpoint}` | `rl:user:123:upload` |

---

## Error Handling & Retries

### Retry Strategy

```python
@app.task(name="task.name", bind=True, acks_late=True, max_retries=3)
def my_task(self, org_id, item_id):
    try:
        # ... work ...
    except self.MaxRetriesExceededError:
        logger.warning("Max retries exceeded for item=%s", item_id)
        # Mark as FAILED, publish failure event
        item.status = Status.FAILED
        session.commit()
        publisher.publish(status=Status.FAILED.value, error_message="max retries exceeded")
    except SpecificException as e:
        logger.error("Specific error: %s, retrying", e)
        raise self.retry(exc=e, countdown=60)  # Retry after 60 seconds
    except Exception as e:
        logger.exception("Unexpected error for item=%s", item_id)
        # Mark as FAILED, don't retry unexpected errors
        item.status = Status.FAILED
        session.commit()
```

**Retry Patterns**:
- **Transient errors** (network, rate limits): Retry with exponential backoff
- **Validation errors**: Don't retry, mark as FAILED immediately
- **Max retries exceeded**: Catch `self.MaxRetriesExceededError`, mark as FAILED
- **Unexpected errors**: Log, mark as FAILED, publish event

### Status Transitions

**Upload/Processing Lifecycle**:
```
PENDING_UPLOAD → PROCESSING → EXTRACTING → READY/COMPLETED
                      ↓              ↓
                   FAILED         FAILED
```

**Job Status**:
```
PENDING → IN_PROGRESS → COMPLETED
             ↓
          FAILED
```

**Guidelines**:
- Always check current status before processing
- Skip if already in terminal state (COMPLETED, FAILED)
- Publish status changes immediately after commit
- Include error messages in FAILED status

### Error Message Handling

```python
# Truncate long error messages
attachment.processing_error = str(error)[:500]

# Categorize errors for clients
error_categories = {
    "checksum_failed": "File integrity check failed",
    "thumbnail_failed": "Could not generate preview",
    "duplicate_detected": "File already exists",
    "extraction_failed": "Could not process document",
    "timeout": "Processing took too long",
}
```

---

## Resource Management

### Event Loop Management

```python
# Create dedicated loop per task (avoid reuse issues)
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

try:
    # Run async operations
    result = loop.run_until_complete(storage.compute_checksum(path))
finally:
    # Close storage clients
    try:
        loop.run_until_complete(storage.close())
    except Exception:
        pass
    finally:
        loop.close()  # Always close loop
```

**Why Dedicated Loops**:
- Avoids "loop is already running" errors
- Clean state per task execution
- Proper cleanup of async resources

### Storage Client Management

```python
storage = AzureOrganisationBlobStorage(organisation_id=organisation_id)

try:
    # Use storage for uploads, downloads, checksums
    checksum = loop.run_until_complete(storage.compute_checksum(path))
    thumbnail_url = loop.run_until_complete(storage.generate_read_sas(thumb_path, minutes=120))
finally:
    # Always close async clients
    try:
        loop.run_until_complete(storage.close())
    except Exception:
        pass
```

### Session Management

```python
session_gen = acquire_org_session(organisation_id)
session = next(session_gen)

try:
    # Use session
    record = session.get(Model, record_id)
    session.commit()
finally:
    # Close both session and generator
    with contextlib.suppress(Exception):
        session.close()
    with contextlib.suppress(Exception):
        session_gen.close()
```

### Redis Pool Lifecycle

```python
# At worker startup (signal handler)
@worker_ready.connect
def worker_ready_handler(sender=None, **kwargs):
    logger.info("Worker ready. Redis connection pool initialized.")

# At worker shutdown (signal handler)
@worker_shutdown.connect
def worker_shutdown_handler(sender=None, **kwargs):
    logger.info("Worker shutting down. Closing Redis connection pool.")
    try:
        close_redis_pool()
        logger.info("Redis connection pool closed successfully")
    except Exception as e:
        logger.error(f"Error closing Redis connection pool: {e}")
```

---

## Worker Skeleton Templates

### Template 1: Upload Post-Processing Worker

**Use Case**: Checksum calculation, thumbnail generation, virus scanning.

**Files**:
- `src/workers/my_worker/config.py`
- `src/workers/my_worker/celery_app.py`
- `src/workers/my_worker/tasks/post_upload.py`

**Config**:
```python
from kombu import Exchange, Queue
from src.workers.base.worker_config import BaseWorkerConfig

class MyWorkerConfig(BaseWorkerConfig):
    def __init__(self):
        super().__init__()
        
        self.task_time_limit = 300  # 5 minutes
        self.task_soft_time_limit = 240  # 4 minutes
        
        my_exchange = Exchange("my_worker", type="direct")
        
        self.task_default_exchange = "my_worker"
        self.task_default_exchange_type = "direct"
        self.task_default_queue = "my_processing"
        self.task_default_routing_key = "my_processing"
        
        self.task_queues = (
            Queue("my_processing", exchange=my_exchange, routing_key="my_processing"),
        )
        
        self.task_routes = {
            "my_worker.post_upload": {
                "queue": "my_processing",
                "routing_key": "my_processing",
            },
        }
```

**Task**:
```python
import asyncio
import contextlib
from src.api.dependencies.organisation_dependencies import acquire_org_session
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.logger import get_logger
from src.workers.my_worker.celery_app import app

logger = get_logger("my_post_upload_task")

@app.task(name="my_worker.post_upload", bind=True, acks_late=True, max_retries=3)
def post_upload(self, organisation_id: str, item_id: str):
    logger.info("Post-upload task started org=%s item=%s", organisation_id, item_id)
    
    session_gen = acquire_org_session(organisation_id)
    session = next(session_gen)
    storage = AzureOrganisationBlobStorage(organisation_id=organisation_id)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    item = None
    try:
        item = session.get(MyModel, item_id)
        if not item:
            logger.warning("Item %s not found", item_id)
            return
        
        if item.status != Status.PENDING:
            logger.info("Item %s already processed", item_id)
            return
        
        # Compute checksum
        if item.storage_path and not item.checksum:
            checksum = loop.run_until_complete(storage.compute_checksum(item.storage_path))
            item.checksum = checksum
            session.merge(item)
            session.commit()
        
        # Mark complete
        item.status = Status.COMPLETED
        session.merge(item)
        session.commit()
        
        logger.info("Post-upload task completed item=%s", item_id)
    except Exception as e:
        logger.error("Post-upload failed org=%s item=%s error=%s", organisation_id, item_id, e)
        if item:
            item.status = Status.FAILED
            item.error_message = str(e)[:500]
            session.merge(item)
            session.commit()
    finally:
        with contextlib.suppress(Exception):
            session.close()
        with contextlib.suppress(Exception):
            session_gen.close()
        try:
            loop.run_until_complete(storage.close())
        except Exception:
            pass
        finally:
            loop.close()
```

### Template 2: Long-Running Processing Worker

**Use Case**: Document extraction, video transcoding, ML inference.

**Config** (30-minute time limits):
```python
class ProcessingWorkerConfig(BaseWorkerConfig):
    def __init__(self):
        super().__init__()
        
        self.task_time_limit = 1800  # 30 minutes
        self.task_soft_time_limit = 1680  # 28 minutes
        
        # Exchange and queues
        exchange = Exchange("processing", type="direct")
        
        self.task_queues = (
            Queue("extraction", exchange=exchange, routing_key="extraction"),
            Queue("enrichment", exchange=exchange, routing_key="enrichment"),
        )
        
        self.task_routes = {
            "processing.extract": {"queue": "extraction", "routing_key": "extraction"},
            "processing.enrich": {"queue": "enrichment", "routing_key": "enrichment"},
        }
```

**Task with Progress Updates**:
```python
@app.task(name="processing.extract", bind=True, acks_late=True, max_retries=3)
def extract_document(self, organisation_id: str, doc_id: str):
    logger.info("Extraction started org=%s doc=%s", organisation_id, doc_id)
    
    session_gen = acquire_org_session(organisation_id)
    session = next(session_gen)
    publisher = MyProgressPublisher(organisation_id=organisation_id, doc_id=doc_id)
    
    try:
        doc = session.get(Document, doc_id)
        if not doc:
            return
        
        # Publish progress updates
        publisher.publish(status="EXTRACTING", progress=0)
        
        # Step 1: Download
        publisher.publish(status="EXTRACTING", progress=25, message="Downloading...")
        # ... download logic ...
        
        # Step 2: Extract
        publisher.publish(status="EXTRACTING", progress=50, message="Extracting text...")
        # ... extraction logic ...
        
        # Step 3: Parse
        publisher.publish(status="EXTRACTING", progress=75, message="Parsing structure...")
        # ... parsing logic ...
        
        # Complete
        doc.status = Status.READY
        session.merge(doc)
        session.commit()
        
        publisher.publish(status="READY", progress=100)
        logger.info("Extraction completed doc=%s", doc_id)
        
    except Exception as e:
        logger.exception("Extraction failed org=%s doc=%s", organisation_id, doc_id)
        doc.status = Status.FAILED
        doc.error_message = str(e)[:500]
        session.commit()
        publisher.publish(status="FAILED", error_message=str(e)[:200])
    finally:
        with contextlib.suppress(Exception):
            session.close()
        with contextlib.suppress(Exception):
            session_gen.close()
```

### Template 3: Async Service-Based Worker

**Use Case**: Organization creation, complex workflows requiring async services.

**Task**:
```python
import asyncio
from src.infra.database.factory import get_async_provider
from src.service.my_service import my_async_service
from src.workers.my_worker.celery_app import app

@app.task(name="my_worker.create", bind=True, acks_late=True, max_retries=3)
def create_resource_task(self, admin_id: str, resource_name: str, details: dict):
    logger.info(f"Creating resource: {resource_name} for admin: {admin_id}")
    
    try:
        provider = get_async_provider()
        
        async def main():
            async with provider.master_session() as session:
                return await my_async_service.create_resource(
                    admin_id=admin_id,
                    name=resource_name,
                    session=session,
                    **details,
                )
        
        result = asyncio.run(main())
        
        logger.info(f"Successfully created resource: {result['id']}")
        return {
            "id": str(result["id"]),
            "name": result["name"],
            "created_at": result["created_at"].isoformat(),
        }
        
    except Exception as e:
        logger.error(f"Failed to create resource {resource_name}: {e}", exc_info=True)
        raise
```

---

## Enqueuing Tasks from API

### Import Worker App

```python
# At top of handler file
from src.workers.chat_worker.celery_app import app as chat_app
from src.workers.knowledge_worker.celery_app import app as knowledge_app
from src.workers.organization_worker.celery_app import app as organization_app
```

### Enqueue Task (Fire-and-Forget)

```python
# In handler after finalizing upload
try:
    chat_app.send_task(
        "attachments.post_upload",
        args=[str(organisation_id), str(attachment_id)],
        queue="attachments",
        routing_key="attachments",
    )
except Exception as task_err:
    logger.error("Failed to enqueue task for attachment %s: %s", attachment_id, task_err)
    # Don't fail the request—task will be retried or handled async
```

### Enqueue with Countdown

```python
# Delay task execution by 60 seconds
knowledge_app.send_task(
    "knowledge.delete_source",
    args=[str(org_id), str(source_id)],
    queue="knowledge_upload",
    routing_key="knowledge_upload",
    countdown=60,
)
```

### Chain Tasks (Sequential Execution)

```python
# In first task, enqueue next step
app.send_task(
    "knowledge.document_extraction",
    args=[organisation_id, source_id],
    queue="knowledge_extract",
    routing_key="knowledge_extract",
)
```

---

## Operational Best Practices

### Logging Standards

```python
logger = get_logger("task_name")

# Start of task
logger.info("Task started org=%s item=%s", org_id, item_id)

# Key milestones
logger.info("Checksum computed for item=%s", item_id)

# Warnings (non-critical)
logger.warning("Item %s already processed, skipping", item_id)

# Errors (with context)
logger.error("Failed to generate thumbnail for item=%s: %s", item_id, error)

# Exceptions (with stack trace)
logger.exception("Unexpected error processing item=%s", item_id)

# End of task
logger.info("Task completed item=%s", item_id)
```

### Idempotency

**Always check state before processing**:
```python
# Check if already processed
if item.status in [Status.COMPLETED, Status.FAILED]:
    logger.info("Item %s already in terminal state, skipping", item_id)
    return

# Check if record exists
if not item:
    logger.warning("Item %s not found", item_id)
    return

# Use job status to track per-phase completion
job = session.query(Job).filter_by(item_id=item_id, job_type="post_upload").first()
if job and job.status == JobStatus.COMPLETED:
    logger.info("Job already completed for item=%s", item_id)
    return
```

### Graceful Degradation

```python
# Non-critical operations should not fail entire task
try:
    thumbnail = generate_thumbnail(image)
    item.thumbnail_path = thumbnail
except Exception as e:
    logger.warning("Thumbnail generation failed for item=%s: %s", item_id, e)
    # Continue without thumbnail

# Always update status even if ancillary operations fail
item.status = Status.COMPLETED
session.commit()
```

### Testing Tasks Locally

```python
# Direct invocation (bypasses Celery)
from src.workers.my_worker.tasks.my_task import my_task_function
result = my_task_function(org_id="...", item_id="...")

# Via Celery (requires running worker)
from src.workers.my_worker.celery_app import app
task = app.send_task("my_worker.my_task", args=["org_id", "item_id"])
result = task.get(timeout=60)
```

---

## Worker Comparison Matrix

| Worker | Queues | Avg Task Duration | Max Time Limit | Key Operations |
|--------|--------|-------------------|----------------|----------------|
| **Chat** | `attachments`, `chat_processing`, `chat_messages` | 2-5 min | 5 min (default) | Checksum, thumbnails, agent orchestration, streaming |
| **Knowledge** | `knowledge_upload`, `knowledge_extract` | 5-25 min | 30 min | Checksum, thumbnails, document extraction, embedding |
| **Organization** | `organization`, `admin` | 10-30 sec | 5 min (default) | Schema creation, role setup, initial data |
| **Content** | `content_generation`, `deck_generation`, `slides` | 5-15 min | 30 min | Slide generation, technical deck creation |
| **Integrations** | `integrations`, `token_refresh` | 5-30 sec | 5 min (default) | OAuth token refresh, integration sync |

---

## Integration Worker Deep Dive

The **Integrations Worker** is responsible for syncing data from external providers (Google Calendar, HubSpot, etc.) via Nango proxy and storing it in the database.

### Architecture Overview

```
Nango Webhook → FastAPI Handler → Celery Task → Dispatcher → Fetcher → Adapter → Database
                (oauth_routes.py)  (sync_tasks.py) (dispatcher.py) (google_calendar.py) (calendar_adapter.py)
```

### Data Flow

1. **Webhook Reception** (`src/api/routers/integrations/oauth_routes.py`)
   - Nango sends POST to `/webhooks/nango` after OAuth or sync event
   - Signature verification via `X-Nango-Signature` header
   - Webhook types: `auth` (new connection) or `sync` (data update)

2. **Task Enqueue** (`src/service/integrations/nango_webhook_service.py`)
   - Resolves `organisation_id` and `integration_id` from webhook payload
   - Enqueues `sync_integration_data_task` with category, provider, data types

3. **Sync Execution** (`src/workers/integrations_worker/tasks/sync_tasks.py`)
   - Task delegates to `execute_sync` dispatcher
   - Uses async database provider to avoid event loop issues

4. **Dispatcher Logic** (`src/workers/integrations_worker/services/dispatcher.py`)
   - Fetches integration record from tenant database
   - Creates `NangoClient` for API proxy requests
   - Loops through `data_types` (e.g., `["events"]`)
   - Acquires sync lock per data type (prevents duplicate syncs)
   - Calls provider-specific fetcher

5. **Fetcher** (`src/workers/integrations_worker/providers/calendar/google_calendar.py`)
   - Uses `NangoClient.proxy_request()` to call Google Calendar API via Nango
   - Endpoint: `/calendar/v3/calendars/primary/events`
   - Parameters: `timeMin`, `timeMax`, `singleEvents`, `maxResults`
   - Returns list of event dictionaries

6. **Adapter** (`src/workers/integrations_worker/adapters/calendar_adapter.py`)
   - Delegates to `SyncService.upsert_calendar_events()`
   - Creates/fetches `OrganisationSource` (type=INTEGRATION)
   - Processes events and creates `OrganisationDocument` records

7. **Database Persistence** (`src/service/integrations/sync_service.py`)
   - **Idempotency**: Uses `content_hash` = SHA256(`calendar|{provider}|{external_id}`)
   - **Upsert Logic**: Checks existing doc by `content_hash`, updates if found, inserts if new
   - **Batch Commits**: Commits every 50 events to avoid long transactions
   - **Final Commit**: Updates `integration.last_synced_at` and commits

### Database Schema

**Tables Used**:
- `KnowledgeIntegration` (tenant schema) - Integration connection metadata
- `OrganisationSource` (tenant schema) - Container for integration data (type=INTEGRATION)
- `OrganisationDocument` (tenant schema) - Individual events/records
- `OrganisationIntegrationDetails` (master schema) - Connection ID mapping

**Document Structure**:
```python
OrganisationDocument(
    source_id=source.source_id,              # Links to OrganisationSource
    name="Event: Team Meeting",              # Human-readable title
    content="Event: Team Meeting\nStart: 2025-10-19T10:00:00Z\n...",  # Formatted summary
    meta_data={"id": "abc123", ...},        # Raw provider payload
    content_hash="sha256_hash",              # Idempotency key
    is_org_wide=True,                        # Visible to all org users
)
```

### Key Components

#### 1. Nango Client (`src/service/integrations/nango_client.py`)

**Purpose**: REST API wrapper for Nango OAuth proxy.

**Key Methods**:
- `proxy_request(provider_config_key, connection_id, endpoint, params)` - Proxies API calls to provider
- `get_connection(provider, connection_id)` - Fetches connection metadata
- `verify_webhook_signature(signature, payload)` - SHA-256 webhook verification
- `refresh_token(provider, connection_id)` - Forces token refresh

**Headers Required**:
- `Provider-Config-Key`: Nango integration key (e.g., `"google-calendar"`)
- `Connection-Id`: Unique connection identifier from OAuth flow

**Example**:
```python
nango = NangoClient()
events = await nango.proxy_request(
    provider_config_key="google-calendar",
    connection_id="conn_123",
    endpoint="/calendar/v3/calendars/primary/events",
    params={"timeMin": "2025-01-01T00:00:00Z", "maxResults": 100}
)
```

#### 2. Provider Registry (`src/workers/integrations_worker/providers/registry.py`)

**Purpose**: Maps `(category, provider, data_type)` to fetcher functions.

```python
_REGISTRY = {
    ("calendar", "google"): {"events": google_calendar_fetch},
    # Future: ("crm", "hubspot"): {"contacts": hubspot_contacts_fetch}
}

fetcher = get_fetcher("calendar", "google", "events")
```

#### 3. Fetcher Functions

**Signature**:
```python
async def fetch_events(
    *,
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,                # True = full sync, False = incremental
    last_synced_at: datetime | None, # Last successful sync time
    data_type: str | None,           # "events", "calendars", etc.
    provider_config_key: str | None, # Nango provider key
) -> list[dict[str, Any]]:
```

**Time Window Logic** (Google Calendar):
- **Initial sync**: Last 90 days to next 365 days
- **Incremental sync**: `last_synced_at - 1 day` to next 365 days (1-day backoff for late updates)

#### 4. Sync Service (`src/service/integrations/sync_service.py`)

**Key Features**:
- **Idempotent Upserts**: `content_hash` prevents duplicates
- **Batch Commits**: Commits every 50 events (configurable)
- **Integration Source**: Creates synthetic `OrganisationSource` with label like `integration://google_calendar/calendar`
- **Metadata Preservation**: Stores full provider payload in `meta_data` JSON field

**Logging**:
```python
logger.info("upsert_calendar_events called: integration=%s, provider=%s, event_count=%d", ...)
logger.info("Processing %d events from provider", len(event_list))
logger.debug("Inserting new event: external_id=%s, summary=%s", external_id, summary)
logger.info("Committed batch of %d events", upserted)
logger.info("Final commit: total %d events upserted for integration", upserted)
```

### Common Issues & Debugging

#### Issue: Webhook received but no data in database

**Root Causes**:
1. **Missing Commit**: Ensure `await self.db.commit()` is called after adding documents
2. **Event Loop Issues**: Task must use `run_async()` helper or dedicated event loop
3. **Fetcher Returns Empty**: Check Nango proxy connection and API scopes
4. **Content Hash Collision**: Verify idempotency key construction
5. **Session Not Scoped**: Ensure `acquire_org_async_session(org_id)` is used

**Debug Steps**:
```bash
# 1. Check webhook logs
grep "NANGO WEBHOOK RECEIVED" logs/app.log

# 2. Check task enqueue
grep "Sync webhook enqueued" logs/app.log

# 3. Check task execution
grep "Task received: integrations.sync_integration_data" logs/celery_integrations.log

# 4. Check fetcher results
grep "Fetched.*records for data_type" logs/celery_integrations.log

# 5. Check database upserts
grep "upsert_calendar_events called" logs/celery_integrations.log
grep "Final commit" logs/celery_integrations.log

# 6. Query database directly
SELECT COUNT(*) FROM organisation_documents WHERE source_id IN (
    SELECT source_id FROM organisation_sources WHERE source_type = 'INTEGRATION'
);
```

#### Issue: Webhook signature verification fails

**Causes**:
- `NANGO_SECRET_KEY` environment variable not set
- Secret key mismatch between Nango dashboard and backend
- Webhook payload tampered in transit

**Fix**:
```bash
# Verify secret key is set
echo $NANGO_SECRET_KEY

# Check Nango dashboard: Environment Settings > Webhook Settings
# Copy secret key and update .env
```

#### Issue: Nango proxy returns 401 Unauthorized

**Causes**:
- OAuth token expired
- `connection_id` invalid or revoked
- Provider API scopes insufficient

**Fix**:
```python
# Force token refresh
nango = NangoClient()
await nango.refresh_token(provider="google-calendar", connection_id="conn_123")

# Check connection status
connection = await nango.get_connection(provider="google-calendar", connection_id="conn_123")
print(connection.get("credentials"))  # Check token expiry
```

#### Issue: Duplicate events created

**Causes**:
- `content_hash` not unique (bad idempotency key)
- Multiple workers processing same webhook
- Database commit failed midway

**Fix**:
```python
# Check content_hash logic
content_hash = hashlib.sha256()
for p in ["calendar", provider, external_id]:
    content_hash.update(p.encode("utf-8"))
    content_hash.update(b"|")
hash_key = content_hash.hexdigest()

# Ensure sync lock is working
from src.workers.integrations_worker.utils.dedupe import acquire_sync_lock
locked = acquire_sync_lock(integration_id, data_type)  # Should return False if already locked
```

### Configuration

**Environment Variables**:
```bash
# Nango Configuration
NANGO_SECRET_KEY=your_secret_key         # From Nango dashboard
NANGO_BASE_URL=https://api.nango.dev    # Default Nango cloud

# Integration Worker
MAX_TENANT_ENGINES=1                     # Minimal DB pool for worker
TENANT_POOL_MULTIPLIER=1.0
```

**Nango Provider Mapping** (`src/infra/config/nango_provider_mapping.py`):
```python
NANGO_TO_INTEGRATION_TYPE = {
    "google-calendar": IntegrationType.GOOGLE_CALENDAR,
    "outlook-calendar": IntegrationType.OUTLOOK_CALENDAR,
    # Add new providers here
}
```

**Provider Categories** (`src/infra/config/provider_categories.py`):
```python
PROVIDER_CATEGORIES = {
    IntegrationType.GOOGLE_CALENDAR: ("calendar", "google"),
    IntegrationType.OUTLOOK_CALENDAR: ("calendar", "microsoft"),
    # (category, provider) tuples
}
```

### Adding a New Integration

**Step 1**: Define integration type
```python
# src/domain/models/organisation_tables/sync/integration_models.py
class IntegrationType(str, Enum):
    HUBSPOT = "hubspot"  # Add new type
```

**Step 2**: Add Nango provider mapping
```python
# src/infra/config/nango_provider_mapping.py
NANGO_TO_INTEGRATION_TYPE = {
    "hubspot": IntegrationType.HUBSPOT,
}
```

**Step 3**: Add provider category
```python
# src/infra/config/provider_categories.py
PROVIDER_CATEGORIES = {
    IntegrationType.HUBSPOT: ("crm", "hubspot"),
}
```

**Step 4**: Create fetcher
```python
# src/workers/integrations_worker/providers/crm/hubspot_contacts.py
async def fetch_contacts(
    *,
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    data_type: str | None,
    provider_config_key: str | None,
) -> list[dict[str, Any]]:
    endpoint = "/crm/v3/objects/contacts"
    params = {"limit": 100}
    
    if not is_initial and last_synced_at:
        # Incremental sync: filter by lastModifiedDate
        params["filterGroups"] = [{
            "filters": [{
                "propertyName": "lastmodifieddate",
                "operator": "GTE",
                "value": last_synced_at.isoformat()
            }]
        }]
    
    resp = await nango.proxy_request(
        provider_config_key=provider_config_key or "hubspot",
        connection_id=connection_id,
        endpoint=endpoint,
        params=params,
    )
    
    return resp.get("results", [])
```

**Step 5**: Register fetcher
```python
# src/workers/integrations_worker/providers/registry.py
from .crm.hubspot_contacts import fetch_contacts as hubspot_contacts_fetch

def _init_defaults() -> None:
    _REGISTRY[("calendar", "google")] = {"events": google_calendar_fetch}
    _REGISTRY[("crm", "hubspot")] = {"contacts": hubspot_contacts_fetch}  # Add here
```

**Step 6**: Create adapter
```python
# src/workers/integrations_worker/adapters/crm_adapter.py
async def upsert_contacts(
    *,
    session: AsyncSession,
    organisation_id: UUID,
    integration: KnowledgeIntegration,
    provider: str,
    contacts: Iterable[dict[str, Any]],
) -> int:
    service = SyncService(session, organisation_id)
    return await service.upsert_crm_contacts(
        integration=integration, contacts=contacts, provider=provider
    )
```

**Step 7**: Update dispatcher
```python
# src/workers/integrations_worker/services/dispatcher.py
if request.category == "calendar":
    count = await upsert_calendar(...)
elif request.category == "crm":
    from ..adapters.crm_adapter import upsert_contacts
    count = await upsert_contacts(...)
```

**Step 8**: Implement upsert logic
```python
# src/service/integrations/sync_service.py
async def upsert_crm_contacts(
    self,
    *,
    integration: KnowledgeIntegration,
    contacts: Iterable[dict[str, Any]],
    provider: str,
) -> int:
    label = f"integration://{integration.integration_type.value}/crm"
    source = await self._get_or_create_integration_source(integration=integration, label=label)
    
    upserted = 0
    for contact in contacts:
        external_id = contact.get("id")
        if not external_id:
            continue
        
        name = f"{contact.get('firstname', '')} {contact.get('lastname', '')}".strip()
        email = contact.get("email")
        
        content = f"Contact: {name}\nEmail: {email}\n..."
        content_hash = self._hash_key("crm", provider, external_id)
        
        # Lookup + upsert logic similar to calendar events...
        upserted += 1
    
    await self.db.commit()
    return upserted
```

### Task Monitoring

**Celery Flower** (optional):
```bash
pip install flower
celery -A src.workers.integrations_worker.celery_app flower --port=5555
# Visit http://localhost:5555
```

**Manual Inspection**:
```python
# Check task result
from src.workers.integrations_worker.celery_app import app
result = app.AsyncResult("task_id")
print(result.state)   # PENDING, STARTED, SUCCESS, FAILURE
print(result.result)  # Task return value
```

**Database Queries**:
```sql
-- Check integration sources
SELECT * FROM organisation_sources WHERE source_type = 'INTEGRATION';

-- Check synced documents
SELECT COUNT(*), name FROM organisation_documents
WHERE source_id IN (SELECT source_id FROM organisation_sources WHERE source_type = 'INTEGRATION')
GROUP BY name
LIMIT 10;

-- Check last sync time
SELECT integration_id, integration_type, last_synced_at, last_sync_status
FROM knowledge_integrations
WHERE is_active = true;
```

---

## Quick Reference: Common Imports

```python
# Task definition
from src.workers.my_worker.celery_app import app

# Database (sync)
from src.api.dependencies.organisation_dependencies import acquire_org_session

# Database (async)
from src.infra.database.factory import get_async_provider

# Storage
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage

# Redis
from src.infra.messaging.redis_client import get_redis_client, close_redis_pool
from src.infra.messaging.redis_streams import xadd_json_frame
from src.infra.messaging.redis_channels import channel_for_chat_job

# Publishing
from src.infra.messaging.attachment_status_publish import publish_attachment_status
from src.infra.messaging.knowledge_source_status_publish import publish_source_status
from src.workers.knowledge_worker.services.progress import ProgressPublisher

# Logging
from src.infra.utils.logger import get_logger

# Models
from src.domain.models.organisation_tables.* import *

# Utilities
import asyncio
import contextlib
from uuid import UUID
from datetime import UTC, datetime
```

---

## Troubleshooting

### Task Not Received by Worker

**Check**:
1. Worker is running and connected to correct Redis instance
2. Task route defined in worker config `task_routes`
3. Queue name matches between enqueue and worker config
4. Routing key specified in both `send_task` and queue definition
5. Exchange type is `direct` (not topic/fanout)

### Task Stuck in PENDING

**Causes**:
- No worker listening to queue
- Worker crashed before acknowledging
- Task serialization failure (non-JSON-serializable args)

**Fix**:
- Check worker logs for startup errors
- Verify `acks_late=True` is set
- Ensure args are JSON-serializable (convert UUIDs to strings)

### Memory Leaks

**Solutions**:
- Set `worker_max_tasks_per_child=50` (restarts worker after N tasks)
- Close all resources in `finally` blocks
- Use dedicated event loop per task
- Monitor with `scripts/monitoring/db_pool_monitor.py`

### Redis Connection Errors

**Check**:
- Redis host/port/password in environment variables
- SSL enabled for remote Redis (disabled for localhost)
- `REDIS_SSL_CERT_REQS` set appropriately
- Connection pool closed on worker shutdown

---

## Summary

**When creating a new Celery worker, use this document to**:
1. Configure queues, exchanges, and routing (use templates)
2. Structure tasks with proper error handling and resource cleanup
3. Access database correctly (prefer sync via `acquire_org_session`)
4. Publish status updates at key lifecycle points
5. Enqueue tasks from API with correct queue/routing_key
6. Monitor and troubleshoot using signals and logging

**Core Principles**:
- **Idempotency**: Always check state before processing
- **Resilience**: Handle failures gracefully, retry transient errors
- **Observability**: Log key milestones, publish status updates
- **Resource Cleanup**: Always close sessions, storage, loops in `finally` blocks
- **Isolation**: One worker = one queue family; minimal DB pools; dedicated event loops

**Reference Workers** (battle-tested):
- **Knowledge Worker**: Long-running extraction, progress updates, multi-phase jobs
- **Chat Worker**: Fast processing, streaming, Redis Streams for frames
- **Organization Worker**: Async service integration, schema creation

Use this architecture document alongside `service-architecture-base.md` for complete API-to-worker lifecycle understanding.
