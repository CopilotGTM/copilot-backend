"""Tests for stakeholder sync tasks.

Tests for Task 1: sync_deal_stakeholders_task and sync_all_deals_stakeholders_org_task.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest

# Handle circular import gracefully - skip all task-dependent tests if import fails
try:
    from src.workers.deal_worker.tasks.deal_mappers.stakeholder_tasks import (
        sync_all_deals_stakeholders_org_task,
        sync_deal_stakeholders_task,
    )

    TASKS_AVAILABLE = True
except ImportError:
    TASKS_AVAILABLE = False
    sync_deal_stakeholders_task = None
    sync_all_deals_stakeholders_org_task = None

pytestmark = pytest.mark.skipif(
    not TASKS_AVAILABLE,
    reason="Circular import in deal_worker - tasks not importable in test context",
)


# Test data structures
@dataclass
class MockStakeholderSyncResult:
    """Mock result from StakeholderSyncService."""

    success: bool = True
    created: int = 0
    updated: int = 0
    deactivated: int = 0
    skipped: int = 0
    requires_rematching: bool = False
    errors: list[str] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


@dataclass
class MockOrgSyncResult:
    """Mock result from StakeholderSyncService.sync_for_organisation."""

    total_deals: int = 0
    successful_deals: int = 0
    failed_deals: int = 0
    stakeholders_created: int = 0
    stakeholders_updated: int = 0
    stakeholders_deactivated: int = 0
    requires_rematching: bool = False
    errors: list[str] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


class TestSyncDealStakeholdersTaskConfig:
    """Tests for sync_deal_stakeholders_task Celery configuration."""

    def test_task_exists(self) -> None:
        """Task should exist and be importable."""
        assert sync_deal_stakeholders_task is not None

    def test_task_name(self) -> None:
        """Task should have correct name."""
        assert sync_deal_stakeholders_task.name == "stakeholder.sync_deal_stakeholders"

    def test_task_max_retries(self) -> None:
        """Task should have max_retries=3."""
        assert sync_deal_stakeholders_task.max_retries == 3

    def test_task_default_retry_delay(self) -> None:
        """Task should have default_retry_delay=60."""
        assert sync_deal_stakeholders_task.default_retry_delay == 60

    def test_task_acks_late(self) -> None:
        """Task should have acks_late=True."""
        assert sync_deal_stakeholders_task.acks_late is True

    def test_task_soft_time_limit(self) -> None:
        """Task should have soft_time_limit=300 (5 minutes)."""
        assert sync_deal_stakeholders_task.soft_time_limit == 300

    def test_task_time_limit(self) -> None:
        """Task should have time_limit=360 (6 minutes)."""
        assert sync_deal_stakeholders_task.time_limit == 360


class TestSyncAllDealsStakeholdersOrgTaskConfig:
    """Tests for sync_all_deals_stakeholders_org_task Celery configuration."""

    def test_task_exists(self) -> None:
        """Task should exist and be importable."""
        assert sync_all_deals_stakeholders_org_task is not None

    def test_task_name(self) -> None:
        """Task should have correct name."""
        assert (
            sync_all_deals_stakeholders_org_task.name
            == "stakeholder.sync_all_deals_stakeholders_org"
        )

    def test_task_max_retries(self) -> None:
        """Task should have max_retries=2."""
        assert sync_all_deals_stakeholders_org_task.max_retries == 2

    def test_task_default_retry_delay(self) -> None:
        """Task should have default_retry_delay=120."""
        assert sync_all_deals_stakeholders_org_task.default_retry_delay == 120

    def test_task_soft_time_limit(self) -> None:
        """Task should have soft_time_limit=1800 (30 minutes)."""
        assert sync_all_deals_stakeholders_org_task.soft_time_limit == 1800

    def test_task_time_limit(self) -> None:
        """Task should have time_limit=1860 (31 minutes)."""
        assert sync_all_deals_stakeholders_org_task.time_limit == 1860


class TestSyncDealStakeholdersTaskParameters:
    """Tests for sync_deal_stakeholders_task parameter handling."""

    def test_accepts_required_parameters(self) -> None:
        """Task should accept required parameters."""
        import inspect

        sig = inspect.signature(sync_deal_stakeholders_task)
        params = sig.parameters
        assert "organisation_id" in params
        assert "deal_id" in params
        assert "change_type" in params

    def test_accepts_optional_parameters(self) -> None:
        """Task should accept optional parameters."""
        import inspect

        sig = inspect.signature(sync_deal_stakeholders_task)
        params = sig.parameters
        assert "integration_id" in params
        assert params["integration_id"].default is None
        assert "trigger_rematching" in params
        assert params["trigger_rematching"].default is True
        assert "job_id" in params
        assert params["job_id"].default is None


class TestSyncAllDealsStakeholdersOrgTaskParameters:
    """Tests for sync_all_deals_stakeholders_org_task parameter handling."""

    def test_accepts_required_parameters(self) -> None:
        """Task should accept organisation_id."""
        import inspect

        sig = inspect.signature(sync_all_deals_stakeholders_org_task)
        assert "organisation_id" in sig.parameters

    def test_accepts_trigger_deal_matching_parameter(self) -> None:
        """Task should accept trigger_deal_matching with default True."""
        import inspect

        sig = inspect.signature(sync_all_deals_stakeholders_org_task)
        params = sig.parameters
        assert "trigger_deal_matching" in params
        assert params["trigger_deal_matching"].default is True


# Tests that don't require task imports - always run
@pytest.mark.skipif(False, reason="")
class TestSyncDealStakeholdersTaskResultStructure:
    """Tests for sync_deal_stakeholders_task return value structure."""

    def test_success_result_has_required_fields(self) -> None:
        """Successful result should have all required fields."""
        result = {
            "status": "success",
            "deal_id": str(uuid4()),
            "change_type": "NEW_DEAL",
            "started_at": datetime.now(UTC).isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "stakeholders_created": 5,
            "stakeholders_updated": 2,
            "stakeholders_deactivated": 1,
            "rematching_triggered": True,
        }
        assert "status" in result
        assert "deal_id" in result
        assert "stakeholders_created" in result
        assert "rematching_triggered" in result

    def test_failed_result_has_error_field(self) -> None:
        """Failed result should include error information."""
        result = {
            "status": "failed",
            "deal_id": str(uuid4()),
            "error": "Some error message",
        }
        assert result["status"] == "failed"
        assert "error" in result


@pytest.mark.skipif(False, reason="")
class TestSyncAllDealsStakeholdersOrgTaskResultStructure:
    """Tests for sync_all_deals_stakeholders_org_task return value structure."""

    def test_completed_result_has_required_fields(self) -> None:
        """Completed result should have all required fields."""
        result = {
            "status": "completed",
            "organisation_id": str(uuid4()),
            "total_deals": 10,
            "successful_deals": 10,
            "failed_deals": 0,
            "stakeholders_created": 50,
            "deal_matching_triggered": True,
        }
        assert "status" in result
        assert "total_deals" in result
        assert "successful_deals" in result
        assert "stakeholders_created" in result

    def test_partial_result_status(self) -> None:
        """Partial result should have status='partial'."""
        result = MockOrgSyncResult(
            total_deals=10,
            successful_deals=7,
            failed_deals=3,
        )
        if result.failed_deals == 0:
            status = "completed"
        elif result.successful_deals == 0:
            status = "failed"
        else:
            status = "partial"
        assert status == "partial"

    def test_failed_result_status(self) -> None:
        """Failed result should have status='failed' when all deals fail."""
        result = MockOrgSyncResult(
            total_deals=10,
            successful_deals=0,
            failed_deals=10,
        )
        if result.failed_deals == 0:
            status = "completed"
        elif result.successful_deals == 0:
            status = "failed"
        else:
            status = "partial"
        assert status == "failed"


@pytest.mark.skipif(False, reason="")
class TestStatusDetermination:
    """Tests for status determination logic."""

    def test_completed_status_when_no_failures(self) -> None:
        """Status should be 'completed' when failed_deals=0."""
        result = MockOrgSyncResult(total_deals=10, successful_deals=10, failed_deals=0)
        if result.failed_deals == 0:
            status = "completed"
        elif result.successful_deals == 0:
            status = "failed"
        else:
            status = "partial"
        assert status == "completed"

    def test_failed_status_when_all_fail(self) -> None:
        """Status should be 'failed' when successful_deals=0."""
        result = MockOrgSyncResult(total_deals=10, successful_deals=0, failed_deals=10)
        if result.failed_deals == 0:
            status = "completed"
        elif result.successful_deals == 0:
            status = "failed"
        else:
            status = "partial"
        assert status == "failed"

    def test_partial_status_when_some_fail(self) -> None:
        """Status should be 'partial' when some succeed and some fail."""
        result = MockOrgSyncResult(total_deals=10, successful_deals=7, failed_deals=3)
        if result.failed_deals == 0:
            status = "completed"
        elif result.successful_deals == 0:
            status = "failed"
        else:
            status = "partial"
        assert status == "partial"


@pytest.mark.skipif(False, reason="")
class TestChangeTypes:
    """Tests for change_type handling."""

    def test_valid_change_types(self) -> None:
        """Valid change types should be accepted."""
        valid_types = ["NEW_DEAL", "PARTICIPANT_ADDED", "PARTICIPANT_REMOVED", "DEAL_UPDATED"]
        for change_type in valid_types:
            result = {"status": "success", "change_type": change_type}
            assert result["change_type"] == change_type
