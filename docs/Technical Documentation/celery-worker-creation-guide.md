# Celery Worker Architecture: Complete Creation Guide

**Purpose**: A comprehensive technical reference for creating and maintaining Celery workers in the CopilotGTM backend. This document provides all the foundational knowledge, patterns, and step-by-step instructions required to create a new Celery worker.

**Last Updated**: December 2025

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Core Components](#2-core-components)
3. [Database Connection Architecture](#3-database-connection-architecture)
4. [Redis/Broker Configuration](#4-redisbroker-configuration)
5. [Step-by-Step: Creating a New Worker](#5-step-by-step-creating-a-new-worker)
6. [Task Implementation Patterns](#6-task-implementation-patterns)
7. [Signal Handlers & Lifecycle](#7-signal-handlers--lifecycle)
8. [Error Handling & Retry Strategies](#8-error-handling--retry-strategies)
9. [Status Publishing & Real-time Updates](#9-status-publishing--real-time-updates)
10. [Environment Configuration](#10-environment-configuration)
11. [Existing Workers Reference](#11-existing-workers-reference)
12. [Troubleshooting Guide](#12-troubleshooting-guide)
13. [Checklist for New Workers](#13-checklist-for-new-workers)

---

## 1. Architecture Overview

### High-Level Architecture

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                           CopilotGTM Backend                            │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────────────┐  │
│  │   FastAPI    │───►│    Redis     │◄───│    Celery Workers        │  │
│  │   (API)      │    │   (Broker)   │    │  ┌────────────────────┐  │  │
│  └──────────────┘    └──────────────┘    │  │   chat_worker      │  │  │
│         │                   │            │  │   knowledge_worker │  │  │
│         │                   │            │  │   integrations_    │  │  │
│         │                   │            │  │   organization_    │  │  │
│         │                   │            │  │   deal_worker      │  │  │
│         ▼                   ▼            │  │   content_worker   │  │  │
│  ┌──────────────────────────────────┐    │  │   notetaker_worker │  │  │
│  │      PostgreSQL (Multi-Tenant)   │◄───┤  └────────────────────┘  │  │
│  │  ┌─────────┐  ┌─────────┐       │    │                           │  │
│  │  │ Master  │  │ Tenant  │ ...   │    │  ┌────────────────────┐   │  │
│  │  │ Schema  │  │ Schemas │       │    │  │  Azure Key Vault   │   │  │
│  │  └─────────┘  └─────────┘       │    │  │  (Tenant Secrets)  │   │  │
│  └──────────────────────────────────┘    │  └────────────────────┘   │  │
│                                          └──────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
```

### Task Flow

1. **API Layer** receives request, creates job record in database
2. **API Layer** enqueues task to Redis via `app.send_task()`
3. **Redis** (broker) stores task in appropriate queue
4. **Celery Worker** picks up task from its queue
5. **Worker** acquires database session for the tenant
6. **Worker** processes task, updates database, publishes status
7. **Worker** cleans up resources and acknowledges task

### Directory Structure

```text
src/workers/
├── base/                          # Shared base components
│   ├── __init__.py
│   ├── celery_base.py            # Factory function for creating Celery apps
│   └── worker_config.py          # Base configuration class
├── chat_worker/                   # Example worker
│   ├── __init__.py
│   ├── celery_app.py             # Celery app instance + signals
│   ├── config.py                 # Worker-specific config
│   ├── README.md                 # Worker documentation
│   └── tasks/                    # Task modules
│       ├── __init__.py
│       ├── message_processing.py
│       ├── post_upload.py
│       └── test_tasks.py
├── knowledge_worker/
├── integrations_worker/
├── organization_worker/
├── deal_worker/
├── content_worker/
├── notetaker_worker/
├── shared/                        # Shared utilities across workers
└── utils/                         # Worker utilities
```

---

## 2. Core Components

### 2.1 Base Worker Config (`src/workers/base/worker_config.py`)

The `BaseWorkerConfig` class provides shared configuration for all workers:

```python
class BaseWorkerConfig:
    """Base configuration for all workers"""

    def __init__(self):
        # Load environment variables
        load_dotenv(override=True)

        # Get configuration singleton
        self.config = Config()
        
        # Redis connection details from config
        self.redis_host, self.redis_port, self.redis_password = self.config.get_redis_client_details
        
        # Build broker URL (redis:// or rediss:// based on environment)
        self.broker_url = self._get_broker_url()
        self.backend_url = self.broker_url
        
        # SSL configuration for remote Redis
        self.ssl_config = self._get_ssl_config()

    def _get_broker_url(self):
        # Use redis:// for localhost, rediss:// for remote (with SSL)
        ssl_enabled = not (
            self.redis_host in ["localhost", "127.0.0.1"] 
            or self.redis_host.startswith("192.168.")
        )
        redis_scheme = "redis" if not ssl_enabled else "rediss"
        redis_password_part = f":{self.redis_password}@" if self.redis_password else ""
        return f"{redis_scheme}://{redis_password_part}{self.redis_host}:{self.redis_port}/0"

    def _get_ssl_config(self):
        # Returns SSL configuration for remote Redis connections
        # Uses REDIS_SSL_CERT_REQS env var (CERT_REQUIRED default)
        ...
```

**Key Points:**

- Automatically determines SSL usage based on host (localhost = no SSL)
- Reads Redis connection details from `Config()` singleton
- SSL certificate requirements configurable via `REDIS_SSL_CERT_REQS` env var

### 2.2 Celery App Factory (`src/workers/base/celery_base.py`)

The `create_celery_app()` factory function creates consistent Celery apps:

```python
def create_celery_app(worker_name, config, tasks_module):
    """Factory function to create a Celery app for a specific worker."""
    
    celery_app = Celery(
        worker_name,
        broker=config.broker_url,
        backend=config.backend_url,
        include=[tasks_module],
    )

    # Apply SSL if configured
    if config.ssl_config:
        celery_app.conf.broker_use_ssl = config.ssl_config
        celery_app.conf.redis_backend_use_ssl = config.ssl_config

    # Apply queue configurations
    if hasattr(config, "task_queues"):
        celery_app.conf.task_queues = config.task_queues
    
    # ... routing, exchange configurations ...

    # Essential Celery configuration
    celery_app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        worker_prefetch_multiplier=1,    # Fair task distribution
        task_acks_late=True,             # Prevent task loss on crash
        worker_disable_rate_limits=True,
        broker_connection_retry_on_startup=True,
        broker_connection_retry=True,
        broker_connection_max_retries=10,
        task_track_started=True,
        task_time_limit=default_time_limit,
        task_soft_time_limit=default_soft_time_limit,
        worker_hijack_root_logger=False,
        worker_log_color=False,
    )

    return celery_app
```

**Critical Configuration:**

| Setting | Value | Purpose |
|---------|-------|---------|
| `worker_prefetch_multiplier` | 1 | Fair task distribution (worker takes 1 task at a time) |
| `task_acks_late` | True | Acknowledge after completion (prevents task loss on crash) |
| `task_track_started` | True | Track when task starts execution |
| `task_time_limit` | 300s (default) | Hard limit before task is killed |
| `task_soft_time_limit` | 240s (default) | Soft limit (raises SoftTimeLimitExceeded) |

---

## 3. Database Connection Architecture

### 3.1 Multi-Tenant Database Design

The system uses a **schema-per-tenant** approach:

```text
PostgreSQL Database
├── public (master schema)
│   ├── users
│   ├── organisations
│   └── admins
├── org_<uuid> (tenant schema 1)
│   ├── chat_sessions
│   ├── chat_messages
│   ├── organisation_sources
│   └── ...
├── org_<uuid> (tenant schema 2)
│   └── ...
```

### 3.2 Database Providers

Two providers handle database connections:

**Sync Provider** (`src/infra/database/provider_sync.py`):

```python
class SyncSqlAlchemyProvider:
    def __init__(self, *, tenant_pool_max=100, tenant_pool_multiplier=5.0):
        self._tenant_engines: dict[UUID, Engine] = {}  # LRU cache
        self._tenant_pool_max = tenant_pool_max
```

**Async Provider** (`src/infra/database/provider_async.py`):

```python
class AsyncSqlAlchemyProvider:
    def __init__(self, *, tenant_pool_max=100, tenant_pool_multiplier=1.0):
        self._tenant_engines: dict[UUID, AsyncEngine] = {}
```

### 3.3 Acquiring Database Sessions in Workers

**Sync Session (Preferred for most tasks):**

```python
from src.api.dependencies.organisation_dependencies import acquire_org_session

def my_task(organisation_id: str, item_id: str):
    # Get session generator
    session_gen = acquire_org_session(organisation_id)
    session = next(session_gen)
    
    try:
        # Use session
        item = session.get(MyModel, item_id)
        item.status = "COMPLETED"
        session.commit()
    finally:
        # ALWAYS clean up
        with contextlib.suppress(Exception):
            session.close()
        with contextlib.suppress(Exception):
            session_gen.close()
```

**Async Session (When needed for async operations):**

```python
from src.api.dependencies.organisation_dependencies import acquire_org_async_session

async def process_async(organisation_id: UUID):
    async for session in acquire_org_async_session(organisation_id):
        result = await session.execute(query)
        await session.commit()
        return result.scalars().all()
```

### 3.4 Worker-Specific Database Pool Settings

Workers set minimal pool settings to conserve resources:

```python
import os

# Set at the TOP of celery_app.py, BEFORE any other imports
os.environ["MAX_TENANT_ENGINES"] = "1"
os.environ["TENANT_POOL_MULTIPLIER"] = "1.0"
```

**Why?**

- Workers process one task at a time (solo pool)
- Don't need large connection pools
- Reduces memory usage and database connections
- Prevents resource exhaustion in multi-worker deployments

### 3.5 Tenant Secret Management

Each tenant has its own database credentials stored in Azure Key Vault:

```python
# Resource naming utilities
from src.infra.utils.resource_naming import (
    get_key_vault_secret_name,  # Returns: "org-<uuid>-db-password"
    get_role_name,               # Returns: "org_<uuid>"
    get_schema_name,             # Returns: "org_<uuid>"
)

# Secrets are fetched via:
from src.infra.secrets.manager import secrets_manager
password = await secrets_manager.get(secret_name)
```

---

## 4. Redis/Broker Configuration

### 4.1 Environment Configuration

Redis configuration is loaded from environment variables:

```python
# In src/infra/config/constant.py
CONFIG_MAP = {
    "local": {
        "redis_client": {
            "host": os.getenv("REDIS_HOST", "localhost"),
            "port": int(os.getenv("REDIS_PORT", 6379)),
            "password": os.getenv("REDIS_PASSWORD", None),
        },
    },
    "dev": { ... },
    "prod": { ... },
}
```

### 4.2 SSL Configuration

SSL is automatically enabled for remote Redis:

```python
# In BaseWorkerConfig
ssl_enabled = not (
    self.redis_host in ["localhost", "127.0.0.1"] 
    or self.redis_host.startswith("192.168.")
)

# SSL options via environment
REDIS_SSL_CERT_REQS = "CERT_REQUIRED"  # Options: CERT_NONE, CERT_OPTIONAL, CERT_REQUIRED
```

### 4.3 Redis Connection Pool Management

Workers manage Redis connection pools via signals:

```python
from src.infra.messaging.redis_client import (
    initialize_redis_pool,
    close_redis_pool,
    get_redis_client,
)

@worker_ready.connect
def worker_ready_handler(sender=None, **kwargs):
    initialize_redis_pool()

@worker_shutdown.connect
def worker_shutdown_handler(sender=None, **kwargs):
    close_redis_pool()
```

**RedisConnectionManager** is a singleton that:

- Lazily initializes connection pool on first use
- Tests connection with PING on initialization
- Provides thread-safe access to Redis client
- Properly closes pool on shutdown

---

## 5. Step-by-Step: Creating a New Worker

### Step 1: Create Worker Directory Structure

```text
src/workers/my_worker/
├── __init__.py           # Package marker
├── celery_app.py         # Celery app instance
├── config.py             # Worker configuration
├── README.md             # Worker documentation
└── tasks/                # Task modules
    ├── __init__.py
    └── process.py        # Task implementations
```

### Step 2: Create Worker Config (`config.py`)

```python
from celery.schedules import crontab  # If using beat schedule
from kombu import Exchange, Queue

from src.workers.base.worker_config import BaseWorkerConfig


class MyWorkerConfig(BaseWorkerConfig):
    """Configuration for My Worker."""

    def __init__(self):
        super().__init__()
        
        # Time limits (override defaults if needed)
        self.task_time_limit = 300       # 5 minutes hard limit
        self.task_soft_time_limit = 240  # 4 minutes soft limit
        
        # Define exchange (use direct type for explicit routing)
        my_exchange = Exchange("my_worker", type="direct")
        
        # Exchange defaults
        self.task_default_exchange = "my_worker"
        self.task_default_exchange_type = "direct"
        self.task_default_queue = "my_processing"
        self.task_default_routing_key = "my_processing"
        
        # Define queues
        self.task_queues = (
            Queue(
                "my_processing",
                exchange=my_exchange,
                routing_key="my_processing",
            ),
            Queue(
                "my_priority",
                exchange=my_exchange,
                routing_key="my_priority",
            ),
        )
        
        # Route tasks to queues
        self.task_routes = {
            "my_worker.process": {
                "queue": "my_processing",
                "routing_key": "my_processing",
            },
            "my_worker.urgent_process": {
                "queue": "my_priority",
                "routing_key": "my_priority",
            },
        }
        
        # Optional: Periodic tasks (Celery Beat)
        self.beat_schedule = {
            "periodic-task-name": {
                "task": "my_worker.periodic_task",
                "schedule": crontab(minute=0, hour="*/3"),  # Every 3 hours
                "options": {
                    "queue": "my_processing",
                    "routing_key": "my_processing",
                },
            },
        }
```

### Step 3: Create Celery App (`celery_app.py`)

```python
import os

# CRITICAL: Set DB pool limits BEFORE any other imports
os.environ["MAX_TENANT_ENGINES"] = "1"
os.environ["TENANT_POOL_MULTIPLIER"] = "1.0"

import contextlib

from celery.signals import (
    task_failure,
    task_postrun,
    task_prerun,
    task_received,
    worker_ready,
    worker_shutdown,
)
from dotenv import load_dotenv

from src.infra.messaging.redis_client import close_redis_pool, initialize_redis_pool
from src.infra.utils.logger import get_logger
from src.workers.base.celery_base import create_celery_app
from src.workers.my_worker.config import MyWorkerConfig

logger = get_logger("my_worker")

# Load environment variables
load_dotenv(override=True)

# Initialize configuration
config = MyWorkerConfig()

# Define tasks module path
tasks_module = "src.workers.my_worker.tasks"

# Create Celery app
app = create_celery_app("my_worker", config, tasks_module)

# Add worker-specific configuration (optional)
app.conf.update(
    worker_log_format="[%(asctime)s: %(levelname)s/%(processName)s] %(message)s",
    worker_task_log_format="[%(asctime)s: %(levelname)s/%(processName)s][%(task_name)s(%(task_id)s)] %(message)s",
    # For long-running tasks, consider:
    # worker_max_tasks_per_child=50,  # Restart worker after N tasks (memory management)
    # result_expires=7200,            # Results expire after 2 hours
)

# Optional: Register DB pool monitor
with contextlib.suppress(Exception):
    import scripts.monitoring.celery_integration  # noqa: F401


# Signal Handlers
@worker_ready.connect
def worker_ready_handler(sender=None, **kwargs):
    """Initialize worker resources when worker is ready."""
    logger.info("My worker starting. Initializing Redis connection pool...")
    initialize_redis_pool()
    logger.info("My worker is ready. Redis connection pool initialized.")


@worker_shutdown.connect
def worker_shutdown_handler(sender=None, **kwargs):
    """Clean up worker resources on shutdown."""
    logger.info("My worker shutting down. Closing Redis connection pool.")
    try:
        close_redis_pool()
        logger.info("Redis connection pool closed successfully")
    except Exception as e:
        logger.error(f"Error closing Redis connection pool: {e}")


@task_received.connect
def task_received_handler(sender=None, headers=None, body=None, **kwargs):
    logger.debug(f"Task received: {headers}")


@task_prerun.connect
def task_prerun_handler(task_id=None, task=None, args=None, kwargs=None, **kwds):
    logger.info(f"Task starting: {task.name}({task_id})")


@task_postrun.connect
def task_postrun_handler(task_id=None, task=None, args=None, kwargs=None, retval=None, state=None, **kwds):
    logger.info(f"Task finished: {task.name}({task_id}) state={state}")


@task_failure.connect
def task_failure_handler(task_id=None, exception=None, traceback=None, einfo=None, **kwds):
    logger.error(f"Task failed: {task_id} exception={exception}")


# Import task modules to ensure registration
# from .tasks import process  # noqa: F401, E402
```

### Step 4: Create Tasks Module (`tasks/__init__.py`)

```python
# Import all task modules to register with Celery
from .process import process_task  # noqa: F401
```

### Step 5: Implement Tasks (`tasks/process.py`)

```python
from __future__ import annotations

import asyncio
import contextlib
from uuid import UUID

from src.api.dependencies.organisation_dependencies import acquire_org_session
from src.domain.models.organisation_tables.my_models import MyModel, MyStatus
from src.infra.utils.logger import get_logger
from src.workers.my_worker.celery_app import app

logger = get_logger("my_worker_tasks")


@app.task(
    name="my_worker.process",
    bind=True,          # Gives access to self (task instance)
    acks_late=True,     # Acknowledge after completion
    max_retries=3,      # Maximum retry attempts
)
def process_task(self, organisation_id: str, item_id: str):
    """Process an item for the given organization.
    
    Args:
        organisation_id: UUID of the organization (string for JSON serialization)
        item_id: UUID of the item to process (string for JSON serialization)
    """
    logger.info(f"Processing item {item_id} for org {organisation_id}")
    
    # Acquire database session
    session_gen = acquire_org_session(UUID(organisation_id))
    session = next(session_gen)
    
    # Create event loop for async operations (if needed)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        # 1. Load and validate item
        item = session.get(MyModel, UUID(item_id))
        if not item:
            logger.warning(f"Item {item_id} not found")
            return
        
        if item.status in [MyStatus.COMPLETED, MyStatus.FAILED]:
            logger.info(f"Item {item_id} already processed, skipping")
            return
        
        # 2. Update status to IN_PROGRESS
        item.status = MyStatus.IN_PROGRESS
        session.commit()
        
        # 3. Perform processing logic
        result = _do_processing(item)
        
        # 4. Update status to COMPLETED
        item.status = MyStatus.COMPLETED
        item.result_data = result
        session.commit()
        
        logger.info(f"Successfully processed item {item_id}")
        
    except self.MaxRetriesExceededError:
        logger.error(f"Max retries exceeded for item {item_id}")
        item.status = MyStatus.FAILED
        item.error_message = "Max retries exceeded"
        session.commit()
        
    except Exception as e:
        logger.exception(f"Error processing item {item_id}: {e}")
        # Retry with exponential backoff
        raise self.retry(exc=e, countdown=60 * (2 ** self.request.retries))
        
    finally:
        # ALWAYS clean up resources
        with contextlib.suppress(Exception):
            session.close()
        with contextlib.suppress(Exception):
            session_gen.close()
        loop.close()


def _do_processing(item: MyModel) -> dict:
    """Actual processing logic."""
    # Your business logic here
    return {"processed": True}
```

### Step 6: Create README (`README.md`)

```markdown
# My Worker

## Purpose
Brief description of what this worker does.

## Queues
- `my_processing`: Standard processing tasks
- `my_priority`: High-priority tasks

## Tasks
- `my_worker.process`: Process an item
- `my_worker.urgent_process`: Priority processing

## Running Locally
```bash
celery -A src.workers.my_worker.celery_app worker -l info -Q my_processing,my_priority --pool=solo
```

## Environment Variables

- `REDIS_HOST`: Redis host (default: localhost)
- `REDIS_PORT`: Redis port (default: 6379)

### Step 7: Create Async Client for API Usage

> **⚠️ CRITICAL**: Never import `celery_app.py` directly in FastAPI API handlers. This causes the full Celery app to initialize at module import time, leading to blocking issues.

#### Why Async Clients Are Required

The Celery `send_task()` method is **synchronous and blocking**. When called from an async FastAPI endpoint, it can block the entire asyncio event loop, causing:

1. **Request Hangs**: The API request appears stuck while `send_task()` establishes a Redis connection
2. **Cold Start Issues**: First requests after pod restart hang because connection isn't established
3. **Intermittent Failures**: Works locally (fast localhost Redis) but fails in production (remote Redis with SSL)
4. **Connection Timeout Issues**: SSL handshake + authentication to Azure Redis can take 100ms-2s

#### The Problem (Don't Do This)

```python
# ❌ WRONG: Direct import initializes Celery app at module load
from src.workers.my_worker.celery_app import app as my_worker_app

@router.post("/trigger")
async def trigger_endpoint():
    # This BLOCKS the event loop while connecting to Redis!
    my_worker_app.send_task("my_worker.process", args=[...])
```

#### The Solution: Async Client Module

Create a lightweight client module (`client.py`) for each worker:

**File: `src/workers/my_worker/client.py`**

```python
"""
Lightweight Celery client for sending tasks to the my_worker.

This module provides async wrappers that run send_task in a thread pool
to avoid blocking the asyncio event loop.
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from celery import Celery

from src.infra.utils.logger import get_logger

logger = get_logger("my_worker_client")

# Thread-safe lazy initialization
_client_lock = threading.Lock()
_my_client: Celery | None = None

# Thread pool for running blocking Celery operations
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="celery_my_client")


def _create_client() -> Celery:
    """Create a minimal Celery client for sending tasks only."""
    from src.workers.base.worker_config import BaseWorkerConfig

    config = BaseWorkerConfig()

    client = Celery(
        "my_client",
        broker=config.broker_url,
        backend=config.backend_url,
    )

    if config.ssl_config:
        client.conf.broker_use_ssl = config.ssl_config
        client.conf.redis_backend_use_ssl = config.ssl_config

    client.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        broker_connection_retry_on_startup=True,
        broker_connection_retry=True,
        broker_connection_max_retries=5,
        broker_connection_timeout=10,  # Prevent indefinite blocking
        worker_hijack_root_logger=False,
    )

    logger.info(
        "My Celery client initialized (broker=%s)",
        config.broker_url.replace(config.redis_password, "****")
        if config.redis_password
        else config.broker_url,
    )

    return client


def get_celery_client() -> Celery:
    """Get or create the singleton Celery client (lazy initialization)."""
    global _my_client

    if _my_client is None:
        with _client_lock:
            if _my_client is None:
                _my_client = _create_client()

    return _my_client


def send_task(
    task_name: str,
    args: list | None = None,
    kwargs: dict | None = None,
    queue: str = "my_processing",
    routing_key: str | None = None,
) -> str:
    """Send a task (synchronous - use send_task_async in async contexts)."""
    client = get_celery_client()
    result = client.send_task(
        task_name,
        args=args or [],
        kwargs=kwargs or {},
        queue=queue,
        routing_key=routing_key or queue,
    )
    return result.id


async def send_task_async(
    task_name: str,
    args: list | None = None,
    kwargs: dict | None = None,
    queue: str = "my_processing",
    routing_key: str | None = None,
) -> str:
    """Send a task asynchronously (non-blocking for asyncio)."""
    loop = asyncio.get_event_loop()
    task_id = await loop.run_in_executor(
        _executor,
        send_task,
        task_name,
        args,
        kwargs,
        queue,
        routing_key,
    )
    return task_id
```

#### Usage in FastAPI Handlers

```python
# ✅ CORRECT: Import async client, not celery_app
from src.workers.my_worker.client import send_task_async

@router.post("/trigger")
async def trigger_endpoint(organisation_id: UUID, item_id: UUID):
    """Enqueue a processing task (non-blocking)."""
    try:
        task_id = await send_task_async(
            "my_worker.process",
            args=[str(organisation_id), str(item_id)],
            queue="my_processing",
            routing_key="my_processing",
        )
        logger.info("Enqueued task %s", task_id)
        return {"task_id": task_id}
    except Exception as e:
        logger.error("Failed to enqueue task: %s", e)
        raise HTTPException(status_code=500, detail="Failed to schedule task")
```

#### Key Benefits of Async Client Pattern

| Aspect | Direct Import | Async Client |
|--------|---------------|--------------|
| Module Load | Initializes full Celery app | No initialization until first use |
| Event Loop | Blocks on `send_task()` | Runs in thread pool |
| Cold Start | Can hang indefinitely | Has connection timeout (10s) |
| Memory | Loads all worker code | Minimal client only |
| Signal Handlers | Registers worker signals | No signal handlers |

#### Existing Async Clients

| Worker | Client Module | Key Functions |
|--------|---------------|---------------|
| chat_worker | `src/workers/chat_worker/client.py` | `send_chat_task_async()` |
| knowledge_worker | `src/workers/knowledge_worker/client.py` | `send_knowledge_task_async()` |
| deal_worker | `src/workers/deal_worker/client.py` | `send_deal_task_async()` |
| organization_worker | `src/workers/organization_worker/client.py` | `send_organization_task_async()`, `send_organization_task_and_wait_async()` |
| content_worker | `src/workers/content_worker/client.py` | `send_content_task_async()` |
| integrations_worker | `src/workers/integrations_worker/client.py` | `send_integrations_task_async()` |

When creating a new worker, **always create a corresponding `client.py`** for API usage.

---

## 6. Task Implementation Patterns

### 6.1 Standard Task Pattern

```python
@app.task(name="worker.action", bind=True, acks_late=True, max_retries=3)
def task_function(self, org_id: str, item_id: str):
    # 1. Initialize resources
    session_gen = acquire_org_session(UUID(org_id))
    session = next(session_gen)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        # 2. Load and validate
        item = session.get(Model, UUID(item_id))
        if not item or item.status in [Status.COMPLETED, Status.FAILED]:
            return  # Idempotency check
        
        # 3. Mark as in-progress
        item.status = Status.IN_PROGRESS
        session.commit()
        
        # 4. Process
        result = do_work(item)
        
        # 5. Mark complete
        item.status = Status.COMPLETED
        item.result = result
        session.commit()
        
    except self.MaxRetriesExceededError:
        item.status = Status.FAILED
        session.commit()
    except Exception as e:
        raise self.retry(exc=e, countdown=60)
    finally:
        # 7. ALWAYS cleanup
        with contextlib.suppress(Exception):
            session.close()
            session_gen.close()
        loop.close()
```

### 6.2 Async Operations in Tasks

For async code within sync Celery tasks:

```python
from src.infra.utils.async_runner import run_async

@app.task(name="worker.async_action", bind=True, acks_late=True)
def async_task(self, org_id: str, item_id: str):
    async def _run():
        from src.infra.database.factory import get_async_provider
        provider = get_async_provider()
        async with provider.tenant_session(UUID(org_id)) as session:
            # Async database operations
            result = await session.execute(query)
            return result.scalars().all()
    
    # Use thread-local event loop (persists across tasks)
    result = run_async(_run())
    return result
```

### 6.3 Long-Running Task Pattern (Knowledge Worker)

```python
@app.task(
    name="knowledge.document_extraction",
    bind=True,
    acks_late=True,
    max_retries=3,
    time_limit=1800,        # 30 minutes hard limit
    soft_time_limit=1680,   # 28 minutes soft limit
)
def document_extraction(self, organisation_id: UUID, source_id: UUID):
    from billiard.exceptions import TimeLimitExceeded
    from celery.exceptions import SoftTimeLimitExceeded
    
    try:
        # Processing logic
        ...
    except (SoftTimeLimitExceeded, TimeLimitExceeded) as e:
        logger.error(f"Task timed out: {e}")
        # Mark as failed with timeout message
        _mark_failed(session, source, "Document extraction timed out")
        return
    except Exception as e:
        # Handle other errors
        raise self.retry(exc=e, countdown=60)
```

### 6.4 Storage Operations Pattern

```python
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage

@app.task(name="worker.with_storage")
def task_with_storage(self, org_id: str, source_id: str):
    storage = AzureOrganisationBlobStorage(organisation_id=org_id)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        # Download file
        content = loop.run_until_complete(storage.download_blob(blob_path))
        
        # Upload result
        loop.run_until_complete(storage.upload_blob(result_path, result_content))
        
        # Generate SAS URL
        url = loop.run_until_complete(storage.generate_read_sas(path, minutes=60))
        
    finally:
        # Close storage connection
        try:
            loop.run_until_complete(storage.close())
        except Exception:
            pass
        finally:
            loop.close()
```

---

## 7. Signal Handlers & Lifecycle

### Available Signals

| Signal | When Fired | Use Case |
|--------|-----------|----------|
| `worker_ready` | Worker finished startup | Initialize pools, connections |
| `worker_shutdown` | Worker is shutting down | Close connections, cleanup |
| `task_received` | Task received from broker | Logging, metrics |
| `task_prerun` | Task about to execute | Logging, setup |
| `task_postrun` | Task finished executing | Cleanup, metrics |
| `task_failure` | Task raised exception | Error logging, alerts |

### Standard Signal Setup

```python
from celery.signals import (
    worker_ready,
    worker_shutdown,
    task_received,
    task_prerun,
    task_postrun,
    task_failure,
)

@worker_ready.connect
def on_ready(sender=None, **kwargs):
    initialize_redis_pool()
    # Optional: Initialize other resources

@worker_shutdown.connect
def on_shutdown(sender=None, **kwargs):
    close_redis_pool()
    # Optional: Close other resources

@task_prerun.connect
def on_task_start(task_id=None, task=None, **kwargs):
    logger.info(f"Starting: {task.name}")

@task_postrun.connect
def on_task_end(task_id=None, task=None, state=None, **kwargs):
    logger.info(f"Finished: {task.name} ({state})")

@task_failure.connect
def on_task_fail(task_id=None, exception=None, **kwargs):
    logger.error(f"Failed: {task_id} - {exception}")
```

---

## 8. Error Handling & Retry Strategies

### 8.1 Retry Configuration

```python
@app.task(
    bind=True,
    max_retries=3,                    # Maximum attempts
    default_retry_delay=60,           # Default delay between retries
    autoretry_for=(TransientError,),  # Auto-retry for specific exceptions
    retry_backoff=True,               # Exponential backoff
    retry_backoff_max=600,            # Max 10 minutes between retries
    retry_jitter=True,                # Add randomness to prevent thundering herd
)
def my_task(self, ...):
    ...
```

### 8.2 Manual Retry with Exponential Backoff

```python
@app.task(bind=True, max_retries=3)
def my_task(self, org_id, item_id):
    try:
        # Processing
        ...
    except TransientError as e:
        # Exponential backoff: 60, 120, 240 seconds
        countdown = 60 * (2 ** self.request.retries)
        raise self.retry(exc=e, countdown=countdown)
```

### 8.3 Non-Retryable Errors

```python
NON_RETRYABLE_ERRORS = {
    "integration_not_found",
    "validation_failed",
    "permission_denied",
}

@app.task(bind=True, max_retries=3)
def my_task(self, ...):
    try:
        result = do_work()
    except Exception as e:
        error_type = classify_error(e)
        if error_type in NON_RETRYABLE_ERRORS:
            logger.error(f"Non-retryable error: {e}")
            return {"status": "failed", "error": str(e)}
        raise self.retry(exc=e)
```

### 8.4 MaxRetriesExceeded Handling

```python
@app.task(bind=True, max_retries=3)
def my_task(self, org_id, item_id):
    session_gen = acquire_org_session(org_id)
    session = next(session_gen)
    item = None
    
    try:
        item = session.get(Model, item_id)
        # Processing
        ...
    except self.MaxRetriesExceededError:
        # Final failure - mark as permanently failed
        if item:
            item.status = Status.FAILED
            item.error_message = "Maximum retries exceeded"
            session.commit()
        logger.error(f"Permanently failed: {item_id}")
    except Exception as e:
        raise self.retry(exc=e, countdown=60)
    finally:
        with contextlib.suppress(Exception):
            session.close()
            session_gen.close()
```

---

## 9. Status Publishing & Real-time Updates

### 9.1 Attachment Status Updates

```python
from src.infra.messaging.attachment_status_publish import publish_attachment_status
from src.api.schemas.upload_models import AttachmentEventUpdate

def _publish_status(org_id: str, attachment: Attachment, status: AttachmentStatus):
    event = AttachmentEventUpdate(
        attachment_id=str(attachment.attachment_id),
        status=status,
        updated_at=attachment.updated_at.isoformat(),
        error_message=attachment.processing_error,
        thumbnail_small_url=thumbnail_url,
    )
    publish_attachment_status(org_id, event)
```

### 9.2 Knowledge Source Progress

```python
from src.workers.knowledge_worker.services.progress import ProgressPublisher

publisher = ProgressPublisher(
    organisation_id=org_id,
    source_id=source_id,
)
publisher.publish(
    status=OrganisationSourceStatus.PROCESSING.value,
    progress=50,
)
```

### 9.3 Chat Streaming (Redis Streams)

```python
from src.infra.messaging.redis_streams import xadd_json_frame
from src.infra.messaging.redis_channels import channel_for_chat_job

stream_key = channel_for_chat_job(organisation_id, job_id)

frame = {
    "type": "answer.delta",
    "content": "Response chunk...",
    "job_id": str(job_id),
    "timestamp": time.time(),
    "sequence": sequence_counter,
}

xadd_json_frame(stream_key, frame, maxlen=500, approximate=True)
```

### 9.4 Channel Naming Conventions

| Purpose | Pattern | Example |
|---------|---------|---------|
| Attachments | `att:{org_id}:attachments` | `att:123e4567:attachments` |
| Knowledge Sources | `kb:{org_id}:sources` | `kb:123e4567:sources` |
| Chat Streaming | `stream:{org_id}:chat:{job_id}` | `stream:123e4567:chat:abc123` |

---

## 10. Environment Configuration

### 10.1 Required Environment Variables

```bash
# Redis
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=

# Environment
ENV=local  # local, dev, prod

# Database
DATABASE_HOST=localhost
DATABASE_PORT=5432
DATABASE_USER=postgres
DATABASE_PASSWORD=
DATABASE_NAME=postgres
DATABASE_SSL_MODE=require
DATABASE_POOL_SIZE=5
DATABASE_MAX_OVERFLOW=2

# Azure (for remote deployments)
AZURE_KEY_VAULT_URL=https://your-vault.vault.azure.net/

# SSL Configuration
REDIS_SSL_CERT_REQS=CERT_REQUIRED  # CERT_NONE, CERT_OPTIONAL, CERT_REQUIRED
```

### 10.2 Worker-Specific Environment Variables

```bash
# Database pool limits (set in celery_app.py before imports)
MAX_TENANT_ENGINES=1
TENANT_POOL_MULTIPLIER=1.0

# Pool monitoring
DB_POOL_MONITOR_ENABLE=true
DB_POOL_MONITOR_INTERVAL_SEC=300
WORKER_DB_CONN_MAX_CHECKOUT_SEC=1200
```

### 10.3 Running Workers

```bash
# Development (single worker, solo pool)
celery -A src.workers.my_worker.celery_app worker -l info -Q my_processing,my_priority --pool=solo

# With Celery Beat (for scheduled tasks)
celery -A src.workers.integrations_worker.celery_app beat -l info

# Production (with concurrency)
celery -A src.workers.my_worker.celery_app worker -l warning -Q my_processing --concurrency=4

# Check broker connectivity
celery -A src.workers.my_worker.celery_app inspect ping
```

---

## 11. Existing Workers Reference

### Worker Comparison Table

| Worker | Queues | Time Limit | Purpose |
|--------|--------|------------|---------|
| **chat_worker** | `attachments`, `chat_processing`, `chat_messages` | 5 min | File processing, message handling, AI streaming |
| **knowledge_worker** | `knowledge_upload`, `knowledge_extract` | 30 min | Document parsing, embeddings, vector storage |
| **integrations_worker** | `integrations`, `integrations_priority` | 5 min | OAuth refresh, data sync from external APIs |
| **organization_worker** | `organization`, `admin` | 5 min | Tenant setup, schema creation |
| **deal_worker** | `deal`, `deal_jobs` | 5 min | Deal processing, meeting matching |
| **content_worker** | - | - | Content-related processing |
| **notetaker_worker** | - | - | Note-taking/transcription tasks |

### Worker Queue Configurations

```python
# Chat Worker
task_queues = (
    Queue("attachments", ...),      # File uploads
    Queue("chat_processing", ...),  # General chat tasks
    Queue("chat_messages", ...),    # Message processing
)

# Knowledge Worker
task_queues = (
    Queue("knowledge_extract", ...),  # Heavy extraction
    Queue("knowledge_upload", ...),   # Post-upload processing
)

# Integrations Worker
task_queues = (
    Queue("integrations", ...),          # Standard sync
    Queue("integrations_priority", ...),  # Manual refresh
)
```

### Beat Schedules (Periodic Tasks)

```python
# Integrations Worker
beat_schedule = {
    "refresh-expiring-oauth-tokens": {
        "task": "integrations.refresh_expiring_tokens",
        "schedule": crontab(minute=0),  # Every hour
    },
    "sync-teams-channels-all-orgs": {
        "task": "integrations.sync_teams_channels",
        "schedule": crontab(minute=0, hour="*/3"),  # Every 3 hours
    },
}

# Deal Worker
beat_schedule = {
    "process-deal-meetings-all-orgs": {
        "task": "deal.trigger_deal_meeting_processing_all_orgs",
        "schedule": crontab(minute=0, hour="*/3"),  # Every 3 hours
    },
}
```

### Integration Worker Deep Dive

The integrations worker has a specialized architecture for handling external API synchronization:

**Architecture Flow:**

```text
Nango Webhook → API Handler → Celery Task → Dispatcher → Fetcher → Adapter → Database
```

**Webhook Flow:**

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

**Key Components:**

```python
# Nango Client (nango_client.py)
async def proxy_request(provider_config_key, connection_id, endpoint, params):
    # Proxies API calls to provider via Nango
    # Headers: Provider-Config-Key, Connection-Id

# Provider Registry (providers/registry.py)
_REGISTRY = {
    ("calendar", "google"): {"events": google_calendar_fetch},
}

# Fetcher Signature
async def fetch_events(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,  # Full sync vs incremental
    last_synced_at: datetime | None,
    data_type: str | None,
    provider_config_key: str | None,
) -> list[dict]:
```

**Adding New Integration:**

1. Define type: `class IntegrationType(str, Enum): HUBSPOT = "hubspot"`
2. Map provider: `NANGO_TO_INTEGRATION_TYPE = {"hubspot": IntegrationType.HUBSPOT}`
3. Add category: `PROVIDER_CATEGORIES = {IntegrationType.HUBSPOT: ("crm", "hubspot")}`
4. Create fetcher: `providers/crm/hubspot_contacts.py`
5. Register: `_REGISTRY[("crm", "hubspot")] = {"contacts": hubspot_contacts_fetch}`
6. Create adapter: `adapters/crm_adapter.py`
7. Update dispatcher: Add `elif request.category == "crm"` case
8. Implement upsert: `sync_service.upsert_crm_contacts()`

**Debugging Integrations:**

```bash
# Check webhook reception
grep "NANGO WEBHOOK RECEIVED" logs/app.log

# Check task execution
grep "Task received: integrations.sync_integration_data" logs/celery_integrations.log

# Check database records
SELECT COUNT(*) FROM organisation_documents WHERE source_id IN (
    SELECT source_id FROM organisation_sources WHERE source_type = 'INTEGRATION'
);
```

---

## 12. Troubleshooting Guide

### Common Issues

| Issue | Cause | Solution |
|-------|-------|----------|
| **API request hangs on send_task** | Synchronous `send_task()` blocking asyncio event loop | Use async client (`client.py`) with `send_task_async()` |
| **Intermittent send_task hangs** | Cold start - Redis connection not established | Use async client with `broker_connection_timeout=10` |
| **"Scheduling" log but no "Enqueued"** | `send_task()` blocking on SSL handshake to Redis | Use thread pool executor in async client |
| Task not received | Worker not connected to correct queue | Check `task_routes` and queue names match |
| Task stuck in PENDING | Worker crashed mid-execution | Enable `acks_late=True`, check worker logs |
| "Event loop is closed" | Event loop reuse issues | Use `run_async()` helper or create new loop per task |
| Memory leaks | Resources not cleaned up | Always cleanup in `finally` block, set `worker_max_tasks_per_child` |
| SSL connection failed | Wrong SSL config for environment | Check `REDIS_SSL_CERT_REQS`, localhost uses no SSL |
| Database pool exhausted | Too many connections | Set `MAX_TENANT_ENGINES=1` for workers |
| Task timeout | Long-running task | Increase `task_time_limit` in config |

### API Request Hanging on Task Submission

**Symptoms:**
- API endpoint logs "Scheduling task..." but never logs "Enqueued"
- Request times out or hangs indefinitely
- Works locally but fails in production (Kubernetes/Azure)
- Works sometimes, fails other times (cold start issue)

**Root Cause:**
Celery's `send_task()` is synchronous. When called from an async FastAPI endpoint, it blocks the asyncio event loop while:
1. Establishing TCP connection to Redis
2. Performing SSL/TLS handshake (production Redis uses SSL)
3. Authenticating with Redis password

**Solution:**
Use the async client pattern (see Step 7 in Section 5):
```python
# ❌ Don't do this in async endpoints
from src.workers.my_worker.celery_app import app
app.send_task(...)  # BLOCKS event loop

# ✅ Do this instead
from src.workers.my_worker.client import send_task_async
await send_task_async(...)  # Runs in thread pool
```

### Debugging Commands

```bash
# Check worker status
celery -A src.workers.my_worker.celery_app inspect active

# List registered tasks
celery -A src.workers.my_worker.celery_app inspect registered

# Check queue lengths
celery -A src.workers.my_worker.celery_app inspect reserved

# Purge queue (dangerous in production!)
celery -A src.workers.my_worker.celery_app purge -Q my_processing

# Task events in real-time
celery -A src.workers.my_worker.celery_app events
```

### Logging Best Practices

```python
from src.infra.utils.logger import get_logger

logger = get_logger("my_worker")

# Task start
logger.info(f"Starting task for org={org_id} item={item_id}")

# Milestones
logger.info(f"Loaded {len(items)} items for processing")

# Errors (with context)
logger.error(f"Failed to process item={item_id} error={str(e)}")

# Debug (conditional)
logger.debug(f"Intermediate result: {result}")
```

---

## 13. Checklist for New Workers

### Pre-Implementation

- [ ] Define the worker's purpose and scope
- [ ] Identify required queues (standard, priority, etc.)
- [ ] Determine time limits (default 5 min, up to 30 min for heavy tasks)
- [ ] List all tasks and their routing

### Implementation

- [ ] Create directory structure: `src/workers/my_worker/`
- [ ] Create `__init__.py` (empty)
- [ ] Create `config.py` with `MyWorkerConfig(BaseWorkerConfig)`
- [ ] Create `celery_app.py` with:
  - [ ] DB pool limits at top (`os.environ["MAX_TENANT_ENGINES"] = "1"`)
  - [ ] Celery app via `create_celery_app()`
  - [ ] Signal handlers for `worker_ready` and `worker_shutdown`
  - [ ] Redis pool initialization/cleanup
- [ ] Create `client.py` for API usage with:
  - [ ] Lazy-initialized singleton Celery client
  - [ ] Thread pool executor for non-blocking sends
  - [ ] `send_task()` sync function
  - [ ] `send_task_async()` async function
  - [ ] `broker_connection_timeout=10` to prevent hangs
- [ ] Create `tasks/__init__.py`
- [ ] Implement task files with:
  - [ ] Proper task decorators (`bind=True`, `acks_late=True`, `max_retries`)
  - [ ] Session acquisition via `acquire_org_session()`
  - [ ] Idempotency checks (skip already processed items)
  - [ ] Status updates (IN_PROGRESS → COMPLETED/FAILED)
  - [ ] Error handling with retry logic
  - [ ] Resource cleanup in `finally` blocks
- [ ] Create `README.md` with usage instructions

### Testing

- [ ] Test task locally with solo pool
- [ ] Verify queue routing works correctly
- [ ] Test retry behavior with simulated failures
- [ ] Verify resource cleanup (no connection leaks)
- [ ] Test with real database and Redis

### Deployment

- [ ] Add worker to Docker Compose / Kubernetes manifests
- [ ] Configure appropriate replica counts
- [ ] Set up monitoring/alerting for task failures
- [ ] Document in central worker architecture docs

---

## Quick Reference: Common Imports

```python
# Celery
from src.workers.my_worker.celery_app import app
from celery.signals import worker_ready, worker_shutdown, task_failure

# Database
from src.api.dependencies.organisation_dependencies import acquire_org_session
from src.infra.database.factory import get_async_provider, get_sync_provider

# Storage
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage

# Redis
from src.infra.messaging.redis_client import get_redis_client, initialize_redis_pool, close_redis_pool

# Logging
from src.infra.utils.logger import get_logger

# Async utilities
from src.infra.utils.async_runner import run_async

# Common models
from src.domain.models.organisation_tables.job_models import JobStatus
from uuid import UUID
import asyncio
import contextlib
from datetime import datetime, UTC
```

---

## Core Principles

When implementing Celery workers, always adhere to these fundamental principles:

| Principle | Description |
|-----------|-------------|
| **Idempotency** | Check state before processing. Tasks may be retried or re-delivered. |
| **Resilience** | Retry transient errors with backoff. Fail fast on validation errors. |
| **Observability** | Log milestones at key points. Publish status updates for UI feedback. |
| **Resource Cleanup** | Always close sessions, storage connections, and event loops in `finally` blocks. |
| **Isolation** | Use minimal DB pools (`MAX_TENANT_ENGINES=1`). Create dedicated event loops per task. |

---

*This document should be kept up-to-date as the worker architecture evolves. For questions or clarifications, refer to existing worker implementations as reference.*
