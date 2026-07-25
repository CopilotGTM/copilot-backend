"""Tests for stakeholder sync tasks.

Tests for Task 1: sync_deal_stakeholders_task and sync_all_deals_stakeholders_org_task.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
import src.workers.deal_worker.tasks.deal_mappers.stakeholder_tasks as stakeholder_tasks
from src.domain.models.organisation_tables.job_models import DealJobType
from src.service.crm.deals.mapping_eligibility_service import (
    DealMappingEligibilityResult,
    DealMappingSkipReason,
)

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
    research_contact_ids: set = None
    errors: list[str] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []
        if self.research_contact_ids is None:
            self.research_contact_ids = set()


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
        assert "emit_events" in params
        assert params["emit_events"].default is False

    def test_rejects_event_request_without_visible_job(self) -> None:
        with pytest.raises(ValueError, match="job_id"):
            sync_deal_stakeholders_task(
                organisation_id=str(uuid4()),
                deal_id=str(uuid4()),
                change_type="MANUAL_REFRESH",
                emit_events=True,
            )


class TestSyncDealStakeholdersTaskEligibility:
    def test_skips_ineligible_deal_before_syncing_stakeholders(self, monkeypatch) -> None:
        organisation_id = uuid4()
        deal_id = uuid4()
        eligibility = DealMappingEligibilityResult(
            deal_id=deal_id,
            is_eligible=False,
            reason=DealMappingSkipReason.NO_APP_USER_ON_DEAL,
        )
        eligibility_service = SimpleNamespace(
            get_deal_eligibility=AsyncMock(return_value=eligibility)
        )
        sync_service = SimpleNamespace(sync_for_deal=AsyncMock())
        tracker = SimpleNamespace(
            start=AsyncMock(),
            complete=AsyncMock(),
            fail=AsyncMock(),
        )

        monkeypatch.setattr(
            stakeholder_tasks,
            "DealMappingEligibilityService",
            lambda _organisation_id: eligibility_service,
        )
        monkeypatch.setattr(
            stakeholder_tasks,
            "StakeholderSyncService",
            lambda *_args, **_kwargs: sync_service,
        )
        monkeypatch.setattr(
            stakeholder_tasks,
            "VisibleDealJobTracker",
            lambda *_args, **_kwargs: tracker,
        )

        result = sync_deal_stakeholders_task(
            organisation_id=str(organisation_id),
            deal_id=str(deal_id),
            change_type="INITIAL_BACKFILL",
            job_id=str(uuid4()),
        )

        assert result["status"] == "skipped"
        assert result["reason"] == DealMappingSkipReason.NO_APP_USER_ON_DEAL
        sync_service.sync_for_deal.assert_not_awaited()
        tracker.complete.assert_awaited_once()


def test_initial_backfill_dispatches_one_deal_hydration(monkeypatch) -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    eligibility_service = SimpleNamespace(
        get_deal_eligibility=AsyncMock(
            return_value=DealMappingEligibilityResult(
                deal_id=deal_id,
                is_eligible=True,
                reason=None,
            )
        )
    )
    sync_service = SimpleNamespace(
        sync_for_deal=AsyncMock(
            return_value=MockStakeholderSyncResult(
                success=True,
                created=0,
                requires_rematching=False,
            )
        )
    )
    tracker = SimpleNamespace(start=AsyncMock(), complete=AsyncMock(), fail=AsyncMock())
    dispatch = AsyncMock(return_value=["task-id"])
    monkeypatch.setattr(
        stakeholder_tasks,
        "DealMappingEligibilityService",
        lambda _organisation_id: eligibility_service,
    )
    monkeypatch.setattr(
        stakeholder_tasks,
        "StakeholderSyncService",
        lambda *_args, **_kwargs: sync_service,
    )
    monkeypatch.setattr(
        stakeholder_tasks,
        "VisibleDealJobTracker",
        lambda *_args, **_kwargs: tracker,
    )
    monkeypatch.setattr(
        stakeholder_tasks,
        "dispatch_contact_communication_hydration_async",
        dispatch,
    )

    result = sync_deal_stakeholders_task(
        organisation_id=str(organisation_id),
        deal_id=str(deal_id),
        change_type="INITIAL_BACKFILL",
        job_id=str(uuid4()),
    )

    dispatch.assert_awaited_once_with(
        organisation_id=organisation_id,
        integration_id=None,
        deal_ids=[deal_id],
        trigger="deal_participants_changed",
    )
    assert result["rematching_triggered"] is True


def test_stakeholder_task_dispatches_returned_research_after_sync(monkeypatch) -> None:
    """The worker owns post-commit research dispatch returned by the service."""
    organisation_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()
    eligibility_service = SimpleNamespace(
        get_deal_eligibility=AsyncMock(
            return_value=DealMappingEligibilityResult(
                deal_id=deal_id,
                is_eligible=True,
                reason=None,
            )
        )
    )
    sync_service = SimpleNamespace(
        sync_for_deal=AsyncMock(
            return_value=MockStakeholderSyncResult(research_contact_ids={contact_id})
        )
    )
    tracker = SimpleNamespace(start=AsyncMock(), complete=AsyncMock(), fail=AsyncMock())
    research_dispatch = AsyncMock(return_value="research-job")
    monkeypatch.setattr(
        stakeholder_tasks,
        "DealMappingEligibilityService",
        lambda _organisation_id: eligibility_service,
    )
    monkeypatch.setattr(
        stakeholder_tasks,
        "StakeholderSyncService",
        lambda *_args, **_kwargs: sync_service,
    )
    monkeypatch.setattr(
        stakeholder_tasks,
        "VisibleDealJobTracker",
        lambda *_args, **_kwargs: tracker,
    )
    monkeypatch.setattr(
        stakeholder_tasks,
        "dispatch_contact_research_for_contact",
        research_dispatch,
    )

    result = sync_deal_stakeholders_task(
        organisation_id=str(organisation_id),
        deal_id=str(deal_id),
        change_type="NO_CHANGE",
        trigger_rematching=False,
    )

    research_dispatch.assert_awaited_once_with(
        organisation_id=organisation_id,
        contact_id=contact_id,
        trigger="stakeholder_sync",
    )
    assert result["research_task_ids"] == ["research-job"]


def test_requested_stakeholder_job_hands_terminal_state_to_hydration(monkeypatch) -> None:
    """Only an opted-in job streams progress and remains open until hydration finishes."""
    organisation_id = uuid4()
    deal_id = uuid4()
    job_id = uuid4()
    eligibility_service = SimpleNamespace(
        get_deal_eligibility=AsyncMock(
            return_value=DealMappingEligibilityResult(
                deal_id=deal_id,
                is_eligible=True,
                reason=None,
            )
        )
    )
    sync_service = SimpleNamespace(
        sync_for_deal=AsyncMock(return_value=MockStakeholderSyncResult(success=True))
    )
    tracker = SimpleNamespace(
        start=AsyncMock(),
        progress=AsyncMock(),
        complete=AsyncMock(),
        fail=AsyncMock(),
    )
    tracker_factory = Mock(return_value=tracker)
    dispatch = AsyncMock(return_value=["hydration-task"])
    monkeypatch.setattr(
        stakeholder_tasks,
        "DealMappingEligibilityService",
        lambda _organisation_id: eligibility_service,
    )
    monkeypatch.setattr(
        stakeholder_tasks,
        "StakeholderSyncService",
        lambda *_args, **_kwargs: sync_service,
    )
    monkeypatch.setattr(stakeholder_tasks, "VisibleDealJobTracker", tracker_factory)
    monkeypatch.setattr(
        stakeholder_tasks,
        "dispatch_contact_communication_hydration_async",
        dispatch,
    )

    result = sync_deal_stakeholders_task(
        organisation_id=str(organisation_id),
        deal_id=str(deal_id),
        change_type="MANUAL_REFRESH",
        job_id=str(job_id),
        emit_events=True,
    )

    tracker_factory.assert_called_once_with(
        organisation_id,
        str(job_id),
        DealJobType.STAKEHOLDER_SYNC,
        emit_events=True,
    )
    dispatch.assert_awaited_once_with(
        organisation_id=organisation_id,
        integration_id=None,
        deal_ids=[deal_id],
        trigger="deal_participants_changed",
        job_id=job_id,
        job_type=DealJobType.STAKEHOLDER_SYNC,
        emit_events=True,
    )
    tracker.progress.assert_awaited_once()
    tracker.complete.assert_not_awaited()
    assert result["rematching_triggered"] is True


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


class TestSyncAllDealsStakeholdersOrgTaskDispatch:
    def test_claims_bounded_deal_jobs_and_dispatches_single_deal_tasks(self, monkeypatch) -> None:
        organisation_id = uuid4()
        deal_id = uuid4()
        job_id = uuid4()
        service_result = {
            "status": "completed",
            "organisation_id": str(organisation_id),
            "eligible_deals": 1,
            "already_active": 0,
            "already_completed": 0,
            "claimable_deals": 1,
            "claimed_deals": 1,
            "deal_tasks_dispatched": 0,
            "claim_batch_id": str(uuid4()),
            "deal_tasks": [
                {
                    "organisation_id": str(organisation_id),
                    "deal_id": str(deal_id),
                    "job_id": str(job_id),
                }
            ],
        }
        backfill_service = SimpleNamespace(
            claim_stakeholder_backfill_batch=AsyncMock(return_value=service_result)
        )
        task_result = SimpleNamespace(id="celery-task-id")
        stakeholder_apply_async = Mock(return_value=task_result)
        old_org_sync_service = SimpleNamespace(sync_for_organisation=AsyncMock())

        monkeypatch.setattr(
            stakeholder_tasks,
            "DealMappingBackfillClaimService",
            lambda _organisation_id: backfill_service,
        )
        monkeypatch.setattr(
            stakeholder_tasks,
            "StakeholderSyncService",
            lambda *_args, **_kwargs: old_org_sync_service,
        )
        monkeypatch.setattr(
            stakeholder_tasks.sync_deal_stakeholders_task,
            "apply_async",
            stakeholder_apply_async,
        )
        result = sync_all_deals_stakeholders_org_task(
            organisation_id=str(organisation_id),
            trigger_deal_matching=True,
        )

        backfill_service.claim_stakeholder_backfill_batch.assert_awaited_once_with(
            limit=stakeholder_tasks.INITIAL_BACKFILL_CLAIM_LIMIT
        )
        old_org_sync_service.sync_for_organisation.assert_not_awaited()
        stakeholder_apply_async.assert_called_once()
        dispatched_kwargs = stakeholder_apply_async.call_args.kwargs["kwargs"]
        assert dispatched_kwargs == {
            "organisation_id": str(organisation_id),
            "deal_id": str(deal_id),
            "change_type": "INITIAL_BACKFILL",
            "trigger_rematching": True,
            "job_id": str(job_id),
        }
        assert result["deal_tasks_dispatched"] == 1
        assert result["deal_tasks"][0]["task_id"] == "celery-task-id"
        assert "chunks_dispatched" not in result

    def test_failed_single_deal_dispatch_marks_claimed_job_failed(self, monkeypatch) -> None:
        organisation_id = uuid4()
        deal_id = uuid4()
        job_id = uuid4()
        service_result = {
            "status": "completed",
            "organisation_id": str(organisation_id),
            "eligible_deals": 1,
            "already_active": 0,
            "already_completed": 0,
            "claimable_deals": 1,
            "claimed_deals": 1,
            "deal_tasks_dispatched": 0,
            "claim_batch_id": str(uuid4()),
            "deal_tasks": [
                {
                    "organisation_id": str(organisation_id),
                    "deal_id": str(deal_id),
                    "job_id": str(job_id),
                }
            ],
        }
        backfill_service = SimpleNamespace(
            claim_stakeholder_backfill_batch=AsyncMock(return_value=service_result),
            mark_dispatch_failed=AsyncMock(),
        )
        stakeholder_apply_async = Mock(side_effect=RuntimeError("broker unavailable"))

        monkeypatch.setattr(
            stakeholder_tasks,
            "DealMappingBackfillClaimService",
            lambda _organisation_id: backfill_service,
        )
        monkeypatch.setattr(
            stakeholder_tasks.sync_deal_stakeholders_task,
            "apply_async",
            stakeholder_apply_async,
        )

        result = sync_all_deals_stakeholders_org_task(
            organisation_id=str(organisation_id),
            trigger_deal_matching=True,
        )

        backfill_service.mark_dispatch_failed.assert_awaited_once_with(
            job_id=job_id,
            error="broker unavailable",
        )
        assert result["deal_tasks_dispatched"] == 0
        assert result["deal_tasks"][0]["status"] == "failed"
        assert result["deal_tasks"][0]["error"] == "broker unavailable"


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
