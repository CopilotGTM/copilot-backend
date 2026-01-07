"""Tests for sync_metadata utility module.

These tests verify the functionality of sync action classification,
metadata management, and record filtering for incremental sync optimization.
"""

from src.workers.integrations_worker.utils.sync_metadata import (
    SyncAction,
    SyncActionCounts,
    SyncContext,
    add_sync_metadata,
    classify_records,
    determine_sync_action,
    extract_sync_action,
    filter_records_by_action,
    log_sync_action_distribution,
)


class TestSyncAction:
    """Tests for SyncAction enum."""

    def test_sync_action_values(self) -> None:
        """Verify SyncAction enum has expected values."""
        assert SyncAction.ADDED.value == "ADDED"
        assert SyncAction.UPDATED.value == "UPDATED"
        assert SyncAction.DELETED.value == "DELETED"
        assert SyncAction.UNCHANGED.value == "UNCHANGED"

    def test_sync_action_is_string_enum(self) -> None:
        """SyncAction should be usable as a string."""
        assert str(SyncAction.ADDED) == "SyncAction.ADDED"
        assert SyncAction.ADDED == "ADDED"


class TestSyncActionCounts:
    """Tests for SyncActionCounts dataclass."""

    def test_initial_counts_are_zero(self) -> None:
        """New SyncActionCounts should have all zeros."""
        counts = SyncActionCounts()
        assert counts.added == 0
        assert counts.updated == 0
        assert counts.deleted == 0
        assert counts.unchanged == 0
        assert counts.total == 0

    def test_increment_added(self) -> None:
        """Incrementing ADDED should update added and total."""
        counts = SyncActionCounts()
        counts.increment(SyncAction.ADDED)
        assert counts.added == 1
        assert counts.total == 1
        assert counts.updated == 0

    def test_increment_updated(self) -> None:
        """Incrementing UPDATED should update updated and total."""
        counts = SyncActionCounts()
        counts.increment(SyncAction.UPDATED)
        assert counts.updated == 1
        assert counts.total == 1

    def test_increment_deleted(self) -> None:
        """Incrementing DELETED should update deleted and total."""
        counts = SyncActionCounts()
        counts.increment(SyncAction.DELETED)
        assert counts.deleted == 1
        assert counts.total == 1

    def test_increment_unchanged(self) -> None:
        """Incrementing UNCHANGED should update unchanged and total."""
        counts = SyncActionCounts()
        counts.increment(SyncAction.UNCHANGED)
        assert counts.unchanged == 1
        assert counts.total == 1

    def test_multiple_increments(self) -> None:
        """Multiple increments should accumulate correctly."""
        counts = SyncActionCounts()
        counts.increment(SyncAction.ADDED)
        counts.increment(SyncAction.ADDED)
        counts.increment(SyncAction.UPDATED)
        counts.increment(SyncAction.DELETED)

        assert counts.added == 2
        assert counts.updated == 1
        assert counts.deleted == 1
        assert counts.unchanged == 0
        assert counts.total == 4

    def test_to_dict(self) -> None:
        """to_dict should return a proper dictionary representation."""
        counts = SyncActionCounts()
        counts.increment(SyncAction.ADDED)
        counts.increment(SyncAction.UPDATED)

        result = counts.to_dict()

        assert result == {
            "added": 1,
            "updated": 1,
            "deleted": 0,
            "unchanged": 0,
            "total": 2,
        }


class TestSyncContext:
    """Tests for SyncContext dataclass."""

    def test_default_values(self) -> None:
        """SyncContext should have sensible defaults."""
        context = SyncContext()
        assert context.sync_type is None
        assert context.is_initial is False
        assert context.response_results == {}
        assert context.existing_external_ids == set()

    def test_from_params_with_initial_sync_type(self) -> None:
        """from_params should detect INITIAL sync type."""
        context = SyncContext.from_params(sync_type="INITIAL")
        assert context.sync_type == "INITIAL"
        assert context.is_initial is True

    def test_from_params_with_incremental_sync_type(self) -> None:
        """from_params should detect INCREMENTAL sync type."""
        context = SyncContext.from_params(sync_type="INCREMENTAL")
        assert context.sync_type == "INCREMENTAL"
        assert context.is_initial is False

    def test_from_params_case_insensitive(self) -> None:
        """from_params should handle case variations."""
        context = SyncContext.from_params(sync_type="initial")
        assert context.is_initial is True

    def test_from_params_with_response_results(self) -> None:
        """from_params should accept response_results."""
        results = {"added": 5, "updated": 10, "deleted": 2}
        context = SyncContext.from_params(response_results=results)
        assert context.response_results == results

    def test_from_params_explicit_is_initial(self) -> None:
        """from_params should respect explicit is_initial."""
        context = SyncContext.from_params(is_initial=True)
        assert context.is_initial is True


class TestDetermineSyncAction:
    """Tests for determine_sync_action function."""

    def test_archived_record_returns_deleted(self) -> None:
        """Record with archived=True should be DELETED."""
        record = {"id": "123", "archived": True}
        assert determine_sync_action(record) == SyncAction.DELETED

    def test_salesforce_deleted_record_returns_deleted(self) -> None:
        """Salesforce record with IsDeleted=True should be DELETED."""
        record = {"Id": "123", "IsDeleted": True}
        assert determine_sync_action(record) == SyncAction.DELETED

    def test_initial_sync_returns_added(self) -> None:
        """During initial sync, non-deleted records should be ADDED."""
        record = {"id": "123", "archived": False}
        context = SyncContext.from_params(sync_type="INITIAL")
        assert determine_sync_action(record, context=context) == SyncAction.ADDED

    def test_existing_record_returns_updated(self) -> None:
        """Record with existing external_id should be UPDATED."""
        record = {"id": "123"}
        existing_ids = {"123", "456"}
        assert determine_sync_action(record, existing_ids=existing_ids) == SyncAction.UPDATED

    def test_new_record_returns_added(self) -> None:
        """Record without existing external_id should be ADDED."""
        record = {"id": "789"}
        existing_ids = {"123", "456"}
        assert determine_sync_action(record, existing_ids=existing_ids) == SyncAction.ADDED

    def test_context_existing_ids_used(self) -> None:
        """Should use existing_external_ids from context."""
        record = {"id": "123"}
        context = SyncContext()
        context.existing_external_ids = {"123"}
        assert determine_sync_action(record, context=context) == SyncAction.UPDATED

    def test_no_context_defaults_to_added(self) -> None:
        """Without context or existing_ids, should default to ADDED."""
        record = {"id": "123"}
        assert determine_sync_action(record) == SyncAction.ADDED


class TestAddSyncMetadata:
    """Tests for add_sync_metadata function."""

    def test_adds_sync_metadata_field(self) -> None:
        """Should add _sync_metadata to record."""
        record = {"id": "123"}
        result = add_sync_metadata(record, SyncAction.UPDATED)

        assert "_sync_metadata" in result
        assert result["_sync_metadata"]["sync_action"] == "UPDATED"
        assert result["_sync_metadata"]["inferred"] is True

    def test_includes_sync_type(self) -> None:
        """Should include sync_type in metadata."""
        record = {"id": "123"}
        result = add_sync_metadata(record, SyncAction.ADDED, sync_type="INCREMENTAL")

        assert result["_sync_metadata"]["sync_type"] == "INCREMENTAL"

    def test_mutates_original_record(self) -> None:
        """Should mutate the original record (not create a copy)."""
        record = {"id": "123"}
        result = add_sync_metadata(record, SyncAction.ADDED)

        assert record is result
        assert "_sync_metadata" in record


class TestClassifyRecords:
    """Tests for classify_records function."""

    def test_classifies_multiple_records(self) -> None:
        """Should classify a list of records."""
        records = [
            {"id": "1"},
            {"id": "2", "archived": True},
            {"id": "3"},
        ]
        existing_ids = {"1"}

        processed, counts = classify_records(records, existing_ids=existing_ids)

        assert len(processed) == 3
        assert counts.updated == 1  # id=1 exists
        assert counts.deleted == 1  # id=2 archived
        assert counts.added == 1  # id=3 new
        assert counts.total == 3

    def test_adds_metadata_when_requested(self) -> None:
        """Should add _sync_metadata when add_metadata=True."""
        records = [{"id": "1"}]

        processed, _ = classify_records(records, add_metadata=True)

        assert "_sync_metadata" in processed[0]

    def test_skips_metadata_when_disabled(self) -> None:
        """Should not add _sync_metadata when add_metadata=False."""
        records = [{"id": "1"}]

        processed, _ = classify_records(records, add_metadata=False)

        assert "_sync_metadata" not in processed[0]

    def test_uses_context_for_classification(self) -> None:
        """Should use context for classification."""
        records = [{"id": "1"}, {"id": "2"}]
        context = SyncContext.from_params(sync_type="INITIAL")

        processed, counts = classify_records(records, context=context)

        # All records should be ADDED during initial sync
        assert counts.added == 2
        assert counts.updated == 0


class TestExtractSyncAction:
    """Tests for extract_sync_action function."""

    def test_extracts_from_sync_metadata(self) -> None:
        """Should extract action from _sync_metadata."""
        record = {"id": "123", "_sync_metadata": {"sync_action": "UPDATED"}}
        assert extract_sync_action(record) == SyncAction.UPDATED

    def test_extracts_from_nango_metadata(self) -> None:
        """Should extract action from _nango_metadata as fallback."""
        record = {"id": "123", "_nango_metadata": {"last_action": "ADDED"}}
        assert extract_sync_action(record) == SyncAction.ADDED

    def test_prefers_sync_metadata_over_nango(self) -> None:
        """Should prefer _sync_metadata over _nango_metadata."""
        record = {
            "id": "123",
            "_sync_metadata": {"sync_action": "UPDATED"},
            "_nango_metadata": {"last_action": "ADDED"},
        }
        assert extract_sync_action(record) == SyncAction.UPDATED

    def test_returns_none_for_missing_metadata(self) -> None:
        """Should return None if no metadata present."""
        record = {"id": "123"}
        assert extract_sync_action(record) is None

    def test_returns_none_for_invalid_action(self) -> None:
        """Should return None for invalid action value."""
        record = {"id": "123", "_sync_metadata": {"sync_action": "INVALID_ACTION"}}
        assert extract_sync_action(record) is None


class TestFilterRecordsByAction:
    """Tests for filter_records_by_action function."""

    def test_filters_by_single_action(self) -> None:
        """Should filter records by a single action."""
        records = [
            {"id": "1", "_sync_metadata": {"sync_action": "ADDED"}},
            {"id": "2", "_sync_metadata": {"sync_action": "UPDATED"}},
            {"id": "3", "_sync_metadata": {"sync_action": "DELETED"}},
        ]

        result = filter_records_by_action(records, {SyncAction.ADDED})

        assert len(result) == 1
        assert result[0]["id"] == "1"

    def test_filters_by_multiple_actions(self) -> None:
        """Should filter records by multiple actions."""
        records = [
            {"id": "1", "_sync_metadata": {"sync_action": "ADDED"}},
            {"id": "2", "_sync_metadata": {"sync_action": "UPDATED"}},
            {"id": "3", "_sync_metadata": {"sync_action": "DELETED"}},
        ]

        result = filter_records_by_action(records, {SyncAction.ADDED, SyncAction.UPDATED})

        assert len(result) == 2
        assert {r["id"] for r in result} == {"1", "2"}

    def test_excludes_records_without_metadata(self) -> None:
        """Should exclude records without sync metadata."""
        records = [
            {"id": "1", "_sync_metadata": {"sync_action": "ADDED"}},
            {"id": "2"},  # No metadata
        ]

        result = filter_records_by_action(records, {SyncAction.ADDED})

        assert len(result) == 1
        assert result[0]["id"] == "1"

    def test_empty_result_when_no_matches(self) -> None:
        """Should return empty list when no records match."""
        records = [
            {"id": "1", "_sync_metadata": {"sync_action": "UPDATED"}},
        ]

        result = filter_records_by_action(records, {SyncAction.DELETED})

        assert len(result) == 0


class TestLogSyncActionDistribution:
    """Tests for log_sync_action_distribution function."""

    def test_logs_without_error(self) -> None:
        """Should log without raising errors."""
        counts = SyncActionCounts()
        counts.increment(SyncAction.ADDED)
        counts.increment(SyncAction.UPDATED)

        # Should not raise
        log_sync_action_distribution(
            counts,
            entity_type="deals",
            provider="hubspot",
            sync_type="INCREMENTAL",
        )

    def test_logs_with_minimal_params(self) -> None:
        """Should work with minimal parameters."""
        counts = SyncActionCounts()

        # Should not raise
        log_sync_action_distribution(counts)


class TestIntegration:
    """Integration tests for sync_metadata utilities."""

    def test_full_classification_workflow(self) -> None:
        """Test complete workflow: classify -> filter -> extract."""
        # Simulate records from a CRM sync
        records = [
            {"id": "new-deal-1", "properties": {"dealname": "New Deal"}},
            {"id": "existing-deal-1", "properties": {"dealname": "Existing Deal"}},
            {"id": "deleted-deal-1", "archived": True},
        ]

        # Classify records
        context = SyncContext.from_params(sync_type="INCREMENTAL")
        context.existing_external_ids = {"existing-deal-1"}

        processed, counts = classify_records(records, context=context, add_metadata=True)

        # Verify counts
        assert counts.added == 1
        assert counts.updated == 1
        assert counts.deleted == 1
        assert counts.total == 3

        # Filter for records to update
        to_update = filter_records_by_action(processed, {SyncAction.UPDATED})
        assert len(to_update) == 1
        assert to_update[0]["id"] == "existing-deal-1"

        # Extract action from a record
        action = extract_sync_action(processed[0])
        assert action in {SyncAction.ADDED, SyncAction.UPDATED, SyncAction.DELETED}
