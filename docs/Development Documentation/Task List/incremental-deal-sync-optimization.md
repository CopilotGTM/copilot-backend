# Task: Incremental Deal Sync Optimization

## Status: TODO

## Problem Statement

Currently, when Nango triggers a sync webhook for deals (incremental sync), the system fetches and processes **all deals** modified since the last sync timestamp, then updates every single record in the database regardless of whether the data has actually changed. This causes:

1. **Unnecessary database writes**: Every deal record is updated even if no fields changed
2. **Wasted processing time**: Deal enrichment, participant processing, and stage history extraction runs for unchanged deals
3. **Downstream task triggers**: Deal updates may trigger unnecessary downstream processing (matching, summaries, etc.)
4. **Performance degradation at scale**: As deal count grows, sync times increase linearly even for minimal changes

### Current Behavior

1. Nango webhook arrives with `responseResults: { added: 2, updated: 5, deleted: 0 }`
2. System calls `HubSpotDealFetcher.fetch_all()` which fetches all deals modified after `last_sync_time`
3. `DealSyncService.sync()` iterates through ALL fetched records and updates each one
4. Every deal gets written to DB, enriched, and has participants/history processed

### What Nango Actually Provides

Nango includes metadata on each record that tells us exactly what changed:

```python
# Each record from Nango includes _nango_metadata:
{
    "id": "12345",
    "properties": {...},
    "_nango_metadata": {
        "last_action": "ADDED" | "UPDATED" | "DELETED",
        "first_seen_at": "2025-01-01T00:00:00Z",
        "last_modified_at": "2025-01-03T12:00:00Z",
        "cursor": "abc123",
        "deleted_at": null  # or timestamp if deleted
    }
}
```

Additionally, webhooks include `modifiedAfter` timestamp that can be used to fetch only modified records.

## Proposed Solution

### 1. Leverage Nango's Record-Level Metadata

Modify the sync flow to inspect `_nango_metadata.last_action` for each record and only process records that have actually changed:

```python
# In deal_sync_service.py or a new incremental processor
async def sync_incremental(
    self,
    *,
    integration: KnowledgeIntegration,
    records: Iterable[dict[str, Any]],
    provider: str,
) -> dict[str, Any]:
    """Sync only records that have actually changed."""

    for deal in records:
        nango_meta = deal.get("_nango_metadata", {})
        last_action = nango_meta.get("last_action")

        if last_action == "ADDED":
            # New deal - create record
            await self._create_deal(deal, ...)
        elif last_action == "UPDATED":
            # Modified deal - update only this record
            await self._update_deal(deal, ...)
        elif last_action == "DELETED":
            # Deleted deal - mark as deleted (soft delete)
            await self._mark_deal_deleted(deal.get("id"), ...)
        else:
            # No action needed - skip this record
            continue
```

### 2. Track Changed Deal IDs for Downstream Processing

Only trigger downstream tasks (enrichment, matching, summaries) for deals that actually changed:

```python
# Track which deals were modified
modified_deal_ids: list[UUID] = []
created_deal_ids: list[UUID] = []
deleted_deal_ids: list[UUID] = []

# After sync, only process modified deals
if modified_deal_ids or created_deal_ids:
    await self._enrich_deals(modified_deal_ids + created_deal_ids)
    # Trigger downstream tasks only for changed deals
```

### 3. Optimize Database Operations

Use conditional updates or skip unchanged records:

```python
# Option A: Check if record actually changed before updating
existing = await self._load_existing_by_external_ids(...)
for deal in records:
    external_id = deal.get("id")
    existing_record = existing.get(external_id)

    if existing_record and not self._has_changes(existing_record, deal):
        # No actual changes - skip
        continue

    # Process the change...

# Option B: Use database-level upsert with change detection
# Only update if source_modified_at has changed
```

### 4. Handle Webhook Counts Validation

Use Nango's `responseResults` to validate our processing:

```python
# In nango_webhook_service.py or sync_tasks.py
webhook_counts = {
    "added": response_results.get("added", 0),
    "updated": response_results.get("updated", 0),
    "deleted": response_results.get("deleted", 0),
}

# After processing, verify counts match
actual_counts = sync_result.get_counts()
if actual_counts != webhook_counts:
    logger.warning(
        "Sync count mismatch: expected=%s actual=%s",
        webhook_counts,
        actual_counts,
    )
```

## Implementation Steps

### Phase 1: Add Nango Metadata Parsing

1. Update fetchers to preserve `_nango_metadata` from Nango responses
2. Add helper methods to extract action type from records
3. Add logging to track action distribution (added/updated/deleted counts)

### Phase 2: Implement Incremental Processing in DealSyncService

1. Add `sync_incremental()` method alongside existing `sync()` method
2. Route to incremental sync when `sync_type == "INCREMENTAL"`
3. Skip processing for records with no changes
4. Track modified deal IDs for downstream processing

### Phase 3: Optimize Downstream Tasks

1. Modify `DealEnrichmentService` to accept list of deal IDs to enrich
2. Update deal worker tasks to process only changed deals
3. Add metrics to track processing efficiency

### Phase 4: Extend to Other Entities

1. Apply same pattern to contacts, companies, owners, users
2. Create shared `IncrementalSyncMixin` or base class
3. Update all CRM sync services

## Related Files

### Primary Files to Modify

- `src/service/integrations/crm/entities/deal_sync_service.py` - Add incremental sync logic
- `src/workers/integrations_worker/providers/crm/hubspot/fetchers/deal_fetcher.py` - Preserve Nango metadata
- `src/workers/integrations_worker/services/dispatcher.py` - Route to incremental sync
- `src/service/integrations/nango_webhook_service.py` - Pass webhook counts to sync task

### Secondary Files (Downstream Processing)

- `src/service/integrations/crm/entities/deal_enrichment_service.py` - Accept deal ID list
- `src/workers/deal_worker/tasks/deal_tasks.py` - Process only changed deals
- `src/workers/integrations_worker/tasks/sync_tasks.py` - Pass incremental flag

### Reference Files (Pattern Extension)

- `src/service/integrations/crm/entities/contact_sync_service.py`
- `src/service/integrations/crm/entities/company_sync_service.py`
- `src/service/integrations/core/base_sync_service.py`

## Benefits

1. **Reduced database load**: Only write records that actually changed
2. **Faster sync times**: Skip unchanged records entirely
3. **Efficient downstream processing**: Only trigger tasks for modified deals
4. **Better observability**: Clear metrics on what actually changed vs what was processed
5. **Scalability**: Sync performance no longer degrades linearly with total deal count

## Success Criteria

- [ ] Incremental syncs process only records with `last_action` in (ADDED, UPDATED, DELETED)
- [ ] Database writes reduced by 80%+ for typical incremental syncs
- [ ] Downstream tasks only triggered for actually changed deals
- [ ] Sync counts match Nango webhook `responseResults`
- [ ] No regression in full (INITIAL) sync behavior

## Testing Considerations

1. Test with INITIAL sync to ensure full sync still works
2. Test with INCREMENTAL sync containing mixed actions (add/update/delete)
3. Verify deleted deals are properly soft-deleted
4. Confirm downstream tasks only process changed deals
5. Load test with large deal counts to measure performance improvement

## References

- [Nango Sync Documentation](https://docs.nango.dev/guides/syncs/use-a-sync)
- [Nango Incremental Sync Guide](https://docs.nango.dev/guides/syncs/overview)
- [Nango Record Metadata](https://docs.nango.dev/customize/guides/advanced/handle-large-datasets)

---

Once task is completed, move to review folder.
