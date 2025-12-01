# Integrations Worker

Celery worker dedicated to managing third-party integrations, OAuth token lifecycle, and data synchronization from external providers (Google Calendar, HubSpot CRM, etc.) via Nango proxy.

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Tasks](#tasks)
- [Supported Integrations](#supported-integrations)
- [Data Flow](#data-flow)
- [Provider Registry](#provider-registry)
- [Sync Services](#sync-services)
- [Usage](#usage)
- [Configuration](#configuration)
- [Error Handling](#error-handling)
- [Development](#development)
- [Adding New Integrations](#adding-new-integrations)
- [Troubleshooting](#troubleshooting)

## Overview

The Integrations Worker is a specialized Celery worker that handles:

- **OAuth Token Management**: Automatic refresh of expiring OAuth tokens
- **Webhook Processing**: Receives and processes sync notifications from Nango
- **Data Synchronization**: Fetches and persists data from external providers
- **Integration Lifecycle**: Manages integration metadata and sync status

### Key Features

✅ **Provider-Agnostic Design**: Extensible architecture supporting multiple providers  
✅ **Incremental Syncing**: Efficient delta syncs based on last sync timestamps  
✅ **Automatic Token Refresh**: Scheduled checks prevent token expiration  
✅ **Retry Mechanisms**: Built-in retry logic with exponential backoff  
✅ **Deduplication**: Sync locks prevent concurrent syncs for same integration  
✅ **Comprehensive Logging**: Detailed logs for debugging and monitoring  
✅ **Webhook-Driven**: Nango webhooks trigger automatic syncs after OAuth authorization  

## Architecture

```text
┌─────────────────────────────────────────────────────────────────┐
│                        External Providers                        │
│  (Google Calendar, HubSpot CRM, Salesforce, etc.)              │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             │ OAuth Flow
                             ▼
                   ┌──────────────────┐
                   │   Nango Platform  │
                   │  (OAuth Proxy &   │
                   │   Data Fetcher)   │
                   └────────┬──────────┘
                            │
                            │ Webhooks & API Calls
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                         FastAPI Backend                          │
│  ┌─────────────────────┐           ┌──────────────────────┐    │
│  │ OAuth Routes        │           │ Webhook Handler       │    │
│  │ /oauth/authorize    │           │ /oauth/webhooks/nango │    │
│  │ /oauth/disconnect   │           │                       │    │
│  └──────────┬──────────┘           └──────────┬───────────┘    │
│             │                                  │                 │
│             │ Enqueue Task                     │ Enqueue Task    │
│             ▼                                  ▼                 │
│  ┌────────────────────────────────────────────────────────┐    │
│  │              Integrations Worker (Celery)              │    │
│  │                                                          │    │
│  │  Tasks:                                                  │    │
│  │  • sync_integration_data     (Queue: integrations)      │    │
│  │  • refresh_expiring_tokens   (Queue: integrations)      │    │
│  │  • refresh_single_integration (Queue: integrations_priority)│
│  │                                                          │    │
│  │  Components:                                             │    │
│  │  ├─ Dispatcher (services/dispatcher.py)                 │    │
│  │  ├─ Provider Registry (providers/registry.py)           │    │
│  │  ├─ Fetchers (providers/crm/hubspot.py, etc.)          │    │
│  │  ├─ Adapters (adapters/crm_adapter.py, etc.)           │    │
│  │  └─ Sync Services (CRMSyncService, CalendarSyncService)│    │
│  └──────────────────────────┬───────────────────────────────┘    │
│                             │                                     │
│                             │ Persist Data                        │
│                             ▼                                     │
│  ┌────────────────────────────────────────────────────────┐    │
│  │        Organisation Database (PostgreSQL)              │    │
│  │  • crm_companies, crm_contacts, crm_deals              │    │
│  │  • crm_users, calendar_events                          │    │
│  │  • knowledge_integrations, integration_sync_history    │    │
│  └────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

## Supported Integrations

### CRM Providers

#### HubSpot

- **Category**: `crm`
- **Provider**: `hubspot`
- **Data Types**:
  - `pipelines` - Deal pipelines and stages
  - `deals` - Sales opportunities with participants and stage history
  - `contacts` - Contact profiles with lifecycle stages
  - `users` - CRM owners/users
  - `companies` - Company/account records ✨ **NEW**

**Company Sync Features**:

- Full and incremental sync support
- 30+ properties including business metrics, contact info, and social links
- Owner mapping and relationship tracking
- Revenue and employee count tracking
- Automatic pagination handling
- Association support (contacts, deals)

### Calendar Providers

#### Google Calendar

- **Category**: `calendar`
- **Provider**: `google`
- **Data Types**:
  - `events` - Calendar events with attendees and metadata

## Data Flow

### Complete Sync Flow (Webhook-Driven)

```text
1. User Authorizes Integration
   └─> Nango OAuth Flow completes
       └─> Nango sends webhook to /oauth/webhooks/nango

2. Webhook Handler (FastAPI)
   ├─> Validates webhook signature (X-Nango-Signature)
   ├─> Extracts sync metadata (connectionId, syncName, syncType)
   ├─> Resolves integration details from database
   └─> Enqueues Celery task: sync_integration_data_task

3. Celery Worker (Integrations Worker)
   ├─> Task: integrations.sync_integration_data
   ├─> Dispatcher validates integration exists
   ├─> Acquires sync lock (prevents concurrent syncs)
   ├─> Fetcher retrieves data from provider via Nango proxy
   │   ├─> Full sync: Fetch all records
   │   └─> Incremental sync: Fetch records modified since last_synced_at
   ├─> Adapter routes data to appropriate sync service
   ├─> Sync Service upserts records to database
   │   ├─> Maps provider fields to normalized schema
   │   ├─> Handles relationships (owners, associations)
   │   └─> Updates sync history and timestamps
   └─> Releases sync lock

4. Result
   ├─> Integration metadata updated (last_synced_at)
   ├─> Sync history record created
   └─> Data available for chat/AI agents
```

### Example: HubSpot Companies Sync

```text
User connects HubSpot → Nango webhook (type=sync, syncName=companies)
  ↓
NangoWebhookService resolves organisation & integration
  ↓
Enqueue: sync_integration_data(
  organisation_id=...,
  integration_id=...,
  category="crm",
  provider="hubspot",
  sync_name="companies"
)
  ↓
Dispatcher → get_fetcher("crm", "hubspot", "companies")
  ↓
HubSpot Fetcher:
  - Full sync: GET /crm/v3/objects/companies (with pagination)
  - Incremental: POST /crm/v3/objects/companies/search (filter by hs_lastmodifieddate)
  - Returns: List[dict] with company records
  ↓
CRM Adapter → sync_crm_data(data_type="companies")
  ↓
CRMSyncService.sync_companies():
  - Load existing companies by external_id
  - Map HubSpot properties to CRMCompany fields
  - Upsert to crm_companies table
  - Update sync history
  ↓
Result: {companies: 18, records_synced: 18}
```

## Provider Registry

The provider registry (`providers/registry.py`) maps category/provider/data_type combinations to fetcher functions:

```python
# Registry structure
_REGISTRY: dict[tuple[str, str], dict[str, FetchFn]] = {
    ("calendar", "google"): {
        "events": google_calendar_fetch
    },
    ("crm", "hubspot"): {
        "pipelines": hubspot_pipelines_fetch,
        "deals": hubspot_deals_fetch,
        "contacts": hubspot_contacts_fetch,
        "users": hubspot_users_fetch,
        "companies": hubspot_companies_fetch  # ✨ NEW
    }
}
```

### Fetcher Function Signature

All fetchers must implement this signature:

```python
async def fetch_<data_type>(
    *,
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    data_type: str | None = None,
    provider_config_key: str | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch records from external provider via Nango proxy.
    
    Args:
        nango: Nango client for API requests
        connection_id: Nango connection identifier
        is_initial: True for full sync, False for incremental
        last_synced_at: Timestamp of last successful sync
        data_type: Optional data type identifier
        provider_config_key: Optional provider config override
        
    Returns:
        List of raw provider records
    """
    pass
```

## Sync Services

### CRMSyncService

Located in `src/service/integrations/crm_sync_service.py`

Handles synchronization of CRM data (HubSpot deals, contacts, users, companies, pipelines).

**Key Methods**:

- `sync_pipelines()` - Sync deal pipelines and stages
- `sync_deals()` - Sync deals with participants and stage history
- `sync_contacts()` - Sync contact profiles
- `sync_users()` - Sync CRM owners/users
- `sync_companies()` - Sync company/account records ✨ **NEW**

**Features**:

- Upsert logic (create or update existing records)
- Relationship mapping (owners, associations)
- Type conversion (int for employees, float for revenue)
- JSONB storage for raw properties and computed metrics
- Sync history tracking with success/failure counts

### Example: Company Sync Logic

```python
# Load existing companies
existing_companies = await db.execute(
    select(CRMCompany)
    .where(CRMCompany.integration_id == integration_id)
    .where(CRMCompany.external_id.in_(company_external_ids))
)

# Process each company
for company in companies:
    record = existing.get(company["id"]) or CRMCompany(...)
    
    # Map HubSpot properties
    record.name = properties.get("name")
    record.domain = properties.get("domain")
    record.number_of_employees = int(properties.get("numberofemployees"))
    record.annual_revenue = float(properties.get("annualrevenue"))
    
    # Store raw data
    record.properties = properties
    record.data_payload = company
    
    # Persist
    db.add(record)
    
await db.commit()
```

## Tasks

### integrations.sync_integration_data

- **Queue**: `integrations`
- **Purpose**: Main sync task - performs provider sync (fetch via Nango proxy, persist records, update metadata)
- **Args**:
  - `organisation_id: str` - Organisation UUID
  - `integration_id: str` - Integration UUID
  - `category: str` - Integration category (e.g., 'crm', 'calendar')
  - `provider: str` - Provider name (e.g., 'hubspot', 'google')
  - `provider_config_key: Optional[str]` - Provider config key from Nango
  - `sync_name: Optional[str]` - Sync name (e.g., 'deals', 'companies')
  - `sync_type: Optional[str]` - Sync type ('INITIAL', 'INCREMENTAL')
  - `sync_variant: Optional[str]` - Sync variant
  - `incremental: bool = True` - Whether to do incremental sync
- **Retries**: 3 attempts, 60s delay, acks-late to avoid losing work mid-sync
- **Features**:
  - Triggered by webhook handler in FastAPI (nango_webhook_service.py)
  - Fetches data from external providers via Nango proxy
  - Persists synchronized records to database
  - Updates integration metadata and sync status
  - Provider-agnostic design via dispatcher pattern
  - Comprehensive error handling with retry logic

**Retry Behavior**:

- **Retryable errors**: Network issues, temporary provider outages, rate limiting
- **Non-retryable errors**: Integration not found, unsupported data type, missing configuration

### integrations.refresh_expiring_tokens

- **Queue**: `integrations`
- **Purpose**: Hourly sweep to refresh tokens expiring within a buffer window
- **Args**: `buffer_hours: int = 2`
- **Retries**: 3 attempts with exponential backoff
- **Features**:
  - Scheduled via Celery beat (`refresh-expiring-oauth-tokens`)
  - Maintains token validity across all integrations
  - Automatically refreshes tokens within configurable buffer window
  - Prevents integration breakage due to expired tokens

### integrations.refresh_single_integration

- **Queue**: `integrations_priority`
- **Purpose**: Manual refresh for a specific integration (API-triggered)
- **Args**: `organisation_id: str`, `integration_id: str`
- **Features**:
  - Priority queue to ensure user-driven refreshes don't wait behind scheduled jobs
  - Triggered on-demand via API endpoints
  - Useful for testing or manual token renewal

## Queues

- **integrations** – Scheduled sweep and regular sync jobs
- **integrations_priority** – High-priority lane for manual refresh/sync requests

## Usage

### Start the worker

```powershell
celery -A src.workers.integrations_worker.celery_app worker -l info -Q integrations,integrations_priority --pool=solo
```

### Start the worker with beat scheduler

```powershell
celery -A src.workers.integrations_worker.celery_app worker -l info --beat -Q integrations,integrations_priority --pool=solo
```

### Test connectivity

```powershell
python -c "from src.workers.integrations_worker.celery_app import app; result = app.send_task('integrations.refresh_expiring_tokens'); print(result.get())"
```

## Configuration

- **Exchange**: `integrations` (direct)
- **Default Queue**: `integrations`
- **Default Routing Key**: `integrations`
- **Environment**: Uses same .env file as other workers
- **Beat Scheduler**: `refresh-expiring-oauth-tokens` (hourly, 2h buffer)

### Queue Configuration

The worker uses dedicated `integrations` and `integrations_priority` exchanges with proper routing keys to ensure task routing aligns with the correct queues.

### Task Routes

```python
{
    "integrations.refresh_expiring_tokens": {"queue": "integrations", "routing_key": "integrations"},
    "integrations.refresh_single_integration": {"queue": "integrations_priority", "routing_key": "integrations_priority"},
    "integrations.sync_integration_data": {"queue": "integrations", "routing_key": "integrations"},
}
```

## Task Separation

This worker focuses on integration-level operations:

- OAuth token management and renewal.
- Webhook processing from external providers (Nango).
- Data synchronization and persistence.
- Integration metadata updates.

For organization-related tasks, use the Organization Worker instead.

## Global Worker Commands

For quick startup reference, here's how to run **all** workers, including this one, from the main project:

```powershell
celery -A src.workers.integrations_worker.celery_app worker -l info -Q integrations,integrations_priority --pool=solo
```

## Dependencies

- **SQLAlchemy**: For database operations
- **Celery**: For task queue management
- **Nango**: OAuth and integration proxy platform

## Error Handling

- Tasks include retry mechanisms with exponential backoff
- Acks-late pattern used for sync operations to prevent data loss mid-sync
- Comprehensive logging for debugging integration issues
- Non-retryable failures skip retry logic (e.g., missing integration, unsupported data type)
- Sync history tracks success/failure with detailed error messages

## Development

### Adding New Integrations

To add a new CRM provider or data type:

1. **Create Fetcher Function** (`providers/crm/<provider>.py`):

```python
async def fetch_companies(
    *,
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    data_type: str | None = None,
    provider_config_key: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch companies from provider via Nango proxy."""
    # Implement provider-specific logic
    if is_initial or not last_synced_at:
        # Full sync
        return await _list_all_companies(nango, connection_id, provider)
    else:
        # Incremental sync
        return await _search_companies_since(nango, connection_id, provider, last_synced_at)
```

1. **Register Fetcher** (`providers/registry.py`):

```python
from .crm.<provider> import fetch_companies as provider_companies_fetch

_REGISTRY[("crm", "<provider>")] = {
    "companies": provider_companies_fetch
}
```

1. **Update Adapter** (`adapters/crm_adapter.py`):

```python
if data_type == "companies":
    return await service.sync_companies(
        integration=integration,
        companies=records,
        provider=provider,
    )
```

1. **Create/Update Sync Service** (`src/service/integrations/crm_sync_service.py`):

```python
async def sync_companies(
    self,
    *,
    integration: KnowledgeIntegration,
    companies: Iterable[dict[str, Any]],
    provider: str,
) -> dict[str, int]:
    # Load existing records
    # Map provider fields to normalized schema
    # Upsert to database
    # Update sync history
    pass
```

1. **Create Database Model** (`src/domain/models/organisation_tables/sync/`):

```python
class CRMCompany(SyncedRecordMixin, table=True):
    __tablename__ = "crm_companies"
    company_id: UUID = Field(default_factory=uuid4, primary_key=True)
    external_id: str = Field(...)
    # Add normalized fields
```

1. **Create Migration**:

```bash
# Generate migration
alembic -c migrations/org_migrations/alembic.ini revision --autogenerate -m "add_crm_companies_table"

# Review and edit migration file
# Run migration
python migrations/migration_manager.py run-org
```

### Testing

```powershell
# Test fetcher
python -c "
from src.workers.integrations_worker.providers.crm.hubspot import fetch_companies
from src.service.integrations.nango_client import NangoClient
import asyncio

async def test():
    nango = NangoClient()
    companies = await fetch_companies(
        nango=nango,
        connection_id='test-connection-id',
        is_initial=True,
        last_synced_at=None
    )
    print(f'Fetched {len(companies)} companies')
    
asyncio.run(test())
"

# Test sync task
python -c "
from src.workers.integrations_worker.celery_app import app
result = app.send_task(
    'integrations.sync_integration_data',
    kwargs={
        'organisation_id': 'test-org-id',
        'integration_id': 'test-integration-id',
        'category': 'crm',
        'provider': 'hubspot',
        'sync_name': 'companies'
    }
)
print(result.get(timeout=60))
"
```

## Troubleshooting

### Common Issues

**Issue**: `relation "crm_companies" does not exist`

- **Cause**: Database migration not run
- **Solution**: Run `python migrations/migration_manager.py run-org`

**Issue**: `No module named 'src.domain.models.base_mixins'`

- **Cause**: Incorrect import path in model file
- **Solution**: Check relative imports match file location
  - Files in `sync/` folder: `from ..base_mixins import utcnow`
  - Files in `sync/crm/` folder: `from ...base_mixins import utcnow`

**Issue**: Task retries indefinitely with `Integration not found`

- **Cause**: Non-retryable error being retried
- **Solution**: Add error type to `NON_RETRYABLE_ERRORS` in `tasks/sync_tasks.py`

**Issue**: Webhook not triggering sync

- **Cause**: Webhook signature validation failing or mapping not found
- **Solution**:
  - Check `X-Nango-Signature` header validation
  - Verify connection_id mapping exists in database
  - Check Nango webhook URL configuration

**Issue**: Duplicate records being created

- **Cause**: Upsert logic not finding existing records
- **Solution**: Verify `external_id` uniqueness constraint and query logic

### Debugging

Enable verbose logging:

```python
# In celery_app.py
import logging
logging.getLogger('src.workers.integrations_worker').setLevel(logging.DEBUG)
```

Monitor Celery events:

```powershell
celery -A src.workers.integrations_worker.celery_app events
```

Inspect task state:

```powershell
celery -A src.workers.integrations_worker.celery_app inspect active
celery -A src.workers.integrations_worker.celery_app inspect scheduled
celery -A src.workers.integrations_worker.celery_app inspect reserved
```

### Performance Tips

- Use `incremental=True` for regular syncs to reduce API calls
- Implement pagination in fetchers to handle large datasets
- Add indexes to foreign keys and frequently queried columns
- Monitor sync_history table for slow syncs
- Consider batch size limits for bulk inserts

## Architecture Decisions

### Why Nango?

- Handles OAuth token refresh automatically
- Provides unified API across providers
- Manages rate limiting and retries
- Simplifies integration maintenance

### Why Celery?

- Async task processing decoupled from API requests
- Reliable retry mechanisms
- Priority queues for critical tasks
- Scheduled tasks for token refresh

### Why Provider Registry?

- Decouples provider-specific logic
- Easy to add new providers
- Testable in isolation
- Clear separation of concerns

### Why Sync History?

- Audit trail for compliance
- Debugging failed syncs
- Performance monitoring
- Analytics on sync patterns

---

**Last Updated**: October 25, 2025  
**Version**: 2.0.0  
**Maintainer**: CopilotGTM Team
