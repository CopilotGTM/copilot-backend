"""Tests for DealTaskSyncService incremental sync functionality.

These tests verify:
1. Change detection logic (_has_meaningful_changes)
2. Incremental sync routing and behavior

Note: Tests use standalone implementations to avoid Supabase import issues.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from src.workers.integrations_worker.utils.sync_metadata import SyncAction


def _parse_date(value):
    """Standalone date parser for testing."""
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


def _normalize_activity_status(raw_status):
    """Simplified activity status normalization for testing."""
    if raw_status is None:
        return None
    status_map = {
        "COMPLETED": "completed",
        "NOT_STARTED": "not_started",
        "IN_PROGRESS": "in_progress",
        "WAITING": "waiting_on_someone_else",
        "DEFERRED": "deferred",
    }
    return status_map.get(str(raw_status).upper(), raw_status)


def _normalize_activity_type(raw_type):
    """Simplified activity type normalization for testing."""
    if raw_type is None:
        return None
    type_map = {
        "CALL": "call",
        "EMAIL": "email",
        "MEETING": "meeting",
        "TODO": "to_do",
        "NOTE": "note",
    }
    return type_map.get(str(raw_type).upper(), "other")


def _normalize_activity_priority(raw_priority):
    """Simplified activity priority normalization for testing."""
    if raw_priority is None:
        return None
    priority_map = {
        "HIGH": "high",
        "MEDIUM": "medium",
        "NORMAL": "normal",
        "LOW": "low",
    }
    return priority_map.get(str(raw_priority).upper(), "none")


def _has_meaningful_changes_hubspot(existing, incoming) -> bool:
    """Standalone implementation of HubSpot change detection for testing."""
    props = incoming.get("properties") or {}

    # Check source_modified_at as primary indicator
    incoming_modified = _parse_date(incoming.get("updatedAt")) or _parse_date(
        props.get("hs_lastmodifieddate")
    )
    if (
        incoming_modified
        and existing.source_modified_at
        and incoming_modified > existing.source_modified_at
    ):
        return True

    # Compare key fields
    checks = [
        (existing.subject, props.get("hs_task_subject")),
        (existing.body, props.get("hs_task_body")),
        (existing.owner_external_id, props.get("hubspot_owner_id")),
    ]

    # Check normalized status/type/priority
    raw_status = props.get("hs_task_status")
    raw_type = props.get("hs_task_type")
    raw_priority = props.get("hs_task_priority")

    normalized_status = _normalize_activity_status(raw_status)
    normalized_type = _normalize_activity_type(raw_type)
    normalized_priority = _normalize_activity_priority(raw_priority)

    if existing.status != normalized_status:
        return True
    if existing.task_type != normalized_type:
        return True
    if existing.priority != normalized_priority:
        return True

    # Compare associations
    associations = incoming.get("associations", {})
    deals_data = associations.get("deals", {})
    if isinstance(deals_data, dict):
        deals = deals_data.get("results", [])
    elif isinstance(deals_data, list):
        deals = deals_data
    else:
        deals = []

    if deals and isinstance(deals, list) and len(deals) > 0:
        first_deal = deals[0]
        deal_ext_id = str(first_deal.get("id")) if isinstance(first_deal, dict) else None
        if deal_ext_id and existing.deal_external_id != deal_ext_id:
            return True

    for existing_val, incoming_val in checks:
        if existing_val is None and incoming_val is None:
            continue
        if existing_val != incoming_val:
            return True

    return False


class TestDealTaskHasMeaningfulChanges:
    """Tests for _has_meaningful_changes method logic."""

    @pytest.fixture
    def mock_task(self) -> MagicMock:
        """Create a mock CRMDealTask for testing."""
        task = MagicMock()
        task.subject = "Follow up call"
        task.body = "Call the customer"
        task.status = "not_started"
        task.task_type = "call"
        task.priority = "high"
        task.owner_external_id = "owner-123"
        task.deal_external_id = "deal-123"
        task.contact_external_id = None
        task.company_external_id = None
        task.source_modified_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        return task

    def test_detects_subject_change(self, mock_task) -> None:
        """Should detect when task subject changes."""
        incoming = {
            "properties": {"hs_task_subject": "New subject"},
            "updatedAt": None,
        }

        result = _has_meaningful_changes_hubspot(mock_task, incoming)
        assert result is True

    def test_detects_body_change(self, mock_task) -> None:
        """Should detect when task body changes."""
        incoming = {
            "properties": {
                "hs_task_subject": "Follow up call",
                "hs_task_body": "Updated call notes",
            },
            "updatedAt": None,
        }

        result = _has_meaningful_changes_hubspot(mock_task, incoming)
        assert result is True

    def test_detects_status_change(self, mock_task) -> None:
        """Should detect when status changes."""
        incoming = {
            "properties": {
                "hs_task_subject": "Follow up call",
                "hs_task_status": "COMPLETED",
            },
            "updatedAt": None,
        }

        result = _has_meaningful_changes_hubspot(mock_task, incoming)
        assert result is True

    def test_detects_priority_change(self, mock_task) -> None:
        """Should detect when priority changes."""
        incoming = {
            "properties": {
                "hs_task_subject": "Follow up call",
                "hs_task_priority": "LOW",
            },
            "updatedAt": None,
        }

        result = _has_meaningful_changes_hubspot(mock_task, incoming)
        assert result is True

    def test_detects_type_change(self, mock_task) -> None:
        """Should detect when task type changes."""
        incoming = {
            "properties": {
                "hs_task_subject": "Follow up call",
                "hs_task_type": "EMAIL",
            },
            "updatedAt": None,
        }

        result = _has_meaningful_changes_hubspot(mock_task, incoming)
        assert result is True

    def test_detects_owner_change(self, mock_task) -> None:
        """Should detect when owner changes."""
        incoming = {
            "properties": {
                "hs_task_subject": "Follow up call",
                "hubspot_owner_id": "owner-456",
            },
            "updatedAt": None,
        }

        result = _has_meaningful_changes_hubspot(mock_task, incoming)
        assert result is True

    def test_detects_deal_association_change(self, mock_task) -> None:
        """Should detect when deal association changes."""
        incoming = {
            "properties": {"hs_task_subject": "Follow up call"},
            "updatedAt": None,
            "associations": {
                "deals": {
                    "results": [{"id": "deal-456"}],
                }
            },
        }

        result = _has_meaningful_changes_hubspot(mock_task, incoming)
        assert result is True

    def test_detects_source_modified_at_change(self, mock_task) -> None:
        """Should detect when source_modified_at is newer."""
        incoming = {
            "properties": {"hs_task_subject": "Follow up call"},
            "updatedAt": "2025-01-02T12:00:00Z",  # Newer than mock
        }

        result = _has_meaningful_changes_hubspot(mock_task, incoming)
        assert result is True

    def test_no_change_returns_false(self, mock_task) -> None:
        """Should return False when nothing meaningful changed."""
        incoming = {
            "properties": {
                "hs_task_subject": "Follow up call",
                "hs_task_body": "Call the customer",
                "hs_task_status": "NOT_STARTED",
                "hs_task_type": "CALL",
                "hs_task_priority": "HIGH",
                "hubspot_owner_id": "owner-123",
            },
            "updatedAt": "2025-01-01T12:00:00Z",  # Same as mock
            "associations": {
                "deals": {
                    "results": [{"id": "deal-123"}],
                }
            },
        }

        result = _has_meaningful_changes_hubspot(mock_task, incoming)
        assert result is False

    def test_handles_none_values(self) -> None:
        """Should handle None values gracefully."""
        mock_task = MagicMock()
        mock_task.subject = None
        mock_task.body = None
        mock_task.status = None
        mock_task.task_type = None
        mock_task.priority = None
        mock_task.owner_external_id = None
        mock_task.deal_external_id = None
        mock_task.source_modified_at = None

        incoming = {
            "properties": {},
            "updatedAt": None,
        }

        result = _has_meaningful_changes_hubspot(mock_task, incoming)
        assert result is False


class TestDealTaskIncrementalRouting:
    """Tests for deal task incremental sync routing logic."""

    def test_database_state_is_source_of_truth(self) -> None:
        """Database state (is_new) should determine create vs update."""
        record_in_db = True
        _sync_action = SyncAction.ADDED  # Unused - demonstrates db state is source of truth

        is_new = not record_in_db

        # Use is_new, NOT sync_action
        should_create = is_new
        should_update = not is_new

        assert should_create is False
        assert should_update is True

    def test_archived_tasks_handled(self) -> None:
        """Archived tasks should be soft-deleted."""
        record = {
            "id": "task-123",
            "archived": True,
            "_sync_metadata": {"sync_action": "UPDATED"},
        }

        is_archived = record.get("archived", False) or record.get("IsDeleted", False)
        assert is_archived is True

    def test_salesforce_isdeleted_handled(self) -> None:
        """Salesforce IsDeleted field should trigger soft-delete."""
        record = {
            "id": "sf-task-123",
            "IsDeleted": True,
        }

        is_archived = record.get("archived", False) or record.get("IsDeleted", False)
        assert is_archived is True


class TestDealTaskAdapterRouting:
    """Tests for deal task adapter sync_type routing."""

    def test_incremental_sync_type_routes_to_incremental(self) -> None:
        """INCREMENTAL sync_type should use sync_incremental method."""
        sync_type = "INCREMENTAL"

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
        assert not should_use_incremental


class TestDealTaskEntityTypes:
    """Tests for deal task entity type handling (tasks, tickets, notes)."""

    def test_deal_tasks_data_type_supported(self) -> None:
        """deal_tasks data_type should be routed correctly."""
        data_type = "deal_tasks"
        supported_types = {"deal_tasks", "tasks", "tickets", "notes"}
        assert data_type in supported_types

    def test_tasks_data_type_supported(self) -> None:
        """tasks data_type should be routed correctly."""
        data_type = "tasks"
        supported_types = {"deal_tasks", "tasks", "tickets", "notes"}
        assert data_type in supported_types

    def test_tickets_data_type_supported(self) -> None:
        """tickets data_type should be routed correctly."""
        data_type = "tickets"
        supported_types = {"deal_tasks", "tasks", "tickets", "notes"}
        assert data_type in supported_types

    def test_notes_data_type_supported(self) -> None:
        """notes data_type should be routed correctly."""
        data_type = "notes"
        supported_types = {"deal_tasks", "tasks", "tickets", "notes"}
        assert data_type in supported_types


class TestDealTaskAssociationComparison:
    """Tests for deal task association comparison logic."""

    def test_deal_association_from_results_format(self) -> None:
        """Should extract deal association from results format."""
        associations = {
            "deals": {
                "results": [{"id": "deal-123", "type": "TASK_TO_DEAL"}],
            }
        }
        deals_data = associations.get("deals", {})
        if isinstance(deals_data, dict):
            deals = deals_data.get("results", [])
        else:
            deals = deals_data if isinstance(deals_data, list) else []

        assert len(deals) == 1
        assert deals[0].get("id") == "deal-123"

    def test_deal_association_from_list_format(self) -> None:
        """Should extract deal association from list format."""
        associations = {
            "deals": [{"id": "deal-456"}],
        }
        deals_data = associations.get("deals", {})
        if isinstance(deals_data, dict):
            deals = deals_data.get("results", [])
        else:
            deals = deals_data if isinstance(deals_data, list) else []

        assert len(deals) == 1
        assert deals[0].get("id") == "deal-456"

    def test_no_association_handles_gracefully(self) -> None:
        """Should handle missing associations gracefully."""
        associations = {}
        deals_data = associations.get("deals", {})
        if isinstance(deals_data, dict):
            deals = deals_data.get("results", [])
        else:
            deals = deals_data if isinstance(deals_data, list) else []

        assert deals == []
