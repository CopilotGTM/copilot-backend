# Incremental Deal Sync Implementation Plan

## Implementation Status: COMPLETE (with Bug Fixes)

| Phase | Description | Status | Date |
|-------|-------------|--------|------|
| Phase 1 | Sync metadata utilities & fetcher updates | ✅ Complete | 2025-01-06 |
| Phase 2 | DealSyncService.sync_incremental() & routing | ✅ Complete | 2025-01-06 |
| Phase 3 | Tests (64 tests passing) | ✅ Complete | 2025-01-06 |
| Bug Fixes | Critical bugs identified and fixed | ✅ Complete | 2026-01-06 |

---

## Critical Bug Fixes (2026-01-06)

### Bugs Identified During Robustness Review

| Bug | Severity | Description | Impact |
|-----|----------|-------------|--------|
| Classification Bug | **Critical** | `classify_records()` had no access to existing database IDs | All records classified as ADDED |
| Instance Overwrite Bug | **Critical** | Logic `sync_action == ADDED or is_new` caused existing records to be overwritten | Database constraint violations |
| Protection Bypass Bug | **Critical** | `deal_summary_data` protection was ineffective for incorrectly classified records | Data loss |
| Change Detection Bypass | **High** | Change detection only ran for UPDATED action | No optimization benefit |

### Root Cause

The implementation relied on `sync_action` from `_sync_metadata` to determine create vs update behavior. However:
- We fetch directly from CRM APIs, not Nango's Records API
- Without per-record action info from Nango, `classify_records()` couldn't accurately classify
- All non-archived records defaulted to `ADDED`

### Fix Applied

**Changed from sync_action-based routing to database-state-based routing:**

```python
# OLD (buggy):
if sync_action == SyncAction.ADDED or is_new:
    record = CRMDeal(...)  # Created new instance even for existing records!

# NEW (fixed):
if is_new:
    record = CRMDeal(...)  # Only create if truly new
    self._update_deal_record(...)  # No protection needed
else:
    if self._has_meaningful_changes(record, deal):
        self._update_deal_record_with_protection(...)  # Protection for existing
```

**Key Changes:**
1. Database state (`is_new`) is now the source of truth for create vs update
2. `sync_action` is only used for DELETED handling and logging/observability
3. Change detection runs for ALL existing records (not just UPDATED action)
4. Protection wrapper only applied to existing records (new records have no data to protect)
5. Archived/deleted records handled via direct field check (`archived` or `IsDeleted`)

## Executive Summary

This document outlines the implementation plan for optimizing deal synchronization from HubSpot and Salesforce CRMs. The current implementation overwrites all deal data during sync, wasting database operations and critically **overwriting AI-generated deal summaries**. The proposed solution leverages Nango's incremental sync metadata to process only changed records while preserving application-generated data.

---

## 1. Current Architecture Analysis

### 1.1 Data Flow Overview

```
Nango Webhook → NangoWebhookService → Celery Task → Dispatcher → DealSyncService → Database
```

**Key Files:**
| Component | Location |
|-----------|----------|
| Webhook Handler | `src/service/integrations/nango_webhook_service.py` |
| Sync Task | `src/workers/integrations_worker/tasks/sync_tasks.py` |
| Dispatcher | `src/workers/integrations_worker/services/dispatcher.py` |
| Deal Sync Service | `src/service/integrations/crm/entities/deal_sync_service.py` |
| Deal Adapter | `src/workers/integrations_worker/adapters/crm/entity_adapters/deal_adapter.py` |
| Deal Model | `src/domain/models/organisation_tables/sync/crm/deals.py` |
| HubSpot Fetcher | `src/workers/integrations_worker/providers/crm/hubspot/fetchers/deal_fetcher.py` |
| Salesforce Fetcher | `src/workers/integrations_worker/providers/crm/salesforce/fetchers/opportunity_fetcher.py` |

### 1.2 Current Sync Behavior

#### What Happens Today

1. **Webhook Reception** (`nango_webhook_service.py:142-265`)
   - Nango sends webhook with `responseResults: {added, updated, deleted}` counts
   - **Good:** Zero-delta check exists at line 183 - if `total_changes == 0` AND sync is incremental, webhook is skipped
   - **Problem:** If ANY changes exist (even 1 update), full processing is triggered

2. **Sync Task Execution** (`sync_tasks.py:84-251`)
   - Creates `SyncJobRequest` with `incremental` flag (True if not INITIAL sync)
   - Calls dispatcher's `execute_sync()`

3. **Data Fetching** (`dispatcher.py:1092-1216`)
   - Fetcher retrieves records from CRM via Nango proxy
   - HubSpot: Filters by `hs_lastmodifieddate >= last_sync_time`
   - Salesforce: SOQL `WHERE LastModifiedDate >= {timestamp}`
   - **Problem:** Returns ALL modified records since last sync, regardless of what actually changed

4. **Deal Processing** (`deal_sync_service.py:39-181`)
   ```python
   for deal in deal_list:
       record = existing.get(external_id)
       if not record:
           record = CRMDeal(...)  # New record

       # CRITICAL PROBLEM: Updates ALL fields unconditionally
       self._update_deal_record(record, deal, provider, pipeline_map, stage_map)

       record.synced_at = now
       record.last_seen_at = now
       self.db.add(record)
   ```

5. **Field Update** (`deal_sync_service.py:201-232`)
   - `_update_deal_record()` overwrites ALL fields from CRM data
   - **No preservation of `deal_summary_data`** - it's not touched, but since we use SQLAlchemy's `add()` on the existing record, it's preserved (unless the record is replaced)

### 1.3 Current Deal Summary Handling

**The Good News:**
Looking at `deal_sync_service.py`, the `_update_deal_record()` method does NOT explicitly overwrite `deal_summary_data`. The field is defined in the model (`deals.py:507-511`) but never touched during sync.

**The Actual Risk:**
The real issue is that if a deal is recreated (rare edge case) or if the upsert logic changes, the summary could be lost. Currently the existing record is loaded, modified, and saved - preserving `deal_summary_data`.

**However, the inefficiency remains:**
- Every modified deal gets all its fields updated
- Database writes happen for every record
- Downstream enrichment runs for unchanged deals

---

## 2. Problems Identified

### 2.1 Problem 1: Full Record Processing on Incremental Sync

**Current Flow:**
```
Webhook: {added: 0, updated: 1, deleted: 0} → Process ALL 500 modified deals
```

Even if only 1 deal was updated, the system:
1. Fetches all deals modified since `last_sync_time`
2. Updates all of them in the database
3. Runs enrichment on all of them
4. Triggers downstream processing

**Impact:**
- Wasted database I/O
- Unnecessary CPU cycles
- Increased latency
- Redundant downstream task triggers

### 2.2 Problem 2: No Nango Metadata Utilization

Nango provides per-record metadata that we're ignoring:

```json
{
  "id": "12345",
  "properties": {...},
  "_nango_metadata": {
    "last_action": "ADDED" | "UPDATED" | "DELETED",
    "first_seen_at": "2025-01-01T00:00:00Z",
    "last_modified_at": "2025-01-03T12:00:00Z",
    "deleted_at": null
  }
}
```

We should use `last_action` to:
- Skip unchanged records
- Handle deletions properly
- Track only modified records for downstream processing

### 2.3 Problem 3: Deal Summary Overwrite Risk

While currently safe, the architecture doesn't explicitly protect `deal_summary_data`:

```python
# CRMDeal model (deals.py:507-511)
deal_summary_data: dict | None = Field(
    default=None,
    sa_column=Column(JSONB, nullable=True),
    description="AI-generated deal summary data..."
)
```

**Risk Scenarios:**
1. Code changes that inadvertently clear the field
2. Migration that resets the column
3. Edge cases in upsert logic

---

## 3. Proposed Solution

### 3.1 Architecture Overview

```
                                    ┌─────────────────────────────────┐
                                    │  Nango Webhook                  │
                                    │  responseResults: {a:2,u:5,d:0} │
                                    └──────────────┬──────────────────┘
                                                   │
                                    ┌──────────────▼──────────────────┐
                                    │  NangoWebhookService            │
                                    │  Zero-delta check ✓ (existing)  │
                                    └──────────────┬──────────────────┘
                                                   │
                                    ┌──────────────▼──────────────────┐
                                    │  Sync Task                      │
                                    │  Pass responseResults counts    │
                                    └──────────────┬──────────────────┘
                                                   │
                                    ┌──────────────▼──────────────────┐
                                    │  Fetcher                        │
                                    │  Preserve _nango_metadata       │
                                    └──────────────┬──────────────────┘
                                                   │
                    ┌──────────────────────────────┼──────────────────────────────┐
                    │                              │                              │
         ┌──────────▼──────────┐       ┌──────────▼──────────┐       ┌──────────▼──────────┐
         │  ADDED Records      │       │  UPDATED Records    │       │  DELETED Records    │
         │  → Create new       │       │  → Update changed   │       │  → Soft delete      │
         │  → Full enrichment  │       │  → Skip if no delta │       │  → Mark is_deleted  │
         └─────────────────────┘       └─────────────────────┘       └─────────────────────┘
                    │                              │                              │
                    └──────────────────────────────┼──────────────────────────────┘
                                                   │
                                    ┌──────────────▼──────────────────┐
                                    │  Downstream Tasks               │
                                    │  Only for ADDED/UPDATED deals   │
                                    │  Preserve deal_summary_data     │
                                    └─────────────────────────────────┘
```

### 3.2 Implementation Phases

#### Phase 1: Preserve Nango Metadata in Fetchers ✅ COMPLETE

**Files Created:**
- `src/workers/integrations_worker/utils/sync_metadata.py` - New utility module with:
  - `SyncAction` enum (ADDED, UPDATED, DELETED, UNCHANGED)
  - `SyncActionCounts` dataclass for tracking action distribution
  - `SyncContext` dataclass for sync operation context
  - `determine_sync_action()` - Classifies records based on state
  - `classify_records()` - Adds `_sync_metadata` to all records
  - `log_sync_action_distribution()` - Logs action counts for observability

**Files Modified:**
- `src/workers/integrations_worker/models/sync_models.py` - Added `response_results` to `SyncJobRequest`
- `src/service/integrations/nango_webhook_service.py` - Passes `response_results` to task
- `src/workers/integrations_worker/tasks/sync_tasks.py` - Accepts and passes `response_results`
- `src/workers/integrations_worker/services/dispatcher.py` - Passes sync context to deal fetchers
- `src/workers/integrations_worker/providers/crm/hubspot/__init__.py` - Adds `_sync_metadata` to records
- `src/workers/integrations_worker/providers/crm/salesforce/__init__.py` - Adds `_sync_metadata` to records

#### Phase 2: Add Incremental Sync Method to DealSyncService ✅ COMPLETE

**File:** `src/service/integrations/crm/entities/deal_sync_service.py`

**Methods Added:**
- `sync_incremental()` (~250 lines) - Routes records by `_sync_metadata.sync_action`:
  - ADDED → Creates new deal records
  - UPDATED → Updates existing records only if `_has_meaningful_changes()` returns True
  - DELETED → Soft-deletes records (is_deleted=True)
  - Returns detailed action breakdown in results including `skipped` count
- `_has_meaningful_changes()` - Compares key fields (name, amount, stage, pipeline, owner, close_date, source_modified_at)
- `_update_deal_record_with_protection()` - Preserves `deal_summary_data` during CRM sync

**Files Modified for Routing:**
- `src/workers/integrations_worker/adapters/crm/entity_adapters/deal_adapter.py` - Added `sync_type` parameter, routes to `sync_incremental()` for INCREMENTAL
- `src/workers/integrations_worker/adapters/crm_adapter.py` - Added `sync_type` to `_sync_deals()` and `sync_crm_data()`
- `src/workers/integrations_worker/services/dispatcher.py` - Passes `sync_type` through `_handle_crm()` and `_dispatch_category()`

#### Phase 3: Tests ✅ COMPLETE

**Test Files Created:**
- `tests/workers/integrations_worker/utils/test_sync_metadata.py` (41 tests)
  - TestSyncAction, TestSyncActionCounts, TestSyncContext
  - TestDetermineSyncAction, TestAddSyncMetadata, TestClassifyRecords
  - TestExtractSyncAction, TestFilterRecordsByAction
  - TestLogSyncActionDistribution, TestIntegration

- `tests/service/integrations/crm/entities/test_deal_sync_service.py` (23 tests)
  - TestHasMeaningfulChanges (10 tests) - Change detection logic
  - TestUpdateDealRecordWithProtection (2 tests) - Summary preservation
  - TestSyncIncrementalRouting (3 tests) - Record routing
  - TestSyncIncrementalResults (2 tests) - Result structure
  - TestDealAdapterRouting (4 tests) - Sync type routing
  - TestIntegrationScenarios (2 tests) - End-to-end scenarios

**Test Results:** All 64 tests passing

---

## 4. Detailed Implementation Steps

### Step 1: Add Change Detection Helper

**File:** `src/service/integrations/crm/entities/deal_sync_service.py`

```python
def _has_meaningful_changes(
    self,
    existing: CRMDeal,
    incoming: dict[str, Any],
) -> bool:
    """Check if incoming data differs from existing record."""

    properties = incoming.get("properties", {})

    # Compare key fields that matter
    checks = [
        (existing.name, properties.get("dealname")),
        (existing.amount, self._safe_float(properties.get("amount"))),
        (existing.stage_external_id, properties.get("dealstage")),
        (existing.pipeline_external_id, properties.get("pipeline")),
        (existing.close_date, self._parse_date(properties.get("closedate"))),
        (existing.owner_external_id, properties.get("hubspot_owner_id")),
    ]

    for existing_val, incoming_val in checks:
        if existing_val != incoming_val:
            return True

    # Check source_modified_at as fallback
    incoming_modified = self._parse_date(incoming.get("updatedAt"))
    if incoming_modified and existing.source_modified_at:
        if incoming_modified > existing.source_modified_at:
            return True

    return False
```

### Step 2: Update Webhook Service to Pass Counts

**File:** `src/service/integrations/nango_webhook_service.py`

```python
# In _handle_sync_webhook, pass responseResults to task
send_integrations_task(
    task_name="integrations.sync_integration_data",
    kwargs={
        # ... existing kwargs ...
        "response_results": {  # NEW
            "added": added,
            "updated": updated,
            "deleted": deleted,
        },
    },
    queue="integrations",
    headers=correlation_headers(),
)
```

### Step 3: Add Metrics and Logging

```python
# In DealSyncService
logger.info(
    "Incremental sync completed: added=%d updated=%d deleted=%d skipped=%d",
    result.created,
    result.updated,
    result.deleted,
    result.skipped,
)
```

---

## 5. Deal Summary Protection Strategy

### 5.1 Current State Analysis

The `deal_summary_data` field in `CRMDeal`:

```python
# deals.py:507-511
deal_summary_data: dict | None = Field(
    default=None,
    sa_column=Column(JSONB, nullable=True),
    description="AI-generated deal summary data with schema_version, generated_at, and summary content.",
)
```

**Currently Safe Because:**
1. `_update_deal_record()` doesn't touch this field
2. SQLAlchemy preserves unmodified fields during updates
3. The field is only written by `DealSummaryService`

### 5.2 Recommended Safeguards

#### Option A: Explicit Field Preservation (Recommended)

```python
def _update_deal_record(self, record: CRMDeal, raw: dict, ...):
    # Preserve application-generated fields
    _preserved_fields = {
        'deal_summary_data': record.deal_summary_data,
        # Add other app-generated fields here if needed
    }

    # ... perform all CRM updates ...

    # Restore preserved fields
    for field, value in _preserved_fields.items():
        if value is not None:
            setattr(record, field, value)
```

#### Option B: Separate Update Methods

```python
def _update_crm_fields(self, record: CRMDeal, raw: dict, ...):
    """Update only CRM-sourced fields."""
    # Only updates fields that come from CRM

def _preserve_application_fields(self, record: CRMDeal):
    """Ensure application fields are not touched."""
    # Explicitly list fields that should NEVER be overwritten by CRM sync
    pass
```

#### Option C: Model-Level Protection

```python
# In CRMDeal model
class CRMDeal(SyncedRecordMixin, table=True):
    # Add a marker for protected fields
    _protected_from_sync = ['deal_summary_data']
```

---

## 6. Testing Strategy

### 6.1 Unit Tests

```python
class TestIncrementalDealSync:

    async def test_added_deal_creates_new_record(self):
        """Records with last_action=ADDED create new deals."""

    async def test_updated_deal_modifies_existing(self):
        """Records with last_action=UPDATED modify existing deals."""

    async def test_deleted_deal_soft_deletes(self):
        """Records with last_action=DELETED mark is_deleted=True."""

    async def test_unchanged_deal_skipped(self):
        """Deals with no meaningful changes are skipped."""

    async def test_deal_summary_preserved_on_update(self):
        """deal_summary_data is never overwritten by CRM sync."""
```

### 6.2 Integration Tests

```python
async def test_incremental_sync_end_to_end():
    """Full sync flow with mixed ADDED/UPDATED/DELETED records."""

    # Setup: Create existing deals with summaries
    # Execute: Run incremental sync with mixed actions
    # Verify:
    #   - New deals created
    #   - Updated deals modified
    #   - Deleted deals soft-deleted
    #   - Summaries preserved
    #   - Unchanged deals not written
```

### 6.3 Load Testing

```python
async def test_large_incremental_sync_performance():
    """Verify performance improvement with large datasets."""

    # Before: Full sync of 10,000 deals takes X seconds
    # After: Incremental sync with 100 changes takes Y seconds
    # Expected: Y << X (significant improvement)
```

---

## 7. Migration Plan

### 7.1 Rollout Strategy

1. **Phase 1: Logging Only**
   - Add logging to track `_nango_metadata.last_action`
   - Measure current sync performance
   - No behavior changes

2. **Phase 2: Feature Flag Rollout**
   - Add feature flag: `ENABLE_INCREMENTAL_DEAL_SYNC`
   - Enable for test organisations first
   - Monitor for regressions

3. **Phase 3: Full Rollout**
   - Enable for all organisations
   - Remove feature flag
   - Update documentation

### 7.2 Rollback Plan

```python
# Feature flag in deal_adapter.py
if settings.ENABLE_INCREMENTAL_DEAL_SYNC and sync_type == "INCREMENTAL":
    return await service.sync_incremental(...)
else:
    return await service.sync(...)  # Original behavior
```

---

## 8. Success Metrics

| Metric | Current | Target |
|--------|---------|--------|
| DB writes per incremental sync | 100% of modified records | Only changed records |
| Sync duration (1000 deals, 10 changes) | ~10s | ~1s |
| Downstream tasks triggered | All deals | Only changed deals |
| Deal summaries overwritten | 0 (but risky) | 0 (explicitly protected) |

---

## 9. Files to Modify Summary

| File | Changes |
|------|---------|
| `deal_sync_service.py` | Add `sync_incremental()`, change detection, summary protection |
| `deal_adapter.py` | Route to incremental sync based on sync_type |
| `nango_webhook_service.py` | Pass response_results to task |
| `sync_tasks.py` | Accept and pass response_results |
| `dispatcher.py` | Pass sync_type to adapter |
| `deal_fetcher.py` | Ensure _nango_metadata preserved |
| `opportunity_fetcher.py` | Ensure _nango_metadata preserved |

---

## 10. Appendix: Nango Metadata Reference

### Webhook Payload
```json
{
  "connectionId": "conn-123",
  "providerConfigKey": "hubspot",
  "syncName": "deals",
  "syncType": "INCREMENTAL",
  "responseResults": {
    "added": 2,
    "updated": 5,
    "deleted": 0
  }
}
```

### Record Metadata
```json
{
  "id": "12345",
  "properties": {...},
  "_nango_metadata": {
    "last_action": "UPDATED",
    "first_seen_at": "2025-01-01T00:00:00Z",
    "last_modified_at": "2025-01-03T12:00:00Z",
    "cursor": "abc123",
    "deleted_at": null
  }
}
```

### Last Action Values
| Value | Meaning |
|-------|---------|
| `ADDED` | New record created in CRM |
| `UPDATED` | Existing record modified |
| `DELETED` | Record deleted/archived in CRM |

---

## 11. Open Questions

1. **Nango Metadata Availability**: Need to verify that HubSpot and Salesforce Nango syncs include `_nango_metadata` in their responses. If not, we need to request this from Nango or implement alternative change detection.

2. **Partial Updates**: Should we implement field-level change detection to only update modified fields, or continue with full record updates?

3. **Sync History**: Should we track incremental sync metrics in `IntegrationSyncHistory` for observability?

4. **Downstream Task Optimization**: Current downstream tasks (deal matching, enrichment) process all deals. Should these also be optimized to only process changed deals?

---

## 12. Implementation Summary

### Files Created
| File | Purpose |
|------|---------|
| `src/workers/integrations_worker/utils/sync_metadata.py` | Sync action classification utilities |
| `src/workers/integrations_worker/utils/__init__.py` | Module exports |
| `tests/workers/integrations_worker/utils/test_sync_metadata.py` | 41 unit tests |
| `tests/service/integrations/crm/entities/test_deal_sync_service.py` | 23 unit tests |

### Files Modified
| File | Changes |
|------|---------|
| `src/service/integrations/crm/entities/deal_sync_service.py` | Added `sync_incremental()`, `_has_meaningful_changes()`, `_update_deal_record_with_protection()` |
| `src/workers/integrations_worker/adapters/crm/entity_adapters/deal_adapter.py` | Added `sync_type` routing |
| `src/workers/integrations_worker/adapters/crm_adapter.py` | Added `sync_type` parameter passthrough |
| `src/workers/integrations_worker/services/dispatcher.py` | Added sync context passing to CRM handlers |
| `src/workers/integrations_worker/models/sync_models.py` | Added `response_results` to SyncJobRequest |
| `src/service/integrations/nango_webhook_service.py` | Passes response_results to task |
| `src/workers/integrations_worker/tasks/sync_tasks.py` | Accepts and passes response_results |
| `src/workers/integrations_worker/providers/crm/hubspot/__init__.py` | Adds _sync_metadata to deal records |
| `src/workers/integrations_worker/providers/crm/salesforce/__init__.py` | Adds _sync_metadata to deal records |

### Data Flow (Complete)
```
Nango Webhook (sync_type=INCREMENTAL, responseResults={added,updated,deleted})
    ↓
NangoWebhookService → passes response_results to task
    ↓
sync_integration_data_task → creates SyncJobRequest with sync_type & response_results
    ↓
dispatcher._execute_category_sync → passes sync_type & response_results to fetcher
    ↓
HubSpot/Salesforce fetcher → adds _sync_metadata to each record
    ↓
_dispatch_category → passes sync_type to _handle_crm
    ↓
sync_crm_data → routes deals to incremental sync based on sync_type
    ↓
DealSyncService.sync_incremental() → routes records by _sync_metadata.sync_action:
    - ADDED → create new deal
    - UPDATED → update if _has_meaningful_changes() returns True
    - DELETED → soft delete (is_deleted=True)
    - No changes → skip (don't write to DB)
    ↓
deal_summary_data preserved via _update_deal_record_with_protection()
```

### Key Benefits Achieved
1. **Reduced DB writes** - Unchanged records are skipped entirely
2. **Protected deal_summary_data** - Explicitly preserved during updates
3. **Detailed metrics** - Action breakdown in sync results (added/updated/deleted/skipped)
4. **Backward compatible** - Full sync used for INITIAL or unknown sync types
5. **Comprehensive tests** - 64 unit tests covering all new functionality

---

*Document Version: 2.0*
*Created: 2025-01-06*
*Updated: 2025-01-06 - Implementation Complete*
*Author: Claude Code Assistant*
