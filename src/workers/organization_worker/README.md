# Organization Worker

This worker handles organization-related background tasks, specifically administrative and organization-scoped operations.

## Tasks

### organization.create

- **Queue**: `organization`
- **Purpose**: Create new organization records and initialize related resources.
- **Args**: `organisation_id: str, ...`
- **Retries**: Configurable via base worker settings.
- **Features**:
  - Orchestrates creation workflows for new organizations.
  - Can trigger provisioning of DB schemas, roles, or integrations.

### organization.test_task

- **Queue**: `organization`
- **Purpose**: Simple test task for queue connectivity.
- **Args**: `message: str`
- **Features**:
  - Verifies that the worker and task routing is functional.

### organization.ping

- **Queue**: `organization`
- **Purpose**: Connectivity check.
- **Args**: none
- **Features**:
  - Simple return message to verify worker is running and reachable.

## Queues

- **organization**: For organization-related tasks
- **admin**: For administrative tasks that may overlap with organization control.

## Usage

### Start the worker

```powershell
celery -A src.workers.organization_worker.celery_app worker -l info -Q organization,admin --pool=solo
```

### Test connectivity

```powershell
python -c "from src.workers.organization_worker.celery_app import app; result = app.send_task('organization.ping'); print(result.get())"
```

## Configuration

- **Exchange**: `organization` (direct)
- **Default Queue**: `organization`
- **Default Routing Key**: `organization`
- **Environment**: Uses same .env file as other workers

### Queue Configuration

The worker uses a dedicated `organization` exchange with proper routing keys to ensure task routing aligns with the correct queues.

### Task Routes

```python
{
    "organization.create": {"queue": "organization", "routing_key": "organization"},
    "organization.test_task": {"queue": "organization", "routing_key": "organization"},
    "organization.ping": {"queue": "organization", "routing_key": "organization"},
}
```

## Task Separation

This worker focuses on organization-level operations:

- Administrative setup and provisioning.
- Background tasks for organization lifecycle.
- Organizational health checks and connectivity tests.

For knowledge base-related tasks, use the Knowledge Worker instead.

## Global Worker Commands

For quick startup reference, here’s how to run **all** workers, including this one, from the main project:

```powershell
celery -A src.workers.organization_worker.celery_app worker -l info -Q organization,admin --pool=solo
```

- **SQLAlchemy**: For database operations
- **Celery**: For task queue management

## Error Handling

- Tasks can include retry mechanisms with exponential backoff.
- Comprehensive logging for debugging.
