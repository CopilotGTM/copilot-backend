# Deprecated Nango Sync Webhook Functions

> **Status**: DEPRECATED
> **Deprecated Date**: 2026-01-11
> **Replacement**: Celery Beat Scheduled Sync Tasks
> **Removal Target**: TBD

## Overview

The Nango sync webhook-driven data pull has been replaced with application-level Celery Beat scheduled sync tasks. This provides better control over sync frequency, error handling, and resource management.

## Deprecated Components

### 1. Webhook Route Handler (Commented Out)

**File**: `src/api/routers/integrations/oauth_routes.py`

**Function**: `handle_nango_webhook` - sync case handling

```python
# The following code block has been commented out in oauth_routes.py:
case "sync":
    # Validate required fields
    if not payload.get("connectionId") or not payload.get("providerConfigKey"):
        ...
    # Fetch mapping details
    mapping_details = await webhook_service._get_connection_mapping(...)
    ...
    return await webhook_service._handle_sync_webhook(...)
```

**Reason**: Sync webhooks are now acknowledged with 200 OK but not processed. CRM data sync is handled by scheduled tasks.

---

### 2. NangoWebhookService - Sync Methods

**File**: `src/service/integrations/nango_webhook_service.py`

The following methods are deprecated but NOT removed (may be used for other integration types):

| Method | Description | Status |
|--------|-------------|--------|
| `_handle_sync_webhook()` | Processes sync webhook payload and dispatches sync tasks | DEPRECATED for CRM |
| `_get_connection_mapping()` | Maps Nango connection_id to organisation_id and integration_id | Still used by auth webhook |
| `_dispatch_sync_task()` | Dispatches Celery tasks based on sync type | DEPRECATED for CRM |

---

### 3. Legacy Sync Tasks

**File**: `src/workers/integrations_worker/tasks/sync_tasks.py`

| Task | Description | Status |
|------|-------------|--------|
| `sync_integration_data_task` | Generic sync task dispatched by webhook | DEPRECATED for CRM |

---

## Replacement: Celery Beat Scheduled Sync

### New Task Architecture

CRM entities now use a three-tier scheduled sync architecture:

| Entity | Tier 1 (Celery Beat) | Tier 2 (Per Org) | Tier 3 (Manual) |
|--------|----------------------|------------------|-----------------|
| Deals | `sync_crm_deals_all_orgs` | `sync_crm_deals_org` | `sync_crm_deals_integration` |
| Pipelines | `sync_crm_pipelines_all_orgs` | `sync_crm_pipelines_org` | `sync_crm_pipelines_integration` |
| Contacts | `sync_crm_contacts_all_orgs` | `sync_crm_contacts_org` | `sync_crm_contacts_integration` |
| Companies | `sync_crm_companies_all_orgs` | `sync_crm_companies_org` | `sync_crm_companies_integration` |
| Users | `sync_crm_users_all_orgs` | `sync_crm_users_org` | `sync_crm_users_integration` |

### Schedule Configuration

**File**: `src/workers/integrations_worker/config.py`

```python
# Celery Beat Schedule
"sync-crm-users-all-orgs": crontab(minute=0, hour="*/3")      # Every 3 hours at :00
"sync-crm-pipelines-all-orgs": crontab(minute=10, hour="*/3") # Every 3 hours at :10
"sync-crm-contacts-all-orgs": crontab(minute=20, hour="*/3")  # Every 3 hours at :20
"sync-crm-companies-all-orgs": crontab(minute=30, hour="*/3") # Every 3 hours at :30
"sync-crm-deals-all-orgs": crontab(minute=45)                 # Every hour at :45
```

### Task Files

```
src/workers/integrations_worker/tasks/crm/
├── __init__.py
├── utils.py                    # Shared utilities (timestamp helpers)
├── deprecating_webhook.md      # This file
├── deals/
│   ├── __init__.py
│   ├── all_orgs_task.py       # Tier 1
│   ├── org_task.py            # Tier 2
│   └── integration_task.py    # Tier 3
├── pipelines/                  # Same structure
├── contacts/                   # Same structure
├── companies/                  # Same structure
└── users/                      # Same structure
```

---

## Key Differences

| Aspect | Old (Webhook) | New (Scheduled) |
|--------|---------------|-----------------|
| Trigger | Nango webhook on data change | Celery Beat cron schedule |
| Frequency | Real-time (when Nango detects changes) | Configurable (hourly/3-hourly) |
| Control | Nango-driven | Application-driven |
| Incremental Sync | Via Nango delta | Via `connection_metadata` timestamps |
| Error Handling | Webhook retry | Celery task retry with backoff |
| Observability | Nango dashboard | Celery Flower + application logs |

---

## Migration Notes

1. **Nango webhook still receives sync events** - We return 200 OK to acknowledge but don't process
2. **Auth webhooks still work** - OAuth authorization flow is unchanged
3. **Historical sync data preserved** - No data migration needed
4. **Integration status tracking** - Now updated by scheduled tasks via `last_synced_at`, `last_sync_status`, `last_sync_error`

---

## Future Cleanup

When ready to fully remove deprecated code:

1. Remove commented sync case from `oauth_routes.py`
2. Remove `_handle_sync_webhook()` and `_dispatch_sync_task()` from `NangoWebhookService` (if not used elsewhere)
3. Remove `sync_integration_data_task` from `sync_tasks.py` (if not used elsewhere)
4. Update Nango dashboard to disable sync webhooks (optional - reduces noise)
