# Integration Data Sync - Worker Architecture (Webhook-Focused)

## Overview

This document provides the **overall architecture** for the webhook-based integration data synchronization system, focusing on **CRM (HubSpot)** and **Calendar (Google Calendar)** integrations using Nango as the OAuth proxy and data provider.

> 📘 **For detailed end-to-end sync flow**, see: [Data Sync Flow](./data-sync-flow.md)

### Key Principles

1. **Webhook-Driven**: Data sync is triggered by Nango webhooks when provider data changes
2. **Worker-Based**: All heavy sync operations are handled asynchronously by Celery workers
3. **Multi-Category Support**: Extensible architecture supporting CRM and Calendar categories (Email service placeholder for future)
4. **Cached Reads**: API serves cached data from PostgreSQL for fast response times

### Related Documentation

- **[Data Sync Flow](./data-sync-flow.md)** - Complete step-by-step sync process with timings and examples
- **[Integrations Worker README](../../../src/workers/integrations_worker/README.md)** - Worker setup and configuration
- **[OAuth Data Flow](../../API%20Documentation/oauth-data-flow.md)** - OAuth connection establishment

---

## System Architecture Diagram

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                            EXTERNAL LAYER                                   │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌──────────────┐         ┌──────────────┐         ┌──────────────┐       │
│  │   HubSpot    │         │    Google    │         │   (Email)    │       │
│  │     CRM      │         │   Calendar   │         │   Reserved   │       │
│  │  (Provider)  │         │  (Provider)  │         │              │       │
│  └──────┬───────┘         └──────┬───────┘         └──────────────┘       │
│         │                        │                                         │
│         └────────────────────────┴─────────────────────┐                   │
│                                                         │                   │
│                                  ┌──────────────────────▼─────────────┐    │
│                                  │        NANGO PLATFORM              │    │
│                                  │  (OAuth Proxy + Sync Engine)       │    │
│                                  │  - Manages OAuth tokens             │    │
│                                  │  - Provides unified data APIs       │    │
│                                  │  - Sends sync webhooks              │    │
│                                  └──────────────┬─────────────────────┘    │
│                                                 │                          │
└─────────────────────────────────────────────────┼──────────────────────────┘
                                                  │
                                                  │ HTTPS Webhooks
                                                  │ (Sync events)
                                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         COPILOT BACKEND SYSTEM                              │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                            API LAYER (FastAPI)                              │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌────────────────────────────────────────────────────────────────────┐    │
│  │  /api/v1/users/integrations/oauth/webhooks/nango                   │    │
│  │  • Receives Nango webhooks (type: 'auth' or 'sync')                │    │
│  │  • Verifies X-Nango-Signature                                       │    │
│  │  • Routes to appropriate handler                                    │    │
│  └─────────────────────────────┬──────────────────────────────────────┘    │
│                                │                                            │
│                                ├──────────────┬──────────────┐             │
│                                │              │              │             │
│                                ▼              ▼              ▼             │
│  ┌──────────────────┐  ┌──────────────┐  ┌──────────────────────────┐    │
│  │   Auth Webhook   │  │Sync Webhook  │  │  Data Read API           │    │
│  │   Handler        │  │  Handler     │  │  GET /integrations/{id}/ │    │
│  │  (Existing)      │  │  (NEW)       │  │      data?type=contacts  │    │
│  │                  │  │              │  │  GET /sync-status        │    │
│  │  • Creates       │  │  • Extracts  │  │  • Serves cached data    │    │
│  │    integration   │  │    response  │  │  • Shows freshness       │    │
│  │  • Enqueues      │  │    Results   │  │  • No blocking calls     │    │
│  │    initial sync  │  │  • Enqueues  │  │                          │    │
│  │                  │  │    sync task │  │                          │    │
│  └────────┬─────────┘  └──────┬───────┘  └──────────────────────────┘    │
│           │                   │                      ▲                     │
└───────────┼───────────────────┼──────────────────────┼─────────────────────┘
            │                   │                      │
            │ Enqueue Task      │ Enqueue Task         │ Read Cached Data
            ▼                   ▼                      │
┌─────────────────────────────────────────────────────────────────────────────┐
│                        QUEUE LAYER (Redis + Celery)                         │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                  Queue: "integrations"                              │   │
│  │                                                                     │   │
│  │  Task Types:                                                        │   │
│  │  ✓ sync_integration_data_task (initial full sync)                  │   │
│  │  ✓ sync_integration_incremental_task (webhook-triggered)           │   │
│  │                                                                     │   │
│  │  Routing Key Pattern: integrations.{category}.{provider}           │   │
│  │  - integrations.crm.hubspot                                         │   │
│  │  - integrations.calendar.google_calendar                            │   │
│  │  - integrations.email.* (reserved)                                  │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
                                      │ Workers Poll Queue
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    WORKER LAYER (Celery Workers)                            │
│                  src/workers/integrations_worker/                           │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                 Sync Orchestrator (NEW)                             │   │
│  │         src/workers/integrations_worker/tasks/sync_tasks.py         │   │
│  │                                                                     │   │
│  │  @app.task(name='sync_integration_data')                            │   │
│  │  def sync_integration_data_task(                                    │   │
│  │      integration_id, organization_id, data_types, is_initial        │   │
│  │  ):                                                                 │   │
│  │      1. Resolve integration category & provider                     │   │
│  │      2. Delegate to category-specific handler                       │   │
│  │      3. Update sync job status                                      │   │
│  └────────────────────────────┬────────────────────────────────────────┘   │
│                               │                                            │
│                               ├──────────────┬──────────────┐             │
│                               ▼              ▼              ▼             │
│  ┌──────────────────┐  ┌──────────────┐  ┌──────────────────────────┐    │
│  │   CRM Handler    │  │   Calendar   │  │   Email Handler          │    │
│  │                  │  │   Handler    │  │   (Reserved)             │    │
│  │  tasks/providers/│  │              │  │                          │    │
│  │  crm/            │  │  tasks/      │  │   tasks/providers/email/ │    │
│  │                  │  │  providers/  │  │                          │    │
│  │  • hubspot.py    │  │  calendar/   │  │   • gmail.py (future)    │    │
│  │                  │  │              │  │   • outlook.py (future)  │    │
│  │  sync_contacts() │  │  • google_   │  │                          │    │
│  │  sync_deals()    │  │    calendar. │  │                          │    │
│  │  sync_companies()│  │    py        │  │                          │    │
│  │                  │  │              │  │                          │    │
│  │                  │  │  sync_events │  │                          │    │
│  └────────┬─────────┘  └──────┬───────┘  └──────────────────────────┘    │
│           │                   │                                            │
│           │ Each handler:     │                                            │
│           │ 1. Calls NangoClient to fetch data                             │
│           │ 2. Handles pagination (cursor/pageToken)                       │
│           │ 3. Normalizes provider-specific data                           │
│           │ 4. Calls SyncService to upsert to DB                           │
│           │ 5. Handles errors & retries                                    │
│           │                                                                │
│           └───────────────────┴────────────────────┐                       │
│                                                     │                      │
│                                                     ▼                      │
│  ┌─────────────────────────────────────────────────────────────────────┐  │
│  │              Service Layer (Business Logic)                         │  │
│  │         src/service/integrations/sync_service.py (NEW)              │  │
│  │                                                                     │  │
│  │  class SyncService:                                                 │  │
│  │    • upsert_synced_data(integration_id, data_type, records)         │  │
│  │    • create_sync_job(integration_id, data_type, trigger)            │  │
│  │    • update_sync_job_status(job_id, status, records_count)          │  │
│  │    • get_last_sync_timestamp(integration_id, data_type)             │  │
│  │                                                                     │  │
│  │  • Database operations abstraction                                  │  │
│  │  • Idempotency via natural keys                                     │  │
│  │  • Batch upserts for efficiency                                     │  │
│  └─────────────────────────────────┬───────────────────────────────────┘  │
│                                    │                                       │
└────────────────────────────────────┼───────────────────────────────────────┘
                                     │
                                     │ Write/Update
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    STORAGE LAYER (PostgreSQL)                               │
│                    Organization-scoped database                             │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  Table: integration_synced_data (NEW)                               │   │
│  │                                                                     │   │
│  │  Columns:                                                           │   │
│  │  • id (UUID, PK)                                                    │   │
│  │  • organization_id (UUID, indexed)                                  │   │
│  │  • integration_id (UUID, FK → knowledge_integrations, indexed)      │   │
│  │  • category (TEXT: 'crm', 'calendar', 'email')                      │   │
│  │  • data_type (TEXT: 'contacts', 'deals', 'events', etc.)            │   │
│  │  • external_id (TEXT, provider's unique ID)                         │   │
│  │  • data_payload (JSONB, GIN indexed)                                │   │
│  │  • last_synced_at (TIMESTAMPTZ)                                     │   │
│  │  • created_at, updated_at (TIMESTAMPTZ)                             │   │
│  │                                                                     │   │
│  │  Unique Constraint: (integration_id, data_type, external_id)        │   │
│  │  Indexes:                                                           │   │
│  │  • (organization_id, category, data_type)                           │   │
│  │  • (integration_id, data_type)                                      │   │
│  │  • GIN(data_payload) for JSONB queries                              │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  Table: sync_jobs (NEW)                                             │   │
│  │                                                                     │   │
│  │  Columns:                                                           │   │
│  │  • job_id (UUID, PK)                                                │   │
│  │  • organization_id (UUID, indexed)                                  │   │
│  │  • integration_id (UUID, FK, indexed)                               │   │
│  │  • category (TEXT: 'crm', 'calendar', 'email')                      │   │
│  │  • data_type (TEXT: 'contacts', 'deals', 'events')                  │   │
│  │  • trigger (ENUM: 'auth', 'webhook', 'scheduled')                   │   │
│  │  • status (ENUM: 'pending', 'running', 'completed', 'failed')       │   │
│  │  • records_synced (INT)                                             │   │
│  │  • error_message (TEXT, nullable)                                   │   │
│  │  • started_at, completed_at (TIMESTAMPTZ)                           │   │
│  │  • created_at, updated_at (TIMESTAMPTZ)                             │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  Table: knowledge_integrations (EXISTING - Enhanced)                │   │
│  │                                                                     │   │
│  │  New/Modified Columns:                                              │   │
│  │  • category (TEXT: 'crm', 'calendar', 'email') [NEW]                │   │
│  │  • last_synced_at (TIMESTAMPTZ) [NEW]                               │   │
│  │  • last_sync_status (TEXT) [NEW]                                    │   │
│  │  • auto_sync_enabled (BOOLEAN, default=true) [NEW]                  │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Component Details

### 1. API Layer (`src/api/routers/integrations/oauth_routes.py`)

#### 1.1 Webhook Handler (Enhanced)

**Endpoint:** `POST /api/v1/users/integrations/oauth/webhooks/nango`

**Responsibilities:**

- Verify Nango webhook signature (`X-Nango-Signature` header)
- Handle two webhook types:
  - **`auth` webhook**: When user completes OAuth (existing)
  - **`sync` webhook**: When provider data changes (NEW)

**Sync Webhook Flow:**

```python
@router.post("/webhooks/nango")
async def handle_nango_webhook(request: Request, x_nango_signature: str = Header(...)):
    payload = await request.json()
    
    # Verify signature
    if not NangoClient.verify_webhook_signature(x_nango_signature, payload):
        return Response.unauthorized_error_response()
    
    webhook_type = payload.get("type")
    
    if webhook_type == "auth":
        # Existing: Create integration + enqueue initial sync
        return await handle_auth_webhook(payload)
    
    elif webhook_type == "sync":
        # NEW: Handle data change notification
        return await handle_sync_webhook(payload)
```

**Sync Webhook Payload Example:**

```json
{
  "type": "sync",
  "connectionId": "123e4567-e89b-12d3-a456-426614174000",
  "providerConfigKey": "hubspot",
  "syncName": "contacts",
  "model": "Contact",
  "responseResults": {
    "added": 5,
    "updated": 12,
    "deleted": 2
  },
  "syncType": "INCREMENTAL",
  "queryTimeStamp": "2025-10-18T10:30:00Z"
}
```

**Handler Logic:**

```python
async def handle_sync_webhook(payload: Dict[str, Any]) -> Response:
    """
    Handle Nango sync webhook - triggered when provider data changes.
    
    Steps:
    1. Extract connection_id and sync details
    2. Lookup integration_id from connection_id
    3. Determine if sync is needed (check responseResults)
    4. Enqueue incremental sync task
    """
    connection_id = payload.get("connectionId")
    sync_name = payload.get("syncName")  # e.g., "contacts", "deals", "events"
    response_results = payload.get("responseResults", {})
    
    # Check if there are actual changes
    total_changes = (
        response_results.get("added", 0) +
        response_results.get("updated", 0) +
        response_results.get("deleted", 0)
    )
    
    if total_changes == 0:
        logger.info(f"Sync webhook for {connection_id}: No changes detected")
        return Response.success_response(message="No changes to sync")
    
    # Get integration from connection_id
    async for org_session in acquire_org_async_session_from_connection(connection_id):
        integration = await get_integration_by_connection_id(org_session, connection_id)
        
        if not integration:
            logger.error(f"Integration not found for connection_id: {connection_id}")
            return Response.not_found_response()
        
        # Enqueue incremental sync task
        from src.workers.integrations_worker.tasks.sync_tasks import sync_integration_incremental_task
        
        sync_integration_incremental_task.delay(
            integration_id=str(integration.integration_id),
            organization_id=str(integration.organization_id),
            data_types=[sync_name],  # e.g., ["contacts"]
            trigger="webhook"
        )
        
        logger.info(
            f"Enqueued incremental sync for integration {integration.integration_id}, "
            f"data_type={sync_name}, changes={total_changes}"
        )
        
        return Response.success_response(
            message="Sync webhook processed",
            data={"integration_id": str(integration.integration_id), "data_type": sync_name}
        )
```

#### 1.2 Auth Webhook (Enhanced - Existing)

**When user completes OAuth:**

```python
async def handle_auth_webhook(payload: Dict[str, Any]) -> Response:
    """
    Existing handler - enhanced to enqueue initial sync.
    
    Steps:
    1. Create integration record (existing)
    2. Enqueue initial full sync (NEW)
    """
    # ... existing integration creation logic ...
    
    # NEW: Enqueue initial sync for all relevant data types
    from src.workers.integrations_worker.tasks.sync_tasks import sync_integration_data_task
    
    # Determine data types based on integration category
    data_types = get_data_types_for_category(integration.category)
    
    sync_integration_data_task.delay(
        integration_id=str(integration.integration_id),
        organization_id=str(integration.organization_id),
        data_types=data_types,
        is_initial=True,
        trigger="auth"
    )
    
    return Response.success_response(...)
```

---

### 2. Worker Layer (`src/workers/integrations_worker/`)

#### 2.1 Directory Structure

```text
src/workers/integrations_worker/
├── celery_app.py                      # Celery app initialization
├── config.py                          # Worker configuration
├── tasks/
│   ├── __init__.py
│   ├── sync_tasks.py                  # NEW: Main orchestrator tasks
│   ├── token_refresh.py               # Existing: Token refresh
│   └── providers/                     # NEW: Provider-specific handlers
│       ├── __init__.py
│       ├── crm/
│       │   ├── __init__.py
│       │   ├── base.py                # Base CRM sync logic
│       │   └── hubspot.py             # HubSpot-specific implementation
│       ├── calendar/
│       │   ├── __init__.py
│       │   ├── base.py                # Base calendar sync logic
│       │   └── google_calendar.py     # Google Calendar implementation
│       └── email/                     # Reserved for future
│           ├── __init__.py
│           └── base.py
```

#### 2.2 Main Sync Task (`sync_tasks.py`)

```python
"""
Main orchestrator tasks for integration data synchronization.
"""
from celery import Task
from typing import List
from uuid import UUID
from src.workers.integrations_worker.celery_app import app
from src.service.integrations.sync_service import SyncService
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


@app.task(
    name="sync_integration_data",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    reject_on_worker_lost=True
)
def sync_integration_data_task(
    self: Task,
    integration_id: str,
    organization_id: str,
    data_types: List[str],
    is_initial: bool = False,
    trigger: str = "webhook"
) -> dict:
    """
    Main task to sync integration data.
    
    Args:
        integration_id: UUID of the integration
        organization_id: UUID of the organization
        data_types: List of data types to sync (e.g., ['contacts', 'deals'])
        is_initial: Whether this is the initial sync after auth
        trigger: What triggered the sync ('auth', 'webhook', 'scheduled')
    
    Returns:
        dict: Sync results summary
    """
    logger.info(
        f"Starting sync task: integration={integration_id}, "
        f"org={organization_id}, types={data_types}, initial={is_initial}"
    )
    
    try:
        # Get integration details to determine category and provider
        integration = get_integration_details(integration_id, organization_id)
        
        if not integration:
            logger.error(f"Integration {integration_id} not found")
            return {"status": "failed", "error": "Integration not found"}
        
        category = integration.category
        provider = integration.integration_type
        
        # Route to category-specific handler
        if category == "crm":
            from src.workers.integrations_worker.tasks.providers.crm import handle_crm_sync
            result = handle_crm_sync(
                integration=integration,
                data_types=data_types,
                is_initial=is_initial,
                trigger=trigger
            )
        
        elif category == "calendar":
            from src.workers.integrations_worker.tasks.providers.calendar import handle_calendar_sync
            result = handle_calendar_sync(
                integration=integration,
                data_types=data_types,
                is_initial=is_initial,
                trigger=trigger
            )
        
        elif category == "email":
            # Reserved for future
            logger.warning(f"Email category not yet implemented for {provider}")
            return {"status": "skipped", "reason": "Email category not implemented"}
        
        else:
            logger.error(f"Unknown category: {category}")
            return {"status": "failed", "error": f"Unknown category: {category}"}
        
        logger.info(f"Sync completed: {result}")
        return result
    
    except Exception as e:
        logger.error(f"Sync task failed: {str(e)}", exc_info=True)
        # Retry with exponential backoff
        raise self.retry(exc=e)


@app.task(
    name="sync_integration_incremental",
    bind=True,
    max_retries=3,
    default_retry_delay=30
)
def sync_integration_incremental_task(
    self: Task,
    integration_id: str,
    organization_id: str,
    data_types: List[str],
    trigger: str = "webhook"
) -> dict:
    """
    Incremental sync task (webhook-triggered).
    
    Similar to full sync but optimized for incremental updates.
    """
    return sync_integration_data_task(
        integration_id=integration_id,
        organization_id=organization_id,
        data_types=data_types,
        is_initial=False,
        trigger=trigger
    )
```

#### 2.3 CRM Handler (`providers/crm/hubspot.py`)

```python
"""
HubSpot CRM data synchronization handler.
"""
from typing import List, Dict, Any
from src.service.integrations.nango_client import NangoClient
from src.service.integrations.sync_service import SyncService
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


def handle_crm_sync(integration, data_types: List[str], is_initial: bool, trigger: str) -> dict:
    """
    Handle CRM data synchronization for HubSpot.
    
    Data types supported:
    - contacts: Contact records
    - deals: Deal/opportunity records
    - companies: Company/account records
    """
    nango = NangoClient()
    sync_service = SyncService(
        organization_id=integration.organization_id,
        integration_id=integration.integration_id
    )
    
    results = {}
    
    for data_type in data_types:
        try:
            # Create sync job record
            job_id = sync_service.create_sync_job(
                integration_id=integration.integration_id,
                category="crm",
                data_type=data_type,
                trigger=trigger,
                status="running"
            )
            
            logger.info(f"Starting CRM sync: job={job_id}, type={data_type}")
            
            # Fetch data from Nango
            if data_type == "contacts":
                records = sync_hubspot_contacts(nango, integration, is_initial)
            elif data_type == "deals":
                records = sync_hubspot_deals(nango, integration, is_initial)
            elif data_type == "companies":
                records = sync_hubspot_companies(nango, integration, is_initial)
            else:
                logger.warning(f"Unknown CRM data type: {data_type}")
                continue
            
            # Upsert to database
            records_count = sync_service.upsert_synced_data(
                category="crm",
                data_type=data_type,
                records=records
            )
            
            # Update job status
            sync_service.update_sync_job_status(
                job_id=job_id,
                status="completed",
                records_synced=records_count
            )
            
            # Update integration metadata
            sync_service.update_integration_sync_metadata(
                integration_id=integration.integration_id,
                last_sync_status="success"
            )
            
            results[data_type] = {
                "status": "success",
                "records_synced": records_count,
                "job_id": str(job_id)
            }
            
        except Exception as e:
            logger.error(f"Failed to sync {data_type}: {str(e)}", exc_info=True)
            sync_service.update_sync_job_status(
                job_id=job_id,
                status="failed",
                error_message=str(e)
            )
            results[data_type] = {"status": "failed", "error": str(e)}
    
    return results


def sync_hubspot_contacts(
    nango: NangoClient,
    integration,
    is_initial: bool
) -> List[Dict[str, Any]]:
    """
    Sync HubSpot contacts via Nango proxy.
    
    Returns:
        List of normalized contact records
    """
    logger.info(f"Fetching HubSpot contacts for integration {integration.integration_id}")
    
    all_records = []
    cursor = None
    page = 0
    
    # Determine sync mode
    if is_initial:
        # Full sync - fetch all contacts
        params = {"limit": 100}
    else:
        # Incremental sync - fetch only recently modified
        last_sync = get_last_sync_timestamp(integration.integration_id, "contacts")
        params = {
            "limit": 100,
            "modifiedAfter": last_sync.isoformat() if last_sync else None
        }
    
    # Paginate through results
    while True:
        try:
            response = nango.proxy_request(
                method="GET",
                provider=integration.integration_type.value,
                connection_id=integration.connection_id,
                endpoint="/crm/v3/objects/contacts",
                params={**params, "after": cursor} if cursor else params
            )
            
            results = response.get("results", [])
            all_records.extend(normalize_hubspot_contacts(results))
            
            # Check for next page
            paging = response.get("paging", {})
            next_cursor = paging.get("next", {}).get("after")
            
            if not next_cursor:
                break
            
            cursor = next_cursor
            page += 1
            
            logger.info(f"Fetched page {page}, total records: {len(all_records)}")
            
        except Exception as e:
            logger.error(f"Error fetching contacts page {page}: {str(e)}")
            raise
    
    logger.info(f"Total contacts fetched: {len(all_records)}")
    return all_records


def normalize_hubspot_contacts(raw_records: List[Dict]) -> List[Dict]:
    """
    Normalize HubSpot contact records to our schema.
    
    Returns:
        List of normalized records with structure:
        {
            "external_id": "12345",
            "data_payload": { ... full contact data ... }
        }
    """
    normalized = []
    
    for record in raw_records:
        normalized.append({
            "external_id": record.get("id"),
            "data_payload": {
                "id": record.get("id"),
                "properties": record.get("properties", {}),
                "createdAt": record.get("createdAt"),
                "updatedAt": record.get("updatedAt"),
                "archived": record.get("archived", False)
            }
        })
    
    return normalized


def sync_hubspot_deals(nango: NangoClient, integration, is_initial: bool) -> List[Dict]:
    """Similar to sync_hubspot_contacts but for deals."""
    # Implementation similar to contacts
    pass


def sync_hubspot_companies(nango: NangoClient, integration, is_initial: bool) -> List[Dict]:
    """Similar to sync_hubspot_contacts but for companies."""
    # Implementation similar to contacts
    pass
```

#### 2.4 Calendar Handler (`providers/calendar/google_calendar.py`)

```python
"""
Google Calendar synchronization handler.
"""
from typing import List, Dict, Any
from datetime import datetime, timedelta
from src.service.integrations.nango_client import NangoClient
from src.service.integrations.sync_service import SyncService
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


def handle_calendar_sync(integration, data_types: List[str], is_initial: bool, trigger: str) -> dict:
    """
    Handle calendar data synchronization for Google Calendar.
    
    Data types supported:
    - events: Calendar events
    """
    nango = NangoClient()
    sync_service = SyncService(
        organization_id=integration.organization_id,
        integration_id=integration.integration_id
    )
    
    results = {}
    
    for data_type in data_types:
        if data_type != "events":
            logger.warning(f"Unknown calendar data type: {data_type}")
            continue
        
        try:
            job_id = sync_service.create_sync_job(
                integration_id=integration.integration_id,
                category="calendar",
                data_type=data_type,
                trigger=trigger,
                status="running"
            )
            
            logger.info(f"Starting calendar sync: job={job_id}")
            
            # Fetch events
            records = sync_google_calendar_events(nango, integration, is_initial)
            
            # Upsert to database
            records_count = sync_service.upsert_synced_data(
                category="calendar",
                data_type=data_type,
                records=records
            )
            
            sync_service.update_sync_job_status(
                job_id=job_id,
                status="completed",
                records_synced=records_count
            )
            
            results[data_type] = {
                "status": "success",
                "records_synced": records_count
            }
            
        except Exception as e:
            logger.error(f"Failed to sync calendar events: {str(e)}", exc_info=True)
            results[data_type] = {"status": "failed", "error": str(e)}
    
    return results


def sync_google_calendar_events(
    nango: NangoClient,
    integration,
    is_initial: bool
) -> List[Dict[str, Any]]:
    """
    Sync Google Calendar events via Nango proxy.
    """
    logger.info(f"Fetching Google Calendar events for integration {integration.integration_id}")
    
    all_records = []
    page_token = None
    
    # Set time range (e.g., last 90 days to next 365 days)
    time_min = (datetime.utcnow() - timedelta(days=90)).isoformat() + 'Z'
    time_max = (datetime.utcnow() + timedelta(days=365)).isoformat() + 'Z'
    
    params = {
        "timeMin": time_min,
        "timeMax": time_max,
        "maxResults": 250,
        "singleEvents": True,
        "orderBy": "startTime"
    }
    
    while True:
        try:
            response = nango.proxy_request(
                method="GET",
                provider=integration.integration_type.value,
                connection_id=integration.connection_id,
                endpoint="/calendar/v3/calendars/primary/events",
                params={**params, "pageToken": page_token} if page_token else params
            )
            
            events = response.get("items", [])
            all_records.extend(normalize_google_calendar_events(events))
            
            # Check for next page
            next_page_token = response.get("nextPageToken")
            if not next_page_token:
                break
            
            page_token = next_page_token
            
            logger.info(f"Fetched page, total records: {len(all_records)}")
            
        except Exception as e:
            logger.error(f"Error fetching calendar events: {str(e)}")
            raise
    
    logger.info(f"Total events fetched: {len(all_records)}")
    return all_records


def normalize_google_calendar_events(raw_events: List[Dict]) -> List[Dict]:
    """
    Normalize Google Calendar events to our schema.
    """
    normalized = []
    
    for event in raw_events:
        normalized.append({
            "external_id": event.get("id"),
            "data_payload": {
                "id": event.get("id"),
                "summary": event.get("summary"),
                "description": event.get("description"),
                "start": event.get("start"),
                "end": event.get("end"),
                "attendees": event.get("attendees", []),
                "organizer": event.get("organizer"),
                "status": event.get("status"),
                "created": event.get("created"),
                "updated": event.get("updated")
            }
        })
    
    return normalized
```

---

### 3. Service Layer (`src/service/integrations/`)

#### 3.1 Sync Service (`sync_service.py` - NEW)

```python
"""
Service layer for integration data synchronization.

Handles:
- Database operations for synced data
- Sync job management
- Idempotent upserts
- Batch processing
"""
from typing import List, Dict, Any, Optional
from uuid import UUID
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert
from src.domain.models.organisation_tables.integration_sync_models import (
    IntegrationSyncedData,
    SyncJob
)
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class SyncService:
    """Service for managing integration data synchronization."""
    
    def __init__(self, organization_id: UUID, integration_id: UUID):
        self.organization_id = organization_id
        self.integration_id = integration_id
    
    async def create_sync_job(
        self,
        integration_id: UUID,
        category: str,
        data_type: str,
        trigger: str,
        status: str = "pending"
    ) -> UUID:
        """
        Create a new sync job record.
        
        Returns:
            UUID: The created job_id
        """
        async with get_org_session(self.organization_id) as session:
            job = SyncJob(
                organization_id=self.organization_id,
                integration_id=integration_id,
                category=category,
                data_type=data_type,
                trigger=trigger,
                status=status,
                started_at=datetime.utcnow() if status == "running" else None
            )
            session.add(job)
            await session.commit()
            await session.refresh(job)
            
            logger.info(f"Created sync job {job.job_id}")
            return job.job_id
    
    async def update_sync_job_status(
        self,
        job_id: UUID,
        status: str,
        records_synced: Optional[int] = None,
        error_message: Optional[str] = None
    ):
        """Update sync job status and completion details."""
        async with get_org_session(self.organization_id) as session:
            job = await session.get(SyncJob, job_id)
            if not job:
                logger.error(f"Sync job {job_id} not found")
                return
            
            job.status = status
            if records_synced is not None:
                job.records_synced = records_synced
            if error_message:
                job.error_message = error_message
            if status in ["completed", "failed"]:
                job.completed_at = datetime.utcnow()
            
            await session.commit()
            logger.info(f"Updated sync job {job_id}: status={status}")
    
    async def upsert_synced_data(
        self,
        category: str,
        data_type: str,
        records: List[Dict[str, Any]]
    ) -> int:
        """
        Upsert synced data records (idempotent).
        
        Args:
            category: 'crm', 'calendar', 'email'
            data_type: 'contacts', 'deals', 'events', etc.
            records: List of dicts with 'external_id' and 'data_payload'
        
        Returns:
            int: Number of records upserted
        """
        if not records:
            return 0
        
        async with get_org_session(self.organization_id) as session:
            # Batch upsert using PostgreSQL INSERT ... ON CONFLICT
            stmt = insert(IntegrationSyncedData).values([
                {
                    "organization_id": self.organization_id,
                    "integration_id": self.integration_id,
                    "category": category,
                    "data_type": data_type,
                    "external_id": record["external_id"],
                    "data_payload": record["data_payload"],
                    "last_synced_at": datetime.utcnow()
                }
                for record in records
            ])
            
            # On conflict (integration_id, data_type, external_id), update
            stmt = stmt.on_conflict_do_update(
                index_elements=["integration_id", "data_type", "external_id"],
                set_={
                    "data_payload": stmt.excluded.data_payload,
                    "last_synced_at": stmt.excluded.last_synced_at,
                    "updated_at": datetime.utcnow()
                }
            )
            
            await session.execute(stmt)
            await session.commit()
            
            logger.info(
                f"Upserted {len(records)} {category}.{data_type} records "
                f"for integration {self.integration_id}"
            )
            
            return len(records)
    
    async def update_integration_sync_metadata(
        self,
        integration_id: UUID,
        last_sync_status: str
    ):
        """Update integration's last sync timestamp and status."""
        async with get_org_session(self.organization_id) as session:
            from src.domain.models.organisation_tables.knowledge_models import KnowledgeIntegration
            
            integration = await session.get(KnowledgeIntegration, integration_id)
            if integration:
                integration.last_synced_at = datetime.utcnow()
                integration.last_sync_status = last_sync_status
                await session.commit()
```

---

### 4. Data Models (`src/domain/models/organisation_tables/`)

#### 4.1 Integration Sync Models (`integration_sync_models.py` - NEW)

```python
"""
Database models for integration data synchronization.
"""
from sqlalchemy import (
    Column, String, Integer, Text, DateTime, Boolean, ForeignKey, Index, Enum
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from datetime import datetime
import uuid
from src.domain.models.organisation_tables.base import OrganisationBase


class IntegrationSyncedData(OrganisationBase):
    """
    Cached data from external integrations.
    
    Generic JSONB storage for flexibility across providers.
    """
    __tablename__ = "integration_synced_data"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    integration_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_integrations.integration_id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    category = Column(String(50), nullable=False)  # 'crm', 'calendar', 'email'
    data_type = Column(String(100), nullable=False)  # 'contacts', 'deals', 'events'
    external_id = Column(String(255), nullable=False)  # Provider's unique ID
    data_payload = Column(JSONB, nullable=False)  # Full record data
    last_synced_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    __table_args__ = (
        # Unique constraint for idempotent upserts
        Index(
            'uix_integration_data_type_external',
            'integration_id', 'data_type', 'external_id',
            unique=True
        ),
        # Query optimization indexes
        Index('idx_org_category_type', 'organization_id', 'category', 'data_type'),
        Index('idx_integration_type', 'integration_id', 'data_type'),
        # GIN index for JSONB queries
        Index('idx_data_payload_gin', 'data_payload', postgresql_using='gin'),
    )


class SyncJob(OrganisationBase):
    """
    Tracks synchronization job status and history.
    """
    __tablename__ = "sync_jobs"
    
    job_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    integration_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_integrations.integration_id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    category = Column(String(50), nullable=False)  # 'crm', 'calendar', 'email'
    data_type = Column(String(100), nullable=False)  # 'contacts', 'deals', 'events'
    trigger = Column(
        Enum('auth', 'webhook', 'scheduled', name='sync_trigger_enum'),
        nullable=False
    )
    status = Column(
        Enum('pending', 'running', 'completed', 'failed', name='sync_status_enum'),
        nullable=False,
        default='pending'
    )
    records_synced = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    __table_args__ = (
        Index('idx_sync_jobs_status', 'integration_id', 'status'),
        Index('idx_sync_jobs_created', 'organization_id', 'created_at'),
    )
```

#### 4.2 Enhanced Knowledge Integration Model

```python
# Add to existing KnowledgeIntegration model:

class KnowledgeIntegration(OrganisationBase):
    # ... existing fields ...
    
    # NEW FIELDS for sync support
    category = Column(String(50), nullable=True)  # 'crm', 'calendar', 'email'
    last_synced_at = Column(DateTime(timezone=True), nullable=True)
    last_sync_status = Column(String(50), nullable=True)  # 'success', 'failed', 'partial'
    auto_sync_enabled = Column(Boolean, default=True)
```

---

## Data Flow Examples

### Example 1: Initial Sync After OAuth

```text
1. User completes OAuth in frontend
   └─> Nango sends auth webhook

2. Backend receives webhook
   └─> Creates integration record in DB
   └─> Enqueues sync task: sync_integration_data_task(
         integration_id="abc-123",
         organization_id="org-456",
         data_types=["contacts", "deals"],
         is_initial=True,
         trigger="auth"
       )

3. Worker picks up task
   └─> Routes to CRM handler (HubSpot)
   └─> For each data type:
       a) Create sync_job (status='running')
       b) Fetch from Nango (paginated)
       c) Normalize data
       d) Batch upsert to integration_synced_data
       e) Update sync_job (status='completed', records_synced=150)
       f) Update integration (last_synced_at, last_sync_status='success')

4. Frontend polls GET /integrations/{id}/sync-status
   └─> Returns: { "last_synced_at": "2025-10-18T10:35:00Z", "status": "success" }
```

### Example 2: Webhook-Triggered Incremental Sync

```text
1. User updates a contact in HubSpot
   └─> HubSpot notifies Nango
   └─> Nango performs sync
   └─> Nango sends sync webhook to backend

2. Backend receives sync webhook
   Payload: {
     "type": "sync",
     "connectionId": "conn-123",
     "syncName": "contacts",
     "responseResults": { "added": 0, "updated": 1, "deleted": 0 }
   }
   
   └─> Looks up integration by connection_id
   └─> Enqueues incremental sync: sync_integration_incremental_task(
         integration_id="abc-123",
         organization_id="org-456",
         data_types=["contacts"],
         trigger="webhook"
       )

3. Worker picks up task
   └─> Routes to CRM handler
   └─> Fetches only recently modified contacts (modifiedAfter=last_synced_at)
   └─> Upserts 1 record to DB
   └─> Updates sync metadata

4. Frontend reads cached data
   └─> GET /integrations/{id}/data?type=contacts
   └─> Returns updated contact instantly (sub-100ms)
```

---

## Category-Specific Configuration

### CRM Category (HubSpot)

**Data Types:**

- `contacts`: Individual contact records
- `deals`: Sales opportunities
- `companies`: Account/organization records

**Nango Sync Names:**

- `hubspot-contacts`
- `hubspot-deals`
- `hubspot-companies`

**Initial Sync:** All data types
**Webhook Triggers:** Per data type (Nango manages subscriptions)

### Calendar Category (Google Calendar)

**Data Types:**

- `events`: Calendar events

**Nango Sync Names:**

- `google-calendar-events`

**Initial Sync:** Events in time range (90 days past to 365 days future)
**Webhook Triggers:** Event changes (Nango manages push notifications)

### Email Category (Reserved)

**Placeholder for:**

- Gmail
- Outlook

**Data Types (future):**

- `messages`: Email messages
- `threads`: Email threads

---

## Deployment & Scaling

### Worker Configuration

```bash
# Start integration worker
celery -A src.workers.integrations_worker.celery_app worker \
  --loglevel=info \
  --concurrency=4 \
  --max-tasks-per-child=1000 \
  -Q integrations \
  -n integrations_worker@%h
```

### Queue Routing (Future Enhancement)

```python
# config.py
CELERY_TASK_ROUTES = {
    'sync_integration_data': {
        'queue': 'integrations',
        'routing_key': 'integrations.{category}.{provider}'
    }
}

# Allows separate queues per category:
# - integrations.crm.hubspot
# - integrations.calendar.google_calendar
```

---

## Error Handling & Observability

### Retry Strategy

```python
@app.task(
    max_retries=3,
    default_retry_delay=60,  # 1 minute
    retry_backoff=True,      # Exponential backoff
    retry_jitter=True        # Add jitter to prevent thundering herd
)
```

### Logging

```python
logger.info(
    "Sync progress",
    extra={
        "integration_id": str(integration_id),
        "organization_id": str(organization_id),
        "category": category,
        "data_type": data_type,
        "records_count": len(records),
        "job_id": str(job_id)
    }
)
```

### Metrics (Future)

- Sync job completion rate
- Average sync duration per category/provider
- Records synced per hour
- Error rate per provider
- Queue depth

---

## Security Considerations

1. **Webhook Verification**: Always verify `X-Nango-Signature` header
2. **Token Management**: Nango handles OAuth token refresh
3. **Data Isolation**: All queries scoped by `organization_id`
4. **PII Protection**: JSONB data encrypted at rest (PostgreSQL level)
5. **Rate Limiting**: Respect provider API limits (handled by Nango proxy)

---

## Migration Path

### Phase 1: Foundation (Week 1)

✅ Database schema (tables, indexes, migrations)
✅ Service layer (SyncService)
✅ Worker task scaffolding

### Phase 2: CRM Implementation (Week 2)

✅ HubSpot handler (contacts, deals, companies)
✅ Webhook handler enhancement (sync webhook support)
✅ Initial sync on auth webhook

### Phase 3: Calendar Implementation (Week 3)

✅ Google Calendar handler (events)
✅ Time-range based sync
✅ Incremental updates

### Phase 4: API & Frontend (Week 4)

✅ Read API for cached data
✅ Sync status API
✅ Frontend integration (display synced data)

### Phase 5: Optimization (Ongoing)

- Performance tuning (batch sizes, concurrency)
- Advanced error handling (circuit breakers)
- Monitoring dashboards
- Email category implementation

---

## Summary

This architecture provides:

✅ **Webhook-driven sync**: No manual triggers needed
✅ **Category-based organization**: CRM, Calendar, Email
✅ **Worker-based processing**: Scalable, resilient
✅ **Cached reads**: Fast API responses
✅ **Extensible**: Easy to add new providers/categories
✅ **Observable**: Comprehensive logging and job tracking
✅ **Secure**: Webhook verification, data isolation

The system is designed to start with HubSpot (CRM) and Google Calendar, with clear patterns for adding more providers in each category.

---

## Quick Reference

### Key Documents

| Document | Purpose | Audience |
|----------|---------|----------|
| [Data Sync Flow](./data-sync-flow.md) | Complete end-to-end sync process with timings, examples, and troubleshooting | Developers, DevOps |
| [Integration Data Sync Architecture](./Integration-data-sync-architecture.md) | Overall system architecture and design patterns | Architects, Team Leads |
| [Integrations Worker README](../../../src/workers/integrations_worker/README.md) | Worker setup, configuration, and commands | Developers, DevOps |
| [Integrations Worker Fixes](../integrations-worker-fixes.md) | Recent production improvements and bug fixes | Developers |
| [OAuth Data Flow](../../API%20Documentation/oauth-data-flow.md) | OAuth connection establishment | API Developers |

### Common Tasks

| Task | See Document | Section |
|------|--------------|---------|
| Understand sync flow | [Data Sync Flow](./data-sync-flow.md) | Complete Flow Diagram |
| Set up worker | [Worker README](../../../src/workers/integrations_worker/README.md) | Usage |
| Add new provider | This document | Provider Registry |
| Debug sync issues | [Data Sync Flow](./data-sync-flow.md) | Troubleshooting Guide |
| Monitor performance | [Data Sync Flow](./data-sync-flow.md) | Monitoring & Observability |
| Test sync | [Data Sync Flow](./data-sync-flow.md) | Testing the Flow |

### Current Status (October 2025)

- ✅ **Production Ready**: Comprehensive error handling, logging, and monitoring
- ✅ **Google Calendar**: Fully implemented with incremental sync
- ⏳ **HubSpot CRM**: Architecture ready, implementation pending
- ⏳ **Email**: Reserved for future implementation
- ✅ **Nango Proxy**: Fixed and working correctly
- ✅ **Worker Infrastructure**: Celery workers with proper task routing
