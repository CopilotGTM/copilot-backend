# Integrations Worker

The Integrations Worker is a dedicated Celery worker that handles all integration-related background tasks, including OAuth token management and data synchronization.

## Overview

This worker provides:
- **Automatic Token Refresh**: Proactively refreshes OAuth tokens before expiry
- **Manual Token Operations**: On-demand token refresh via API
- **Data Synchronization**: Scheduled and manual sync of integration data (Step 5)
- **Priority Queue Management**: Separate queues for scheduled vs. manual operations

## Architecture

### Queues

The worker uses two queues with different priorities:

1. **`integrations`** (Normal Priority)
   - Scheduled token refresh (hourly)
   - Batch data synchronization
   - Background maintenance tasks

2. **`integrations_priority`** (High Priority)
   - Manual token refresh requests
   - On-demand sync operations
   - User-initiated actions

### Task Routes

| Task | Queue | Description |
|------|-------|-------------|
| `integrations.refresh_expiring_tokens` | integrations | Scheduled hourly refresh |
| `integrations.refresh_single_integration` | integrations_priority | Manual refresh |
| `integrations.sync_integration` | integrations_priority | Manual sync (Step 5) |
| `integrations.sync_all_active_integrations` | integrations | Scheduled sync (Step 5) |

### Beat Schedule

The worker includes a Celery Beat schedule for periodic tasks:

```python
# Runs every hour at minute 0 (12:00, 1:00, 2:00, etc.)
"refresh-expiring-oauth-tokens": {
    "task": "integrations.refresh_expiring_tokens",
    "schedule": crontab(minute=0),
    "args": (2,),  # 2-hour buffer
}
```

## Configuration

### Environment Variables

```bash
# Redis connection
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/0

# Database
MASTER_DB_CONNECTION_STRING=postgresql://user:pass@localhost/master_db
ORG_DB_CONNECTION_STRING_TEMPLATE=postgresql://user:pass@localhost/org_{org_id}

# Nango configuration
NANGO_HOST=https://api.nango.dev
NANGO_SECRET_KEY=your-secret-key

# Worker settings
INTEGRATIONS_WORKER_CONCURRENCY=4
INTEGRATIONS_WORKER_PREFETCH_MULTIPLIER=4
```

### Worker Configuration

The worker configuration is defined in `config.py`:

```python
class IntegrationsWorkerConfig(BaseWorkerConfig):
    # Exchange and routing
    exchange_name = "integrations"
    exchange_type = "direct"
    
    # Queues
    queues = [
        Queue("integrations", routing_key="integrations"),
        Queue("integrations_priority", routing_key="integrations_priority", priority=10),
    ]
    
    # Task routes
    task_routes = {
        "integrations.refresh_expiring_tokens": {
            "queue": "integrations",
            "routing_key": "integrations",
        },
        "integrations.refresh_single_integration": {
            "queue": "integrations_priority",
            "routing_key": "integrations_priority",
        },
    }
```

## Running the Worker

### Local Development

```bash
# Start the worker with beat scheduler
celery -A src.workers.integrations_worker.celery_app worker \
    --loglevel=info \
    --concurrency=4 \
    --beat

# Start worker only (without beat)
celery -A src.workers.integrations_worker.celery_app worker \
    --loglevel=info \
    --concurrency=4

# Start beat only (separate process)
celery -A src.workers.integrations_worker.celery_app beat \
    --loglevel=info
```

### Production (Docker)

```yaml
# docker-compose.yml
services:
  integrations-worker:
    build: .
    command: celery -A src.workers.integrations_worker.celery_app worker --loglevel=info --concurrency=4
    environment:
      - CELERY_BROKER_URL=${CELERY_BROKER_URL}
      - CELERY_RESULT_BACKEND=${CELERY_RESULT_BACKEND}
      - MASTER_DB_CONNECTION_STRING=${MASTER_DB_CONNECTION_STRING}
      - ORG_DB_CONNECTION_STRING_TEMPLATE=${ORG_DB_CONNECTION_STRING_TEMPLATE}
      - NANGO_HOST=${NANGO_HOST}
      - NANGO_SECRET_KEY=${NANGO_SECRET_KEY}
    depends_on:
      - redis
      - postgres
    restart: unless-stopped

  integrations-beat:
    build: .
    command: celery -A src.workers.integrations_worker.celery_app beat --loglevel=info
    environment:
      - CELERY_BROKER_URL=${CELERY_BROKER_URL}
    depends_on:
      - redis
    restart: unless-stopped
```

### Kubernetes (Helm)

```yaml
# values.yaml
integrationsWorker:
  enabled: true
  replicas: 2
  concurrency: 4
  resources:
    requests:
      memory: "512Mi"
      cpu: "250m"
    limits:
      memory: "1Gi"
      cpu: "500m"

integrationsBeat:
  enabled: true
  resources:
    requests:
      memory: "256Mi"
      cpu: "100m"
    limits:
      memory: "512Mi"
      cpu: "200m"
```

## Tasks

### Token Refresh Tasks

#### `refresh_expiring_tokens_task`

Scheduled task that runs hourly to refresh tokens expiring soon.

```python
@app.task(name="integrations.refresh_expiring_tokens")
def refresh_expiring_tokens_task(buffer_hours: int = 2):
    """
    Refresh tokens expiring within buffer_hours.
    
    Returns:
        {
            "organizations_processed": 5,
            "total_integrations_checked": 23,
            "total_successful_refreshes": 12,
            "total_failed_refreshes": 0,
            "total_deactivated": 0
        }
    """
```

**Features:**
- Processes all active organizations
- Checks integrations expiring within 2 hours (configurable)
- Retries failed refreshes (max 3 attempts, 5-min delay)
- Aggregates results across all orgs

**Monitoring:**
```python
# Check task status
result = refresh_expiring_tokens_task.apply_async()
print(result.get())

# View recent runs in Flower
# http://localhost:5555/task/integrations.refresh_expiring_tokens
```

#### `refresh_single_integration_task`

Manual task to refresh a specific integration's token.

```python
@app.task(name="integrations.refresh_single_integration")
def refresh_single_integration_task(
    organisation_id: str,
    integration_id: str
):
    """
    Manually refresh a single integration's token.
    
    Returns:
        {
            "success": True,
            "integration_id": "uuid",
            "provider": "hubspot",
            "new_expires_at": "2024-06-15T12:00:00Z"
        }
    """
```

**Use Cases:**
- User manually reconnects integration
- Token refresh after failed API call
- Admin troubleshooting

**Example:**
```python
# Trigger from API endpoint
from src.workers.integrations_worker.tasks import refresh_single_integration_task

result = refresh_single_integration_task.apply_async(
    args=["org-id-123", "integration-id-456"],
    queue="integrations_priority"
)
```

### Data Sync Tasks (Step 5)

Coming soon: Tasks for syncing integration data (files, contacts, etc.)

## Monitoring

### Task Status

```bash
# View active tasks
celery -A src.workers.integrations_worker.celery_app inspect active

# View registered tasks
celery -A src.workers.integrations_worker.celery_app inspect registered

# View beat schedule
celery -A src.workers.integrations_worker.celery_app inspect scheduled
```

### Flower Dashboard

```bash
# Start Flower
celery -A src.workers.integrations_worker.celery_app flower

# Access dashboard
# http://localhost:5555
```

### Metrics

The worker emits the following metrics:

- Task execution count
- Task duration
- Task success/failure rate
- Queue length
- Worker pool utilization

## Error Handling

### Retry Strategy

All tasks implement exponential backoff with max retries:

- **Token Refresh (Scheduled)**: 3 retries, 5-min delay
- **Token Refresh (Manual)**: 3 retries, 1-min delay

### Failure Scenarios

| Scenario | Behavior |
|----------|----------|
| Nango API down | Retry with backoff, log error |
| Invalid token | Deactivate integration, notify user |
| Database error | Retry, skip org if persistent |
| Network timeout | Retry with exponential backoff |

### Dead Letter Queue

Failed tasks after max retries are sent to:
- Queue: `integrations_dlq`
- Retention: 7 days
- Manual inspection required

## Troubleshooting

### Common Issues

#### Tasks Not Executing

```bash
# Check worker is running
celery -A src.workers.integrations_worker.celery_app inspect active

# Check queue has messages
redis-cli
> LLEN integrations
> LLEN integrations_priority
```

#### High Memory Usage

```bash
# Reduce concurrency
celery -A src.workers.integrations_worker.celery_app worker --concurrency=2

# Enable prefetch limit
celery -A src.workers.integrations_worker.celery_app worker --prefetch-multiplier=1
```

#### Token Refresh Failures

```bash
# Check Nango connectivity
curl -H "Authorization: Bearer $NANGO_SECRET_KEY" \
  https://api.nango.dev/config

# Check logs
docker logs integrations-worker | grep "token refresh"

# Manually trigger refresh
from src.workers.integrations_worker.tasks import refresh_single_integration_task
refresh_single_integration_task.apply_async(args=["org-id", "int-id"])
```

## Development

### Adding New Tasks

1. Create task in `tasks/` directory
2. Register in `tasks/__init__.py`
3. Add route in `config.py`
4. Import in `celery_app.py`
5. Update this README

Example:
```python
# tasks/new_feature.py
from src.workers.integrations_worker.celery_app import app

@app.task(name="integrations.new_feature")
def new_feature_task():
    pass

# tasks/__init__.py
from .new_feature import new_feature_task
__all__ = [..., "new_feature_task"]

# config.py
task_routes = {
    "integrations.new_feature": {
        "queue": "integrations",
        "routing_key": "integrations",
    },
}
```

### Testing

```bash
# Run unit tests
pytest tests/workers/integrations_worker/

# Test task execution
python -c "
from src.workers.integrations_worker.tasks import refresh_expiring_tokens_task
result = refresh_expiring_tokens_task.apply()
print(result.get())
"

# Load testing
celery -A src.workers.integrations_worker.celery_app inspect stats
```

## Related Documentation

- [Token Refresh Implementation Guide](../../../docs/Technical%20Documentation/token-refresh-implementation-guide.md)
- [Nango Integration Guide](../../../docs/Technical%20Documentation/nango-integration-guide.md)
- [Frontend Integration Guide](../../../docs/API%20Documentation/frontend-integration-guide.md)
- [OAuth Flow Documentation](../../../docs/Technical%20Documentation/oauth-flow-documentation.md)

## Support

For issues or questions:
1. Check logs: `docker logs integrations-worker`
2. Review Flower dashboard: http://localhost:5555
3. Inspect task state: `celery -A src.workers.integrations_worker.celery_app inspect active`
4. Contact platform team
