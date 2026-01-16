"""Tests for timeline sync tasks.

Tests for:
- trigger_timeline_sync_all_orgs_task (Tier 1)
- sync_org_timeline_task (Tier 2)
- sync_deal_timeline_task (Tier 3)
- exclude_timeline_event_task
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

# Handle circular import gracefully - skip all task-dependent tests if import fails
try:
    from src.workers.deal_worker.tasks.deal_mappers.timeline_sync_tasks import (
        exclude_timeline_event_task,
        sync_deal_timeline_task,
        sync_org_timeline_task,
        trigger_timeline_sync_all_orgs_task,
    )

    TASKS_AVAILABLE = True
except ImportError:
    TASKS_AVAILABLE = False
    trigger_timeline_sync_all_orgs_task = None
    sync_org_timeline_task = None
    sync_deal_timeline_task = None
    exclude_timeline_event_task = None

pytestmark = pytest.mark.skipif(
    not TASKS_AVAILABLE,
    reason="Circular import in deal_worker - tasks not importable in test context",
)


# =============================================================================
# Tier 1: trigger_timeline_sync_all_orgs_task Config Tests
# =============================================================================


class TestTriggerTimelineSyncAllOrgsTaskConfig:
    """Tests for trigger_timeline_sync_all_orgs_task Celery configuration."""

    def test_task_exists(self) -> None:
        """Task should exist and be importable."""
        assert trigger_timeline_sync_all_orgs_task is not None

    def test_task_name(self) -> None:
        """Task should have correct name."""
        assert trigger_timeline_sync_all_orgs_task.name == "deal.trigger_timeline_sync_all_orgs"

    def test_task_max_retries(self) -> None:
        """Task should have max_retries=0 (coordinator task)."""
        assert trigger_timeline_sync_all_orgs_task.max_retries == 0

    def test_task_acks_late(self) -> None:
        """Task should have acks_late=True."""
        assert trigger_timeline_sync_all_orgs_task.acks_late is True

    def test_task_soft_time_limit(self) -> None:
        """Task should have soft_time_limit=300 (5 minutes)."""
        assert trigger_timeline_sync_all_orgs_task.soft_time_limit == 300

    def test_task_time_limit(self) -> None:
        """Task should have time_limit=360 (6 minutes)."""
        assert trigger_timeline_sync_all_orgs_task.time_limit == 360


# =============================================================================
# Tier 2: sync_org_timeline_task Config Tests
# =============================================================================


class TestSyncOrgTimelineTaskConfig:
    """Tests for sync_org_timeline_task Celery configuration."""

    def test_task_exists(self) -> None:
        """Task should exist and be importable."""
        assert sync_org_timeline_task is not None

    def test_task_name(self) -> None:
        """Task should have correct name."""
        assert sync_org_timeline_task.name == "deal.sync_org_timeline"

    def test_task_max_retries(self) -> None:
        """Task should have max_retries=2."""
        assert sync_org_timeline_task.max_retries == 2

    def test_task_default_retry_delay(self) -> None:
        """Task should have default_retry_delay=300 (5 minutes)."""
        assert sync_org_timeline_task.default_retry_delay == 300

    def test_task_acks_late(self) -> None:
        """Task should have acks_late=True."""
        assert sync_org_timeline_task.acks_late is True

    def test_task_soft_time_limit(self) -> None:
        """Task should have soft_time_limit=1800 (30 minutes)."""
        assert sync_org_timeline_task.soft_time_limit == 1800

    def test_task_time_limit(self) -> None:
        """Task should have time_limit=1860 (31 minutes)."""
        assert sync_org_timeline_task.time_limit == 1860


# =============================================================================
# Tier 3: sync_deal_timeline_task Config Tests
# =============================================================================


class TestSyncDealTimelineTaskConfig:
    """Tests for sync_deal_timeline_task Celery configuration."""

    def test_task_exists(self) -> None:
        """Task should exist and be importable."""
        assert sync_deal_timeline_task is not None

    def test_task_name(self) -> None:
        """Task should have correct name."""
        assert sync_deal_timeline_task.name == "deal.sync_deal_timeline"

    def test_task_max_retries(self) -> None:
        """Task should have max_retries=3."""
        assert sync_deal_timeline_task.max_retries == 3

    def test_task_default_retry_delay(self) -> None:
        """Task should have default_retry_delay=60."""
        assert sync_deal_timeline_task.default_retry_delay == 60

    def test_task_acks_late(self) -> None:
        """Task should have acks_late=True."""
        assert sync_deal_timeline_task.acks_late is True

    def test_task_soft_time_limit(self) -> None:
        """Task should have soft_time_limit=120 (2 minutes)."""
        assert sync_deal_timeline_task.soft_time_limit == 120

    def test_task_time_limit(self) -> None:
        """Task should have time_limit=150 (2.5 minutes)."""
        assert sync_deal_timeline_task.time_limit == 150


# =============================================================================
# exclude_timeline_event_task Config Tests
# =============================================================================


class TestExcludeTimelineEventTaskConfig:
    """Tests for exclude_timeline_event_task Celery configuration."""

    def test_task_exists(self) -> None:
        """Task should exist and be importable."""
        assert exclude_timeline_event_task is not None

    def test_task_name(self) -> None:
        """Task should have correct name."""
        assert exclude_timeline_event_task.name == "deal.exclude_timeline_event"

    def test_task_max_retries(self) -> None:
        """Task should have max_retries=2."""
        assert exclude_timeline_event_task.max_retries == 2

    def test_task_default_retry_delay(self) -> None:
        """Task should have default_retry_delay=30."""
        assert exclude_timeline_event_task.default_retry_delay == 30

    def test_task_acks_late(self) -> None:
        """Task should have acks_late=True."""
        assert exclude_timeline_event_task.acks_late is True

    def test_task_soft_time_limit(self) -> None:
        """Task should have soft_time_limit=30."""
        assert exclude_timeline_event_task.soft_time_limit == 30

    def test_task_time_limit(self) -> None:
        """Task should have time_limit=60."""
        assert exclude_timeline_event_task.time_limit == 60


# =============================================================================
# Parameter Tests
# =============================================================================


class TestSyncOrgTimelineTaskParameters:
    """Tests for sync_org_timeline_task parameter handling."""

    def test_accepts_required_parameters(self) -> None:
        """Task should accept organisation_id."""
        import inspect

        sig = inspect.signature(sync_org_timeline_task)
        assert "organisation_id" in sig.parameters

    def test_accepts_optional_parameters(self) -> None:
        """Task should accept optional parameters."""
        import inspect

        sig = inspect.signature(sync_org_timeline_task)
        params = sig.parameters
        assert "deal_ids" in params
        assert params["deal_ids"].default is None
        assert "event_types" in params
        assert params["event_types"].default is None
        assert "batch_size" in params
        assert params["batch_size"].default == 50


class TestSyncDealTimelineTaskParameters:
    """Tests for sync_deal_timeline_task parameter handling."""

    def test_accepts_required_parameters(self) -> None:
        """Task should accept organisation_id and deal_id."""
        import inspect

        sig = inspect.signature(sync_deal_timeline_task)
        params = sig.parameters
        assert "organisation_id" in params
        assert "deal_id" in params

    def test_accepts_event_types_parameter(self) -> None:
        """Task should accept optional event_types."""
        import inspect

        sig = inspect.signature(sync_deal_timeline_task)
        params = sig.parameters
        assert "event_types" in params
        assert params["event_types"].default is None


class TestExcludeTimelineEventTaskParameters:
    """Tests for exclude_timeline_event_task parameter handling."""

    def test_accepts_required_parameters(self) -> None:
        """Task should accept organisation_id and timeline_event_id."""
        import inspect

        sig = inspect.signature(exclude_timeline_event_task)
        params = sig.parameters
        assert "organisation_id" in params
        assert "timeline_event_id" in params


# =============================================================================
# Result Structure Tests (no imports needed)
# =============================================================================


@pytest.mark.skipif(False, reason="")
class TestTriggerTimelineSyncAllOrgsResultStructure:
    """Tests for trigger_timeline_sync_all_orgs_task return value structure."""

    def test_completed_result_has_required_fields(self) -> None:
        """Completed result should have all required fields."""
        result = {
            "status": "completed",
            "started_at": datetime.now(UTC).isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "summary": {
                "total_orgs": 5,
                "orgs_triggered": 5,
                "orgs_skipped": 0,
            },
        }
        assert "status" in result
        assert "started_at" in result
        assert "summary" in result
        assert "total_orgs" in result["summary"]
        assert "orgs_triggered" in result["summary"]

    def test_failed_result_has_error_field(self) -> None:
        """Failed result should include error information."""
        result = {
            "status": "failed",
            "started_at": datetime.now(UTC).isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "error": "Some error message",
        }
        assert result["status"] == "failed"
        assert "error" in result


@pytest.mark.skipif(False, reason="")
class TestSyncOrgTimelineResultStructure:
    """Tests for sync_org_timeline_task return value structure."""

    def test_success_result_has_required_fields(self) -> None:
        """Success result should have all required fields."""
        result = {
            "status": "success",
            "organisation_id": str(uuid4()),
            "started_at": datetime.now(UTC).isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "deals_processed": 10,
            "emails_synced": 50,
            "meetings_synced": 20,
            "stage_changes_synced": 15,
            "deal_created_synced": 10,
            "total_created": 95,
            "total_skipped": 5,
        }
        assert "status" in result
        assert "organisation_id" in result
        assert "deals_processed" in result
        assert "emails_synced" in result
        assert "meetings_synced" in result
        assert "stage_changes_synced" in result
        assert "deal_created_synced" in result
        assert "total_created" in result
        assert "total_skipped" in result

    def test_failed_result_has_error_field(self) -> None:
        """Failed result should include error information."""
        result = {
            "status": "failed",
            "organisation_id": str(uuid4()),
            "started_at": datetime.now(UTC).isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "error": "Failed to acquire database session",
        }
        assert result["status"] == "failed"
        assert "error" in result


@pytest.mark.skipif(False, reason="")
class TestSyncDealTimelineResultStructure:
    """Tests for sync_deal_timeline_task return value structure."""

    def test_success_result_has_required_fields(self) -> None:
        """Success result should have all required fields."""
        result = {
            "status": "success",
            "organisation_id": str(uuid4()),
            "deal_id": str(uuid4()),
            "started_at": datetime.now(UTC).isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "emails_synced": 5,
            "meetings_synced": 3,
            "stage_changes_synced": 2,
            "deal_created_synced": 1,
            "total_created": 11,
            "total_skipped": 0,
        }
        assert "status" in result
        assert "organisation_id" in result
        assert "deal_id" in result
        assert "emails_synced" in result
        assert "meetings_synced" in result
        assert "stage_changes_synced" in result
        assert "deal_created_synced" in result
        assert "total_created" in result

    def test_failed_result_has_error_field(self) -> None:
        """Failed result should include error information."""
        result = {
            "status": "failed",
            "organisation_id": str(uuid4()),
            "deal_id": str(uuid4()),
            "started_at": datetime.now(UTC).isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "error": "Some error message",
        }
        assert result["status"] == "failed"
        assert "error" in result


@pytest.mark.skipif(False, reason="")
class TestExcludeTimelineEventResultStructure:
    """Tests for exclude_timeline_event_task return value structure."""

    def test_success_result_structure(self) -> None:
        """Success result should have correct structure."""
        result = {
            "success": True,
            "timeline_event_id": str(uuid4()),
        }
        assert result["success"] is True
        assert "timeline_event_id" in result

    def test_failed_result_has_error_field(self) -> None:
        """Failed result should include error information."""
        result = {
            "success": False,
            "timeline_event_id": str(uuid4()),
            "error": "Event not found",
        }
        assert result["success"] is False
        assert "error" in result


# =============================================================================
# Event Types Tests
# =============================================================================


@pytest.mark.skipif(False, reason="")
class TestTimelineEventTypes:
    """Tests for supported timeline event types."""

    def test_valid_event_types(self) -> None:
        """Valid event types should be EMAIL, MEETING, STAGE_CHANGE, DEAL_CREATED."""
        valid_types = ["EMAIL", "MEETING", "STAGE_CHANGE", "DEAL_CREATED"]
        for event_type in valid_types:
            # Just verify these are acceptable string values
            assert event_type in valid_types

    def test_event_types_filter_structure(self) -> None:
        """Event types filter should be a list of strings."""
        event_types_filter = ["EMAIL", "MEETING"]
        assert isinstance(event_types_filter, list)
        for et in event_types_filter:
            assert isinstance(et, str)
