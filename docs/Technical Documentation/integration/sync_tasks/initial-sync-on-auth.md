# Initial Sync on Authentication

**Last Updated**: January 2026
**Status**: Implementation Guide

## Overview

This document explains how the **initial sync** is triggered when a user authenticates a new integration via Nango. When a user connects their Google Calendar, Outlook, HubSpot, or any other integration, the system automatically fires the first sync to populate data immediately rather than waiting for the next scheduled sync cycle.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Auth Webhook Flow](#auth-webhook-flow)
3. [Integration-Level Task Design](#integration-level-task-design)
4. [Code Consistency Requirements](#code-consistency-requirements)
5. [Implementation by Integration Type](#implementation-by-integration-type)
6. [Task Dispatch Patterns](#task-dispatch-patterns)
7. [Error Handling](#error-handling)

---

## Architecture Overview

### Why Trigger Initial Sync on Auth?

When a user connects an integration:
1. **User expectation**: They expect to see their data (calendar events, emails, CRM deals) immediately
2. **Scheduled sync delay**: Without initial sync, users would wait until the next schedule cycle (could be hours)
3. **Better UX**: Immediate data population provides instant value

### Flow Diagram

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│  USER ACTION: Connects integration via OAuth flow                           │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  NANGO: Completes OAuth, sends authorization webhook                        │
│         POST /api/webhooks/nango/auth                                       │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  WEBHOOK HANDLER: nango_webhook_service.py                                  │
│                                                                             │
│  1. Validate webhook signature                                              │
│  2. Extract user_id, organisation_id from endUser tags                      │
│  3. Check if connection already exists (idempotency)                        │
│  4. Reactivate existing integration OR create new one                       │
│  5. ──────────────────────────────────────────────────────────────────────  │
│     *** TRIGGER INITIAL SYNC *** (Integration-Level Task)                   │
│  ──────────────────────────────────────────────────────────────────────────  │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  CELERY TASK: Integration-Level Sync (Tier 3)                               │
│                                                                             │
│  Task triggered: sync_{category}_integration_task                           │
│  Examples:                                                                  │
│  • sync_calendar_integration_task                                           │
│  • sync_email_integration_task                                              │
│  • sync_messaging_channels_integration_task                                 │
│  • sync_crm_deals_integration_task (+ other CRM entities)                   │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Supported Integrations

The following table shows all integration types supported for initial sync on authentication:

| Category | Integration Type | Initial Sync Service | Sync Pattern | Worker Tasks |
|----------|------------------|---------------------|--------------|--------------|
| **CRM** | `HUBSPOT` | ✅ Supported | Celery Chord (parallel + enrichment) | `sync_crm_*_org_task` (6 entities) |
| **CRM** | `SALESFORCE` | ✅ Supported | Celery Chord (parallel + enrichment) | `sync_crm_*_org_task` (6 entities) |
| **Calendar** | `GOOGLE_CALENDAR` | ✅ Supported | Direct task dispatch | `sync_calendar_integration_task` |
| **Calendar** | `OUTLOOK_CALENDAR` | ✅ Supported | Direct task dispatch | `sync_calendar_integration_task` |
| **Email** | `GMAIL` | ✅ Supported | Direct task dispatch | `sync_email_integration_task` |
| **Email** | `OUTLOOK_EMAIL` | ✅ Supported | Direct task dispatch | `sync_email_integration_task` |
| **Messaging** | `TEAMS` | ✅ Supported | Direct task dispatch | `sync_messaging_channels_integration_task` |

### CRM Entities Synced on Initial Auth

When a CRM integration (HubSpot or Salesforce) is authenticated, ALL 6 CRM entities sync in parallel:

| Entity | Task | Enrichment |
|--------|------|------------|
| Companies | `sync_crm_companies_org_task` | `skip_enrichment=True` |
| Contacts | `sync_crm_contacts_org_task` | `skip_enrichment=True` |
| Deals | `sync_crm_deals_org_task` | `skip_enrichment=True` |
| Users | `sync_crm_users_org_task` | `skip_enrichment=True` |
| Pipelines | `sync_crm_pipelines_org_task` | N/A |
| Activities | `sync_crm_activities_org_task` | `skip_enrichment=True` |

After all 6 tasks complete, the **enrichment callback** (`enrich_crm_after_initial_sync_task`) runs to enrich all records.

### Adding New Integrations

When adding a new integration type:

1. **Create worker tasks** in `src/workers/integrations_worker/tasks/{category}/`
2. **Update `InitialSyncService` class** in `initial_sync_service.py`:
   - Add the integration type to the appropriate constant (e.g., `EMAIL_INTEGRATION_TYPES`)
   - Or create a new constant and `_trigger_*_sync()` method if it's a new category
3. **Update this documentation** with the new integration

---

## Auth Webhook Flow

### Webhook Handler Location

**File**: `src/service/integrations/nango_webhook_service.py`

### Key Methods

**Webhook Service** (`src/service/integrations/nango_webhook_service.py`):

| Method | Purpose |
|--------|---------|
| `_handle_authorization_webhook()` | Main entry point for auth webhooks |
| `_reactivate_integration()` | Handles reconnection of existing integrations |
| `_handle_new_integration_auth()` | Creates new integration records |

**Initial Sync Service** (`src/service/integrations/initial_sync_service.py`):

| Class/Method | Purpose |
|--------------|---------|
| `InitialSyncService` | Service class for triggering initial sync on auth |
| `.trigger_initial_sync()` | Main dispatcher - routes to correct task based on integration type |
| `._trigger_crm_sync()` | CRM-specific orchestration with Celery chord pattern |
| `._trigger_calendar_sync()` | Calendar sync task dispatch |
| `._trigger_email_sync()` | Email sync task dispatch |
| `._trigger_messaging_sync()` | Messaging sync task dispatch |
| `initial_sync_service` | Singleton instance for easy import |

### Webhook Payload Structure

```python
{
    "type": "auth",
    "connectionId": "uuid-connection-id",
    "providerConfigKey": "google-calendar",  # or "outlook", "hubspot", etc.
    "endUser": {
        "endUserId": "user-uuid",
        "tags": {
            "organizationId": "org-uuid"
        }
    }
}
```

---

## Integration-Level Task Design

### Why Use Integration-Level (Tier 3) Tasks?

The three-tier task architecture is:

```text
Tier 1: All Organizations (Celery Beat scheduled)
    └─> Tier 2: Organization-level (processes all integrations for one org)
        └─> Tier 3: Integration-level (processes single integration)
```

**For initial sync on auth, we use Tier 3 (Integration-level) because:**

1. **Single integration scope**: Auth webhook is for ONE specific integration, not all integrations in the org
2. **Immediate execution**: We want to sync just the newly connected integration
3. **No cascading**: We don't need to query all orgs or all integrations
4. **Reusability**: Same task used for manual "Refresh" button

### Task Naming Convention

```text
Pattern: sync_{category}_integration_task

Examples:
• sync_calendar_integration_task
• sync_email_integration_task
• sync_messaging_channels_integration_task
• sync_crm_deals_integration_task
• sync_crm_contacts_integration_task
• sync_crm_companies_integration_task
• sync_crm_pipelines_integration_task
• sync_crm_users_integration_task
• sync_crm_activities_integration_task
```

---

## Code Consistency Requirements

### Critical: Same Underlying Function for All Tiers

**The most important architectural requirement**: Both Tier 2 (org_task) and Tier 3 (integration_task) MUST use the same underlying sync function.

#### Correct Pattern (Calendar, Email, Messaging)

```python
# org_task.py (Tier 2)
for integration in integrations:
    sync_integration_data_task.apply_async(...)  # Same function

# integration_task.py (Tier 3)
sync_integration_data_task.apply_async(...)  # Same function
```

Both tasks dispatch to `sync_integration_data_task`, ensuring:
- Consistent behavior between scheduled and manual syncs
- Single point of maintenance for sync logic
- No code duplication

#### Correct Pattern (CRM)

For CRM tasks, the sync logic is more complex. Both tiers should call a **shared helper function**:

```python
# crm/deals/sync_helper.py (shared module)
async def sync_deals_for_integration(
    *,
    session,
    nango: NangoClient,
    organisation_id: UUID,
    integration: KnowledgeIntegration,
    skip_enrichment: bool = False,
    force_full_sync: bool = False,
) -> dict[str, Any]:
    """Shared sync logic used by both org_task and integration_task."""
    # Fetch records
    # Sync to database
    # Update timestamps
    # Optionally run enrichment
    return result

# crm/deals/org_task.py (Tier 2)
for integration in integrations:
    result = await sync_deals_for_integration(
        session=session,
        nango=nango,
        organisation_id=org_id,
        integration=integration,
        skip_enrichment=skip_enrichment,
    )

# crm/deals/integration_task.py (Tier 3)
result = await sync_deals_for_integration(
    session=session,
    nango=nango,
    organisation_id=org_id,
    integration=integration,
    skip_enrichment=False,  # Integration task can run enrichment
    force_full_sync=force_full_sync,
)
```

### Why This Matters

If org_task and integration_task use **different** sync logic:

1. **Bug duplication**: Fix in one place doesn't fix the other
2. **Behavior drift**: Scheduled and manual syncs behave differently
3. **Maintenance burden**: Changes require updating multiple files
4. **Testing complexity**: Need to test same logic in multiple places

---

## Implementation by Integration Type

### Calendar Integration

**Files:**
- `src/workers/integrations_worker/tasks/calendar/integration_task.py`
- `src/workers/integrations_worker/tasks/calendar/org_task.py`

**Initial Sync Trigger:**
```python
# In nango_webhook_service.py
from src.workers.integrations_worker.tasks.calendar import sync_calendar_integration_task

sync_calendar_integration_task.delay(
    organisation_id=str(organisation_id),
    integration_id=str(integration.integration_id),
)
```

**Underlying Sync Function:** `sync_integration_data_task` with `sync_name="events"`

---

### Email Integration

**Files:**
- `src/workers/integrations_worker/tasks/email/integration_task.py`
- `src/workers/integrations_worker/tasks/email/org_task.py`

**Initial Sync Trigger:**
```python
from src.workers.integrations_worker.tasks.email import sync_email_integration_task

sync_email_integration_task.delay(
    organisation_id=str(organisation_id),
    integration_id=str(integration.integration_id),
)
```

**Underlying Sync Function:** `EmailDeltaSyncService.sync_integration()`

---

### Messaging Integration (Teams/Slack)

**Files:**
- `src/workers/integrations_worker/tasks/messaging/channels_integration_task.py`
- `src/workers/integrations_worker/tasks/messaging/channels_org_task.py`

**Initial Sync Trigger:**
```python
from src.workers.integrations_worker.tasks.messaging import sync_messaging_channels_integration_task

sync_messaging_channels_integration_task.delay(
    organisation_id=str(organisation_id),
    integration_id=str(integration.integration_id),
)
```

**Underlying Sync Function:** `sync_integration_data_task` with `sync_name="channels"` and `sync_name="chats"`

---

### CRM Integration (HubSpot, Salesforce)

CRM is special because it has **multiple entities** that need to sync:
- Deals
- Contacts
- Companies
- Pipelines
- Users
- Activities

**Initial Sync Pattern:** Uses Celery `chord` for parallel execution + enrichment callback

**Files:**
- `src/workers/integrations_worker/tasks/crm/deals/integration_task.py`
- `src/workers/integrations_worker/tasks/crm/deals/org_task.py`
- Similar for contacts, companies, pipelines, users, activities

**Initial Sync Trigger:**
```python
# Uses chord pattern for CRM
from celery import chord, group
from src.workers.integrations_worker.tasks.crm import (
    sync_crm_companies_org_task,
    sync_crm_contacts_org_task,
    sync_crm_deals_org_task,
    sync_crm_users_org_task,
    sync_crm_pipelines_org_task,
    sync_crm_activities_org_task,
    enrich_crm_after_initial_sync_task,
)

# Run all sync tasks in parallel, then enrichment
sync_group = group(
    sync_crm_companies_org_task.s(org_id_str, skip_enrichment=True),
    sync_crm_contacts_org_task.s(org_id_str, skip_enrichment=True),
    sync_crm_deals_org_task.s(org_id_str, skip_enrichment=True),
    sync_crm_users_org_task.s(org_id_str, skip_enrichment=True),
    sync_crm_pipelines_org_task.s(org_id_str),
    sync_crm_activities_org_task.s(org_id_str, skip_enrichment=True),
)

chord(sync_group)(enrich_crm_after_initial_sync_task.si(org_id_str, int_id_str))
```

**Note:** CRM uses org-level tasks for initial sync because:
1. Multiple entities need to sync (not just one data type)
2. Enrichment requires all entities to be synced first
3. `skip_enrichment=True` during parallel sync, enrichment runs in callback

---

## Task Dispatch Patterns

**File**: `src/service/integrations/initial_sync_service.py`

### Class Structure

```python
class InitialSyncService:
    """Service for triggering initial sync on newly authenticated integrations."""

    # Integration type constants
    CRM_INTEGRATION_TYPES = {IntegrationType.HUBSPOT, IntegrationType.SALESFORCE}
    CALENDAR_INTEGRATION_TYPES = {IntegrationType.GOOGLE_CALENDAR, IntegrationType.OUTLOOK_CALENDAR}
    EMAIL_INTEGRATION_TYPES = {IntegrationType.GMAIL, IntegrationType.OUTLOOK_EMAIL}
    MESSAGING_INTEGRATION_TYPES = {IntegrationType.TEAMS}

    def trigger_initial_sync(
        self,
        organisation_id: UUID,
        integration: KnowledgeIntegration,
    ) -> None:
        """Main dispatcher - routes to correct task based on integration type.

        Note: Synchronous because .delay() returns immediately.
        """
        integration_type = integration.integration_type
        if isinstance(integration_type, str):
            integration_type = IntegrationType(integration_type)

        org_id_str = str(organisation_id)
        int_id_str = str(integration.integration_id)

        # CRM - uses chord pattern
        if integration_type in self.CRM_INTEGRATION_TYPES:
            self._trigger_crm_sync(organisation_id, integration)
            return

        # Calendar
        if integration_type in self.CALENDAR_INTEGRATION_TYPES:
            self._trigger_calendar_sync(org_id_str, int_id_str)
            return

        # Email
        if integration_type in self.EMAIL_INTEGRATION_TYPES:
            self._trigger_email_sync(org_id_str, int_id_str)
            return

        # Messaging (Teams)
        if integration_type in self.MESSAGING_INTEGRATION_TYPES:
            self._trigger_messaging_sync(org_id_str, int_id_str)
            return

        # Unknown type
        logger.warning("No initial sync configured for integration type: %s", integration_type.value)

    def _trigger_calendar_sync(self, org_id_str: str, int_id_str: str) -> None:
        """Trigger calendar initial sync."""
        ...

    def _trigger_email_sync(self, org_id_str: str, int_id_str: str) -> None:
        """Trigger email initial sync."""
        ...

    def _trigger_messaging_sync(self, org_id_str: str, int_id_str: str) -> None:
        """Trigger messaging initial sync."""
        ...

    def _trigger_crm_sync(self, organisation_id: UUID, integration: KnowledgeIntegration) -> None:
        """Trigger CRM initial sync using Celery chord for parallel execution."""
        ...


# Singleton instance for easy import
initial_sync_service = InitialSyncService()
```

### Usage in Webhook Service

```python
from src.service.integrations.initial_sync_service import initial_sync_service

# In _reactivate_integration() or _handle_new_integration_auth()
# Note: No await needed - .delay() returns immediately
initial_sync_service.trigger_initial_sync(organisation_id, integration)
```

### CRM Integration (Chord Pattern)

CRM uses a different pattern with Celery chord for parallel sync + callback enrichment.
See `_trigger_crm_sync()` method in `InitialSyncService` class.

---

## Error Handling

### Webhook Should Not Fail on Sync Dispatch Error

The auth webhook handler should:
1. Always succeed in creating/reactivating the integration
2. Log errors if sync dispatch fails, but don't fail the webhook
3. User can manually trigger sync from frontend if initial sync fails

```python
try:
    await self._trigger_initial_sync(organisation_id, integration)
except Exception as e:
    # Log but don't fail the webhook
    logger.error(
        "Failed to trigger initial sync for integration %s: %s",
        integration.integration_id,
        str(e),
        exc_info=True,
    )
    # Webhook still succeeds - sync will happen on next schedule
```

### Task-Level Retries

Integration-level tasks have their own retry configuration:
```python
@app.task(
    name="integrations.sync_calendar_integration",
    bind=True,
    max_retries=3,
    default_retry_delay=60,  # 1 minute between retries
    ...
)
```

---

## Related Files

| Component | Path |
|-----------|------|
| Initial Sync Service | `src/service/integrations/initial_sync_service.py` |
| Auth Webhook Handler | `src/service/integrations/nango_webhook_service.py` |
| Calendar Integration Task | `src/workers/integrations_worker/tasks/calendar/integration_task.py` |
| Email Integration Task | `src/workers/integrations_worker/tasks/email/integration_task.py` |
| Messaging Integration Task | `src/workers/integrations_worker/tasks/messaging/channels_integration_task.py` |
| CRM Deals Integration Task | `src/workers/integrations_worker/tasks/crm/deals/integration_task.py` |
| CRM Deals Sync Helper | `src/workers/integrations_worker/tasks/crm/deals/sync_helper.py` |
| CRM Enrichment Task | `src/workers/integrations_worker/tasks/crm/enrichment_task.py` |

---

## Related Documentation

- [Scheduled Sync Architecture](./scheduled-sync-architecture.md) - Three-tier task architecture
- [Calendar Sync Flow](./calendar-sync-flow.md) - Calendar-specific sync details
- [Email Sync Flow](./email-sync-flow.md) - Email-specific sync details
- [Messaging Sync Flow](./messaging-sync-flow.md) - Messaging-specific sync details
- [CRM Sync Flow](./crm-sync-flow.md) - CRM-specific sync details
