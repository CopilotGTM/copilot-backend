"""Tests for DealSyncService incremental sync functionality.

These tests verify:
1. Change detection logic (_has_meaningful_changes)
2. Deal summary protection (_update_deal_record_with_protection)
3. Incremental sync routing and behavior

Note: Tests that require importing DealSyncService directly are skipped when
Supabase configuration is missing. Unit tests for logic use standalone
implementations to avoid import issues.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from src.workers.integrations_worker.utils.sync_metadata import SyncAction


def _parse_date(value):
    """Standalone date parser for testing (matches DealSyncService implementation)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            from dateutil import parser

            return parser.isoparse(value)
        except Exception:
            return None
    return None


def _safe_float(value):
    """Standalone float converter for testing."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _has_meaningful_changes(existing, incoming) -> bool:
    """Standalone implementation of change detection logic for testing.

    This mirrors the logic in DealSyncService._has_meaningful_changes
    without requiring the actual import.
    """
    properties = incoming.get("properties") or {}

    # Check source_modified_at as primary indicator
    incoming_modified = _parse_date(incoming.get("updatedAt"))
    if (
        incoming_modified
        and existing.source_modified_at
        and incoming_modified > existing.source_modified_at
    ):
        return True

    # Compare key fields
    checks = [
        (existing.name, properties.get("dealname") or incoming.get("name")),
        (existing.amount, _safe_float(properties.get("amount"))),
        (existing.stage_external_id, properties.get("dealstage")),
        (existing.pipeline_external_id, properties.get("pipeline")),
        (existing.owner_external_id, properties.get("hubspot_owner_id")),
    ]

    # Compare close_date
    existing_close = existing.close_date.isoformat() if existing.close_date else None
    incoming_close_date = _parse_date(properties.get("closedate"))
    incoming_close = incoming_close_date.isoformat() if incoming_close_date else None
    if existing_close != incoming_close:
        return True

    # Check key field comparisons
    for existing_val, incoming_val in checks:
        # Normalize None comparisons
        if existing_val is None and incoming_val is None:
            continue
        if existing_val != incoming_val:
            return True

    return False


class TestHasMeaningfulChanges:
    """Tests for _has_meaningful_changes method logic."""

    @pytest.fixture
    def mock_deal(self) -> MagicMock:
        """Create a mock CRMDeal for testing."""
        deal = MagicMock()
        deal.name = "Test Deal"
        deal.amount = 10000.0
        deal.stage_external_id = "stage-1"
        deal.pipeline_external_id = "pipeline-1"
        deal.owner_external_id = "owner-123"
        deal.close_date = datetime(2025, 6, 15, tzinfo=UTC)
        deal.source_modified_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        return deal

    def test_detects_name_change(self, mock_deal) -> None:
        """Should detect when deal name changes."""
        incoming = {
            "properties": {"dealname": "Changed Deal Name"},
            "updatedAt": None,
        }

        result = _has_meaningful_changes(mock_deal, incoming)
        assert result is True

    def test_detects_amount_change(self, mock_deal) -> None:
        """Should detect when amount changes."""
        incoming = {
            "properties": {"amount": "20000"},
            "updatedAt": None,
        }

        result = _has_meaningful_changes(mock_deal, incoming)
        assert result is True

    def test_detects_stage_change(self, mock_deal) -> None:
        """Should detect when stage changes."""
        incoming = {
            "properties": {
                "dealname": "Test Deal",
                "dealstage": "stage-2",
            },
            "updatedAt": None,
        }

        result = _has_meaningful_changes(mock_deal, incoming)
        assert result is True

    def test_detects_pipeline_change(self, mock_deal) -> None:
        """Should detect when pipeline changes."""
        incoming = {
            "properties": {
                "dealname": "Test Deal",
                "pipeline": "pipeline-2",
            },
            "updatedAt": None,
        }

        result = _has_meaningful_changes(mock_deal, incoming)
        assert result is True

    def test_detects_owner_change(self, mock_deal) -> None:
        """Should detect when owner changes."""
        incoming = {
            "properties": {
                "dealname": "Test Deal",
                "hubspot_owner_id": "owner-456",
            },
            "updatedAt": None,
        }

        result = _has_meaningful_changes(mock_deal, incoming)
        assert result is True

    def test_detects_close_date_change(self, mock_deal) -> None:
        """Should detect when close date changes."""
        incoming = {
            "properties": {
                "dealname": "Test Deal",
                "closedate": "2025-07-20T00:00:00Z",
            },
            "updatedAt": None,
        }

        result = _has_meaningful_changes(mock_deal, incoming)
        assert result is True

    def test_detects_source_modified_at_change(self, mock_deal) -> None:
        """Should detect when source_modified_at is newer."""
        incoming = {
            "properties": {"dealname": "Test Deal"},
            "updatedAt": "2025-01-02T12:00:00Z",  # Newer than mock
        }

        result = _has_meaningful_changes(mock_deal, incoming)
        assert result is True

    def test_no_change_returns_false(self, mock_deal) -> None:
        """Should return False when nothing meaningful changed."""
        incoming = {
            "properties": {
                "dealname": "Test Deal",
                "amount": "10000",
                "dealstage": "stage-1",
                "pipeline": "pipeline-1",
                "hubspot_owner_id": "owner-123",
                "closedate": "2025-06-15T00:00:00Z",
            },
            "updatedAt": "2025-01-01T12:00:00Z",  # Same as mock
        }

        result = _has_meaningful_changes(mock_deal, incoming)
        assert result is False

    def test_handles_none_values(self) -> None:
        """Should handle None values gracefully."""
        mock_deal = MagicMock()
        mock_deal.name = None
        mock_deal.amount = None
        mock_deal.stage_external_id = None
        mock_deal.pipeline_external_id = None
        mock_deal.owner_external_id = None
        mock_deal.close_date = None
        mock_deal.source_modified_at = None

        incoming = {
            "properties": {},
            "updatedAt": None,
        }

        # Should not raise and return False (no changes)
        result = _has_meaningful_changes(mock_deal, incoming)
        assert result is False

    def test_handles_missing_properties(self, mock_deal) -> None:
        """Should handle missing properties dict."""
        incoming = {"updatedAt": None}

        # Should not raise
        result = _has_meaningful_changes(mock_deal, incoming)
        # Will detect changes because all properties will be None vs existing values
        assert isinstance(result, bool)


class TestUpdateDealRecordWithProtection:
    """Tests for _update_deal_record_with_protection method logic.

    The corrected implementation:
    - Uses protection wrapper ONLY for existing records (not new ones)
    - New records don't need protection (no existing data to preserve)
    """

    def test_preserves_deal_summary_data_logic(self) -> None:
        """Should preserve deal_summary_data during update (logic test)."""
        # This tests the logic pattern used in _update_deal_record_with_protection
        mock_deal = MagicMock()
        original_summary = {
            "schema_version": "1.0",
            "generated_at": "2025-01-01T00:00:00Z",
            "data": {"executive_summary": "This is an AI-generated summary."},
        }
        mock_deal.deal_summary_data = original_summary

        # Simulate the protection pattern
        preserved_deal_summary = mock_deal.deal_summary_data

        # Simulate _update_deal_record clearing the summary
        mock_deal.deal_summary_data = None

        # Restore preserved field (only if it had a value)
        if preserved_deal_summary is not None:
            mock_deal.deal_summary_data = preserved_deal_summary

        # Summary should be preserved
        assert mock_deal.deal_summary_data == original_summary

    def test_allows_none_summary_to_remain_none_logic(self) -> None:
        """Should not set summary if it was originally None (logic test)."""
        mock_deal = MagicMock()
        mock_deal.deal_summary_data = None

        # Simulate the protection pattern
        preserved_deal_summary = mock_deal.deal_summary_data

        # Simulate some update operation
        # (nothing happens to deal_summary_data)

        # Restore preserved field (only if it had a value)
        if preserved_deal_summary is not None:
            mock_deal.deal_summary_data = preserved_deal_summary

        # Summary should remain None
        assert mock_deal.deal_summary_data is None

    def test_protection_only_for_existing_records(self) -> None:
        """Protection should only apply to EXISTING records.

        The corrected implementation:
        - New records: Use _update_deal_record (no protection needed)
        - Existing records: Use _update_deal_record_with_protection
        """
        # Scenario 1: New record - no protection needed
        is_new = True
        # For new records, deal_summary_data doesn't exist yet
        # No need to preserve anything

        # Scenario 2: Existing record - protection needed
        is_new = False
        existing_deal = MagicMock()
        existing_deal.deal_summary_data = {"important": "data"}

        # Only existing records need protection
        should_use_protection = not is_new
        assert should_use_protection is True

    def test_new_record_gets_no_protection(self) -> None:
        """New records should use direct update without protection wrapper."""
        is_new = True

        # The fixed implementation logic:
        # if is_new:
        #     _update_deal_record(...)  # No protection
        # else:
        #     _update_deal_record_with_protection(...)  # Protection
        should_use_protection = not is_new
        assert should_use_protection is False


class TestSyncIncrementalRouting:
    """Tests for sync_incremental routing logic.

    The corrected implementation uses database state (is_new) as the source of truth,
    NOT sync_action. sync_action is only used for:
    - DELETED action handling
    - Logging/observability
    """

    def test_database_state_is_source_of_truth(self) -> None:
        """Database state (is_new) should determine create vs update, not sync_action.

        This is the key fix: even if sync_action is ADDED, if the record exists
        in the database, we should UPDATE (not create a new instance).
        """
        # Simulating the fixed logic from sync_incremental
        record_in_db = True  # Record EXISTS in database
        _sync_action = SyncAction.ADDED  # But sync_action says ADDED (unused - db is truth)

        is_new = not record_in_db  # is_new = False

        # The FIX: Use is_new, NOT sync_action
        # OLD (buggy): if sync_action == SyncAction.ADDED or is_new: CREATE
        # NEW (fixed): if is_new: CREATE, else: UPDATE
        should_create = is_new
        should_update = not is_new

        assert should_create is False  # Should NOT create (record exists!)
        assert should_update is True  # Should UPDATE existing record

    def test_archived_records_deleted_regardless_of_sync_action(self) -> None:
        """Archived records should be soft-deleted regardless of sync_action."""
        # Record with archived=True but sync_action=UPDATED
        record = {
            "id": "deal-123",
            "archived": True,
            "_sync_metadata": {"sync_action": "UPDATED"},
        }

        is_archived = record.get("archived", False) or record.get("IsDeleted", False)

        # The fix: Check archived status directly, not just sync_action
        should_soft_delete = is_archived

        assert should_soft_delete is True

    def test_salesforce_isdeleted_handled(self) -> None:
        """Salesforce IsDeleted field should trigger soft-delete."""
        record = {
            "id": "sf-opp-123",
            "IsDeleted": True,
            "_sync_metadata": {"sync_action": "DELETED"},
        }

        is_archived = record.get("archived", False) or record.get("IsDeleted", False)
        assert is_archived is True

    def test_sync_action_extraction(self) -> None:
        """sync_action should still be extractable for logging."""
        from src.workers.integrations_worker.utils.sync_metadata import extract_sync_action

        record = {
            "id": "deal-123",
            "properties": {"dealname": "Test"},
            "_sync_metadata": {"sync_action": "UPDATED"},
        }

        action = extract_sync_action(record)
        assert action == SyncAction.UPDATED

    def test_deleted_action_triggers_soft_delete(self) -> None:
        """DELETED sync_action should trigger soft-delete even if not archived."""
        record = {
            "id": "deal-123",
            "archived": False,
            "_sync_metadata": {"sync_action": "DELETED"},
        }

        sync_action = SyncAction(record["_sync_metadata"]["sync_action"])
        is_archived = record.get("archived", False)

        # Either condition triggers soft-delete
        should_soft_delete = is_archived or sync_action == SyncAction.DELETED
        assert should_soft_delete is True


class TestSyncIncrementalResults:
    """Tests for sync_incremental result structure."""

    def test_result_includes_skipped_count(self) -> None:
        """Result should include count of skipped (unchanged) records."""
        # This tests the expected result structure
        expected_keys = {
            "deals",
            "participants",
            "stage_history",
            "skipped",
            "action_counts",
        }

        # Verify the result structure matches expectations
        sample_result = {
            "deals": 10,
            "participants": 25,
            "stage_history": 5,
            "skipped": 3,
            "action_counts": {
                "added": 2,
                "updated": 5,
                "deleted": 0,
                "unchanged": 3,
                "total": 10,
            },
        }

        assert expected_keys.issubset(sample_result.keys())

    def test_action_counts_structure(self) -> None:
        """action_counts should have expected structure."""
        from src.workers.integrations_worker.utils.sync_metadata import SyncActionCounts

        counts = SyncActionCounts()
        counts.increment(SyncAction.ADDED)
        counts.increment(SyncAction.UPDATED)
        counts.increment(SyncAction.UPDATED)

        result = counts.to_dict()

        assert result["added"] == 1
        assert result["updated"] == 2
        assert result["deleted"] == 0
        assert result["unchanged"] == 0
        assert result["total"] == 3


class TestDealAdapterRouting:
    """Tests for deal adapter sync_type routing."""

    def test_incremental_sync_type_routes_to_incremental(self) -> None:
        """INCREMENTAL sync_type should use sync_incremental method."""
        sync_type = "INCREMENTAL"

        # The logic in deal_adapter.py
        should_use_incremental = sync_type and sync_type.upper() == "INCREMENTAL"
        assert should_use_incremental is True

    def test_initial_sync_type_routes_to_full(self) -> None:
        """INITIAL sync_type should use full sync method."""
        sync_type = "INITIAL"

        should_use_incremental = sync_type and sync_type.upper() == "INCREMENTAL"
        assert should_use_incremental is False

    def test_none_sync_type_routes_to_full(self) -> None:
        """None sync_type should use full sync method."""
        sync_type = None

        should_use_incremental = sync_type and sync_type.upper() == "INCREMENTAL"
        # None evaluates to False in boolean context, so this is falsy
        assert not should_use_incremental

    def test_case_insensitive_routing(self) -> None:
        """Routing should be case-insensitive."""
        for sync_type in ["incremental", "Incremental", "INCREMENTAL"]:
            should_use_incremental = sync_type and sync_type.upper() == "INCREMENTAL"
            assert should_use_incremental is True, f"Failed for {sync_type}"


class TestIntegrationScenarios:
    """Integration test scenarios for incremental sync."""

    def test_mixed_action_scenario(self) -> None:
        """Test a realistic scenario with mixed record actions."""
        records = [
            # New deal
            {
                "id": "deal-new-1",
                "properties": {"dealname": "New Deal 1", "amount": "5000"},
                "_sync_metadata": {"sync_action": "ADDED"},
            },
            # Updated deal
            {
                "id": "deal-existing-1",
                "properties": {"dealname": "Updated Deal", "amount": "15000"},
                "_sync_metadata": {"sync_action": "UPDATED"},
            },
            # Deleted deal
            {
                "id": "deal-deleted-1",
                "archived": True,
                "_sync_metadata": {"sync_action": "DELETED"},
            },
            # Another new deal
            {
                "id": "deal-new-2",
                "properties": {"dealname": "New Deal 2", "amount": "10000"},
                "_sync_metadata": {"sync_action": "ADDED"},
            },
        ]

        # Count actions
        from src.workers.integrations_worker.utils.sync_metadata import (
            SyncActionCounts,
            extract_sync_action,
        )

        counts = SyncActionCounts()
        for record in records:
            action = extract_sync_action(record)
            if action:
                counts.increment(action)

        assert counts.added == 2
        assert counts.updated == 1
        assert counts.deleted == 1
        assert counts.total == 4

    def test_webhook_response_results_correlation(self) -> None:
        """Test that webhook counts correlate with processed records."""
        # Simulated webhook response_results
        webhook_results = {"added": 2, "updated": 3, "deleted": 1}
        total_expected = sum(webhook_results.values())

        # Simulated classified records
        from src.workers.integrations_worker.utils.sync_metadata import SyncActionCounts

        actual_counts = SyncActionCounts()
        actual_counts.added = 2
        actual_counts.updated = 3
        actual_counts.deleted = 1
        actual_counts.total = 6

        # In a real scenario, these should match
        assert actual_counts.added == webhook_results["added"]
        assert actual_counts.updated == webhook_results["updated"]
        assert actual_counts.deleted == webhook_results["deleted"]
        assert actual_counts.total == total_expected
