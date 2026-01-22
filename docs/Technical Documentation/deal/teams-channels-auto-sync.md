# Teams Channels Auto-Sync Implementation

## Overview

Implemented automatic Teams channel synchronization with both scheduled and on-demand sync capabilities.

## Architecture

### 1. **Scheduled Sync (Celery Beat)**

- **File**: `src/workers/integrations_worker/tasks/teams_sync_tasks.py`
- **Task**: `integrations.sync_teams_channels`
- **Schedule**: Every 3 hours (configured in Celery Beat)
- **Behavior**:
  - Scans all approved organizations
  - Finds active Teams integrations (user-level)
  - Triggers channel sync for each integration
  - Filters: approved orgs + active integrations + non-deleted

### 2. **On-Demand Sync (Frontend Refresh Button)**

- **Endpoint**: `POST /users/integrations/{integration_id}/teams/sync-channels`
- **File**: `src/api/routers/integrations/integration_routes.py`
- **Behavior**:
  - User clicks "Refresh" button for their Teams integration
  - Immediately triggers channel sync for that specific integration
  - Returns task ID for tracking

### 3. **Celery Beat Configuration**

- **File**: `src/workers/integrations_worker/config.py`
- **Schedule**: `crontab(minute=0, hour="*/3")` - Every 3 hours
- **No Helm changes needed** - schedule runs inside worker container

## User-Level Integration Model

### Why User-Level for Teams?

✅ **Correct Approach** for these reasons:

- Teams channels are **user-scoped** (personal access)
- Microsoft Graph API requires **user OAuth** (can't access other users' channels)
- Each user has different **channel permissions**
- Each user belongs to different **teams/channels**

### Expected Behavior

- Organization with 50 users → 50 Teams integrations ✅
- Each integration syncs independently
- Each user only sees their accessible channels
- Centralized integrations (HubSpot) remain org-level

## Data Flow

```text
1. Scheduled Sync (Every 3 hours)
   └─> sync_teams_channels_task (no args)
       └─> Gets all approved orgs
           └─> For each org: gets active Teams integrations
               └─> For each integration: triggers sync_integration_data_task
                   └─> Calls fetch_channels via Nango
                       └─> Writes to messaging_channels table

2. Manual Sync (Refresh button)
   └─> POST /users/integrations/{id}/teams/sync-channels
       └─> sync_teams_channels_task (org_id, integration_id)
           └─> Triggers sync for specific integration only
               └─> Same flow as above
```

## Database Tables

### `messaging_channels`

- Populated by channels sync
- Default: `is_monitored=false`
- User must enable monitoring via UI/API

### `messaging_messages`

- Only syncs messages from monitored channels
- Empty until channels are marked as monitored

## Enabling Channel Monitoring

### Option 1: API (To be built)

```http
POST /integrations/{integration_id}/teams/channels/{channel_id}/monitor
```

### Option 2: Link to Deal

```python
messaging_service.link_channel_to_deal(
    channel_id=channel_id,
    deal_id=deal_id
)
# Automatically sets is_monitored=true
```

### Option 3: Manual (temporary)

```sql
UPDATE messaging_channels 
SET is_monitored = 'true' 
WHERE channel_id = '<uuid>';
```

## Configuration

### Environment Variables (No changes needed)

- Uses existing Redis for Celery
- Uses existing database connections
- Uses existing Nango configuration

### Celery Beat Schedule

```python
"sync-teams-channels-all-orgs": {
    "task": "integrations.sync_teams_channels",
    "schedule": crontab(minute=0, hour="*/3"),  # Every 3 hours
    "kwargs": {},  # Empty = all orgs
}
```

### Task Routing

```python
"integrations.sync_teams_channels": {
    "queue": "integrations",
    "routing_key": "integrations",
}
```

## Testing

### Test Discovery

```bash
python test-local-scripts/test_teams_discovery.py
```

Shows all organizations and their Teams integrations.

### Manual Trigger (All Orgs)

```bash
# Via Python
from src.workers.integrations_worker.tasks.teams_sync_tasks import sync_teams_channels_task
result = sync_teams_channels_task.delay()

# Via Celery CLI
celery -A src.workers.integrations_worker.celery_app call integrations.sync_teams_channels
```

### Manual Trigger (Specific Org)

```bash
python -c "
from src.workers.integrations_worker.tasks.teams_sync_tasks import sync_teams_channels_task
result = sync_teams_channels_task.apply_async(kwargs={
    'organisation_id': 'edf96b50-4acc-4e8c-aa0a-ad9c731faf23',
    'integration_id': 'fcf4de51-44cc-4fc0-8a65-0bc285bab1c6'
})
print(result.id)
"
```

### Frontend Integration

```typescript
// Refresh button click handler
async function refreshTeamsChannels(integrationId: string) {
  const response = await fetch(
    `/users/integrations/${integrationId}/teams/sync-channels`,
    { method: 'POST' }
  );
  const data = await response.json();
  console.log('Sync triggered:', data.task_id);
  // Poll task status or show success message
}
```

## Deployment

### Helm Values (No changes needed)

The periodic task runs automatically via Celery Beat inside the existing worker container.

### Verify Beat is Running

```bash
# Check worker logs for Beat scheduler
kubectl logs -l app=copilot-backend-worker | grep beat

# Should see:
# "celery beat v5.x.x is starting"
# "Scheduler: Running beat schedule"
```

### Monitor Periodic Execution

```bash
# View scheduled tasks
celery -A src.workers.integrations_worker.celery_app inspect scheduled

# View active tasks
celery -A src.workers.integrations_worker.celery_app inspect active
```

## Files Changed

### New Files

1. `src/workers/integrations_worker/tasks/teams_sync_tasks.py` - Teams-specific sync tasks
2. `test-local-scripts/test_teams_discovery.py` - Testing utility

### Modified Files

1. `src/workers/integrations_worker/config.py` - Added beat schedule
2. `src/api/routers/integrations/integration_routes.py` - Added refresh endpoint
3. `src/workers/integrations_worker/services/dispatcher.py` - Already has monitored channel logic

### Deprecated Files

1. `scripts/maintenance/sync_teams_channels_all_orgs.py` - Replaced by periodic task
2. `test-local-scripts/trigger_teams_channels_sync.py` - Use API endpoint instead

## Next Steps

### Immediate

1. ✅ Restart Celery worker to load new task
2. ✅ Verify Beat schedule is active
3. ✅ Test manual sync via API endpoint

### Short-term

1. Build frontend UI:
   - List channels with monitoring toggle
   - Refresh button for each integration
   - Link channel to deal UI

### Long-term

1. Add API endpoints:
   - `GET /integrations/{id}/teams/channels` - List all channels
   - `POST /integrations/{id}/teams/channels/{channel_id}/monitor` - Toggle monitoring
   - `POST /integrations/{id}/teams/channels/{channel_id}/link-deal` - Link to deal

2. Add monitoring:
   - Track sync success/failure rates
   - Alert on repeated failures
   - Dashboard for sync status

## FAQ

**Q: Why every 3 hours instead of real-time?**
A: Balances freshness vs API rate limits. Teams channels don't change frequently. Can adjust schedule as needed.

**Q: What if a sync fails?**
A: Task has retry logic (3 retries with 60s delay). Failures are logged. Next scheduled run will try again.

**Q: Can I change the schedule?**
A: Yes, edit `config.py` beat_schedule. Options:

- `crontab(hour="*/6")` - Every 6 hours
- `crontab(hour=0)` - Daily at midnight
- `crontab(minute="*/30")` - Every 30 minutes

**Q: How to disable the scheduled sync?**
A: Comment out the beat_schedule entry in `config.py`:

```python
# "sync-teams-channels-all-orgs": { ... }
```

**Q: Does this affect messages sync?**
A: No, this only syncs channels. Messages sync (every 15 min via Nango) only fetches from monitored channels.
