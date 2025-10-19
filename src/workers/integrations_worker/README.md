# Integrations Worker

This worker executes integration-specific jobs: OAuth token maintenance, webhook processing, and data sync orchestration.

## Tasks

### integrations.refresh_expiring_tokens
- **Queue**: `integrations`
- **Purpose**: Hourly sweep to refresh tokens expiring within a buffer window.
- **Args**: `buffer_hours: int = 2`
- **Retries**: 3 attempts with exponential backoff.
- **Features**:
  - Scheduled via Celery beat (`refresh-expiring-oauth-tokens`).
  - Maintains token validity across all integrations.
  - Automatically refreshes tokens within configurable buffer window.

### integrations.refresh_single_integration
- **Queue**: `integrations_priority`
- **Purpose**: Manual refresh for a specific integration (API-triggered).
- **Args**: `organisation_id: str`, `integration_id: str`
- **Features**: 
  - Priority queue to ensure user-driven refreshes don't wait behind scheduled jobs.
  - Triggered on-demand via API endpoints.

### integrations.sync_integration_data
- **Queue**: `integrations`
- **Purpose**: Main sync task - performs provider sync (fetch via Nango proxy, persist records, update metadata).
- **Args**: `organisation_id: str`, `integration_id: str`, `category: str`, `provider: str`, `data_types: list[str]`, `incremental: bool`, `sync_name: Optional[str]`
- **Retries**: 3 attempts, 60s delay, acks-late to avoid losing work mid-sync.
- **Features**:
  - Triggered by webhook handler in FastAPI (nango_webhook_service.py).
  - Handles multiple data types in a single task execution.
  - Fetches data from external providers via Nango proxy.
  - Persists synchronized records to database.
  - Updates integration metadata and sync status.
  - Provider-agnostic design via dispatcher pattern.

## Queues

- **integrations** – scheduled sweep and regular sync jobs.
- **integrations_priority** – high-priority lane for manual refresh/sync requests.

## Usage

### Start the worker:
```powershell
celery -A src.workers.integrations_worker.celery_app worker -l info -Q integrations,integrations_priority --pool=solo
```

### Start the worker with beat scheduler:
```powershell
celery -A src.workers.integrations_worker.celery_app worker -l info --beat -Q integrations,integrations_priority --pool=solo
```

### Test connectivity:
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

- Tasks include retry mechanisms with exponential backoff.
- Acks-late pattern used for sync operations to prevent data loss mid-sync.
- Comprehensive logging for debugging integration issues.
