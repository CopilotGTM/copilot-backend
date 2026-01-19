# Calendar Sync - Test Plan & Implementation Review

## Status: REVIEW COMPLETE

---

## Implementation Review

### 1. Pagination Status

| Provider | Max Per Request | Pagination Implemented | Issue |
|----------|-----------------|------------------------|-------|
| **Google Calendar** | 2500 | **NO** | Missing `nextPageToken` handling |
| **Outlook Calendar** | 999 | **NO** | Missing `@odata.nextLink` handling |

**Current Implementation:**

```python
# google_calendar.py - Line 51-53
params = {
    "maxResults": 2500,  # Max per request, but no pagination loop
    ...
}

# outlook_calendar.py - Line 57
params = {
    "$top": "999",  # Max per request, but no pagination loop
    ...
}
```

**Impact:**
- Users with >2500 Google Calendar events in the time window will have truncated sync
- Users with >999 Outlook Calendar events in the time window will have truncated sync
- For most users (90 days past to 365 days future), this is unlikely to be an issue
- **Recommendation**: Add pagination in a follow-up PR for completeness

**Pagination Fix (Future):**

```python
# Google Calendar with pagination
async def fetch_events_with_pagination(...):
    all_items = []
    page_token = None

    while True:
        params = {...}
        if page_token:
            params["pageToken"] = page_token

        resp = await nango.proxy_request(...)
        items = resp.get("items", [])
        all_items.extend(items)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return all_items

# Outlook Calendar with pagination
async def fetch_events_with_pagination(...):
    all_items = []
    next_link = None
    endpoint = "/v1.0/me/calendar/events"

    while True:
        if next_link:
            # Use the full next link URL
            resp = await nango.proxy_request(..., endpoint=next_link)
        else:
            resp = await nango.proxy_request(..., endpoint=endpoint, params=params)

        items = resp.get("value", [])
        all_items.extend(items)

        next_link = resp.get("@odata.nextLink")
        if not next_link:
            break

    return all_items
```

---

### 2. Incremental Sync Logic - VERIFIED

The incremental sync implementation in `CalendarSyncService.sync_calendar_events_incremental()` is **correctly implemented**:

| Feature | Status | Implementation |
|---------|--------|----------------|
| Load existing events for comparison | PASS | `_load_existing_events_by_external_ids()` |
| Skip unchanged events | PASS | `_has_meaningful_event_changes()` returns False → skip |
| Create new events | PASS | `is_new` check → `_upsert_event()` |
| Update changed events | PASS | Change detected → `_upsert_event()` |
| Handle cancelled events | PASS | `status == "cancelled"` → mark as cancelled |
| Handle deleted events | PASS | `sync_action == SyncAction.DELETED` → mark as cancelled |
| Batch commits | PASS | Commits every 100 records |
| Track sync history | PASS | `_create_sync_history()` / `_complete_sync_history()` |
| Update last_synced_at | PASS | Updates `integration.last_synced_at` |

**Change Detection Logic (`_has_meaningful_event_changes`):**

Compares these fields:
1. `source_modified_at` (primary indicator - if newer, always update)
2. `title`, `description`, `location`, `status`
3. `conference_link`, `organiser_email`
4. `start_at`, `end_at` (datetime comparison)
5. `attendee_count`, `is_all_day`

**Time Window Logic:**

| Sync Type | Past Window | Future Window | Buffer |
|-----------|-------------|---------------|--------|
| Initial | 90 days | 365 days | None |
| Incremental | last_synced_at - 1 day | 365 days | 1 day backoff |

The 1-day backoff ensures late-arriving updates are captured.

---

### 3. Scheduled Task Logic - VERIFIED

The `sync_calendar_events_task` correctly:
1. Queries all approved organizations (when no args)
2. Queries active `GOOGLE_CALENDAR` and `OUTLOOK_CALENDAR` integrations
3. Uses `sync_integration_data_task.apply_async()` for actual sync
4. Handles per-org errors without stopping entire job
5. Returns structured results with counts

---

## Test Plan

### Unit Tests

**File:** `tests/workers/integrations_worker/tasks/test_calendar_sync_tasks.py`

#### 1. Task Parameter Tests

```python
class TestCalendarSyncTaskParameters:
    """Test task parameter handling."""

    def test_sync_all_orgs_when_no_params(self):
        """When no org_id provided, should sync all approved orgs."""

    def test_sync_specific_org_when_org_id_provided(self):
        """When org_id provided, should sync only that org."""

    def test_sync_specific_integration_when_both_ids_provided(self):
        """When org_id and integration_id provided, should sync only that integration."""

    def test_handles_invalid_org_id_gracefully(self):
        """Invalid org_id should return error, not crash."""

    def test_handles_invalid_integration_id_gracefully(self):
        """Invalid integration_id should return error, not crash."""
```

#### 2. Organization Query Tests

```python
class TestOrganizationQuery:
    """Test organization filtering."""

    def test_only_approved_orgs_are_synced(self):
        """Should only query APPROVED organizations."""

    def test_pending_orgs_are_skipped(self):
        """PENDING organizations should not be synced."""

    def test_suspended_orgs_are_skipped(self):
        """SUSPENDED organizations should not be synced."""

    def test_empty_org_list_returns_success(self):
        """No approved orgs should return success with 0 processed."""
```

#### 3. Integration Query Tests

```python
class TestIntegrationQuery:
    """Test integration filtering."""

    def test_only_calendar_types_are_queried(self):
        """Should only query GOOGLE_CALENDAR and OUTLOOK_CALENDAR types."""

    def test_teams_integration_is_not_synced(self):
        """TEAMS integration type should not be included."""

    def test_inactive_integrations_are_skipped(self):
        """is_active=False integrations should not be synced."""

    def test_no_connection_id_integrations_are_skipped(self):
        """Integrations without connection_id should not be synced."""

    def test_google_calendar_is_synced(self):
        """GOOGLE_CALENDAR type should trigger sync."""

    def test_outlook_calendar_is_synced(self):
        """OUTLOOK_CALENDAR type should trigger sync."""

    def test_empty_integration_list_returns_success(self):
        """No calendar integrations should return success with 0 syncs."""
```

#### 4. Sync Task Triggering Tests

```python
class TestSyncTaskTriggering:
    """Test sync_integration_data_task.apply_async calls."""

    def test_correct_category_is_used(self):
        """Should use category='calendar'."""

    def test_correct_provider_for_google(self):
        """Google should use provider='google'."""

    def test_correct_provider_for_outlook(self):
        """Outlook should use provider='outlook'."""

    def test_correct_provider_config_key_for_google(self):
        """Google should use provider_config_key='google-calendar'."""

    def test_correct_provider_config_key_for_outlook(self):
        """Outlook should use provider_config_key='outlook-bflm'."""

    def test_sync_name_is_events(self):
        """Should use sync_name='events'."""

    def test_sync_type_is_incremental(self):
        """Should use sync_type='INCREMENTAL'."""

    def test_correlation_headers_are_passed(self):
        """correlation_headers() should be passed to apply_async."""
```

#### 5. Error Handling Tests

```python
class TestErrorHandling:
    """Test error handling at various levels."""

    def test_org_error_does_not_stop_other_orgs(self):
        """Error in one org should not stop processing other orgs."""

    def test_integration_error_does_not_stop_other_integrations(self):
        """Error in one integration should not stop other integrations."""

    def test_database_session_error_is_captured(self):
        """Database session errors should be logged and returned."""

    def test_apply_async_error_is_captured(self):
        """Celery apply_async errors should be logged and returned."""

    def test_error_details_included_in_result(self):
        """Error results should include error message and type."""
```

#### 6. Return Value Tests

```python
class TestReturnValue:
    """Test return value structure."""

    def test_return_includes_status(self):
        """Return should include 'status' field."""

    def test_return_includes_organizations_processed(self):
        """Return should include 'organizations_processed' count."""

    def test_return_includes_total_syncs_triggered(self):
        """Return should include 'total_syncs_triggered' count."""

    def test_return_includes_per_org_results(self):
        """Return should include per-org results array."""

    def test_per_org_result_includes_integration_details(self):
        """Per-org results should include integration_id and type."""

    def test_per_org_result_includes_task_id(self):
        """Per-org results should include Celery task_id."""
```

---

### Integration Tests

**File:** `tests/integration/test_calendar_sync_integration.py`

#### 1. End-to-End Sync Tests

```python
class TestCalendarSyncEndToEnd:
    """End-to-end integration tests."""

    async def test_full_sync_flow_google(self, test_org, google_calendar_integration):
        """Test complete sync flow for Google Calendar."""
        # 1. Trigger sync_calendar_events_task
        # 2. Verify sync_integration_data_task was called
        # 3. Verify events were stored in calendar_events
        # 4. Verify attendees were stored in calendar_event_attendees
        # 5. Verify integration_sync_history was created
        # 6. Verify integration.last_synced_at was updated

    async def test_full_sync_flow_outlook(self, test_org, outlook_calendar_integration):
        """Test complete sync flow for Outlook Calendar."""

    async def test_incremental_sync_skips_unchanged(self, test_org, integration, existing_events):
        """Incremental sync should skip unchanged events."""
        # 1. Create existing events in database
        # 2. Trigger sync with same events
        # 3. Verify skipped_count > 0
        # 4. Verify no unnecessary DB writes

    async def test_incremental_sync_updates_changed(self, test_org, integration, existing_events):
        """Incremental sync should update changed events."""
        # 1. Create existing events in database
        # 2. Trigger sync with modified event (different title)
        # 3. Verify updated_count > 0
        # 4. Verify title was updated in DB

    async def test_cancelled_events_are_marked(self, test_org, integration, existing_events):
        """Cancelled events should be marked as cancelled, not deleted."""
        # 1. Create existing event
        # 2. Trigger sync with status='cancelled'
        # 3. Verify event.status == 'cancelled'
        # 4. Verify event still exists in DB
```

#### 2. Sync History Tests

```python
class TestSyncHistory:
    """Test integration_sync_history tracking."""

    async def test_sync_history_created_on_start(self, test_org, integration):
        """Sync history record should be created at sync start."""

    async def test_sync_history_updated_on_success(self, test_org, integration):
        """Sync history should be updated with SUCCESS status."""

    async def test_sync_history_updated_on_failure(self, test_org, integration):
        """Sync history should be updated with FAILED status on error."""

    async def test_sync_history_includes_counts(self, test_org, integration):
        """Sync history should include records_processed and records_failed."""

    async def test_sync_history_includes_metadata(self, test_org, integration):
        """Sync history should include metadata with created/updated/skipped."""
```

#### 3. Idempotency Tests

```python
class TestIdempotency:
    """Test idempotent behavior."""

    async def test_duplicate_sync_does_not_duplicate_events(self, test_org, integration):
        """Running sync twice should not create duplicate events."""
        # 1. Trigger sync
        # 2. Count events
        # 3. Trigger sync again
        # 4. Verify event count unchanged

    async def test_concurrent_syncs_handled_by_lock(self, test_org, integration):
        """Concurrent syncs should be handled by Redis lock."""
        # 1. Trigger two syncs simultaneously
        # 2. Verify one was skipped (status='skipped', reason='locked')
```

---

### Provider Tests

**File:** `tests/workers/integrations_worker/providers/calendar/test_google_calendar.py`

```python
class TestGoogleCalendarFetcher:
    """Test Google Calendar fetcher."""

    async def test_initial_sync_time_window(self, mock_nango):
        """Initial sync should use 90 days past to 365 days future."""

    async def test_incremental_sync_time_window(self, mock_nango):
        """Incremental sync should use last_synced_at - 1 day."""

    async def test_extracts_items_from_response(self, mock_nango):
        """Should extract 'items' array from Google API response."""

    async def test_handles_empty_response(self, mock_nango):
        """Should return empty list for empty response."""

    async def test_uses_correct_endpoint(self, mock_nango):
        """Should use /calendar/v3/calendars/primary/events endpoint."""

    async def test_max_results_is_2500(self, mock_nango):
        """Should request maxResults=2500."""
```

**File:** `tests/workers/integrations_worker/providers/calendar/test_outlook_calendar.py`

```python
class TestOutlookCalendarFetcher:
    """Test Outlook Calendar fetcher."""

    async def test_initial_sync_time_window(self, mock_nango):
        """Initial sync should use 90 days past to 365 days future."""

    async def test_incremental_sync_time_window(self, mock_nango):
        """Incremental sync should use last_synced_at - 1 day."""

    async def test_extracts_value_from_response(self, mock_nango):
        """Should extract 'value' array from Microsoft Graph response."""

    async def test_handles_empty_response(self, mock_nango):
        """Should return empty list for empty response."""

    async def test_uses_correct_endpoint(self, mock_nango):
        """Should use /v1.0/me/calendar/events endpoint."""

    async def test_top_is_999(self, mock_nango):
        """Should request $top=999."""

    async def test_filter_format_is_correct(self, mock_nango):
        """Should use correct $filter format for date range."""
```

---

### Service Tests

**File:** `tests/service/integrations/test_calendar_sync_service.py`

```python
class TestCalendarSyncServiceIncremental:
    """Test CalendarSyncService incremental sync."""

    async def test_creates_sync_history_at_start(self, service, integration):
        """Should create sync history record at start."""

    async def test_completes_sync_history_on_success(self, service, integration):
        """Should complete sync history with SUCCESS status."""

    async def test_completes_sync_history_on_failure(self, service, integration):
        """Should complete sync history with FAILED status on error."""

    async def test_skips_unchanged_events(self, service, integration, existing_event):
        """Should skip events with no meaningful changes."""

    async def test_updates_changed_events(self, service, integration, existing_event):
        """Should update events with meaningful changes."""

    async def test_creates_new_events(self, service, integration):
        """Should create new events."""

    async def test_marks_cancelled_events(self, service, integration, existing_event):
        """Should mark cancelled events as cancelled."""

    async def test_batch_commits_every_100(self, service, integration, many_events):
        """Should commit every 100 records."""

    async def test_updates_integration_last_synced_at(self, service, integration):
        """Should update integration.last_synced_at on success."""


class TestMeaningfulEventChanges:
    """Test _has_meaningful_event_changes method."""

    def test_returns_true_when_title_changed(self, service, existing, incoming):
        """Should return True when title changed."""

    def test_returns_true_when_description_changed(self, service, existing, incoming):
        """Should return True when description changed."""

    def test_returns_true_when_location_changed(self, service, existing, incoming):
        """Should return True when location changed."""

    def test_returns_true_when_start_at_changed(self, service, existing, incoming):
        """Should return True when start_at changed."""

    def test_returns_true_when_end_at_changed(self, service, existing, incoming):
        """Should return True when end_at changed."""

    def test_returns_true_when_attendee_count_changed(self, service, existing, incoming):
        """Should return True when attendee_count changed."""

    def test_returns_true_when_source_modified_at_newer(self, service, existing, incoming):
        """Should return True when source_modified_at is newer."""

    def test_returns_false_when_no_changes(self, service, existing, incoming):
        """Should return False when no meaningful changes."""
```

---

## Test Fixtures

```python
# conftest.py

@pytest.fixture
def test_org(db_session):
    """Create a test organization."""
    org = Organisation(
        organisation_id=uuid4(),
        name="Test Org",
        status=OrganisationStatus.APPROVED,
    )
    db_session.add(org)
    db_session.commit()
    return org

@pytest.fixture
def google_calendar_integration(db_session, test_org):
    """Create a test Google Calendar integration."""
    integration = KnowledgeIntegration(
        integration_id=uuid4(),
        organisation_id=test_org.organisation_id,
        integration_type=IntegrationType.GOOGLE_CALENDAR,
        name="Test Google Calendar",
        connection_id="test-connection-id",
        is_active=True,
        credentials={},
        created_by_user_id=uuid4(),
    )
    db_session.add(integration)
    db_session.commit()
    return integration

@pytest.fixture
def outlook_calendar_integration(db_session, test_org):
    """Create a test Outlook Calendar integration."""
    integration = KnowledgeIntegration(
        integration_id=uuid4(),
        organisation_id=test_org.organisation_id,
        integration_type=IntegrationType.OUTLOOK_CALENDAR,
        name="Test Outlook Calendar",
        connection_id="test-connection-id",
        is_active=True,
        credentials={},
        created_by_user_id=uuid4(),
    )
    db_session.add(integration)
    db_session.commit()
    return integration

@pytest.fixture
def existing_calendar_event(db_session, test_org):
    """Create an existing calendar event."""
    event = CalendarEvent(
        event_id=uuid4(),
        organisation_id=test_org.organisation_id,
        external_id="test-external-id",
        title="Test Event",
        start_at=datetime.now(UTC),
        end_at=datetime.now(UTC) + timedelta(hours=1),
        synced_at=datetime.now(UTC),
    )
    db_session.add(event)
    db_session.commit()
    return event

@pytest.fixture
def mock_nango_client(mocker):
    """Mock NangoClient for provider tests."""
    mock = mocker.patch("src.service.integrations.nango_client.NangoClient")
    return mock
```

---

## Summary

### What's Working Well

1. **Incremental sync logic** - Correctly detects changes, skips unchanged, updates changed
2. **Change detection** - Comprehensive field comparison including timestamps
3. **Sync history tracking** - Properly creates and completes sync history records
4. **Scheduled task** - Correctly queries integrations and triggers syncs
5. **Error handling** - Per-org/per-integration errors don't stop entire job

### What Needs Improvement

1. **Pagination** - Neither provider implements pagination for large calendars
   - Google: Missing `nextPageToken` handling
   - Outlook: Missing `@odata.nextLink` handling
   - **Priority**: Medium (most users won't hit limits)
   - **Recommendation**: Add in follow-up PR

### Test Coverage Priorities

1. **High Priority**:
   - Task parameter handling
   - Integration filtering (correct types, active only)
   - Incremental sync logic (skip/update/create)
   - Error handling (per-org isolation)

2. **Medium Priority**:
   - Provider time window calculations
   - Change detection edge cases
   - Sync history tracking

3. **Lower Priority**:
   - Return value structure
   - Correlation headers
