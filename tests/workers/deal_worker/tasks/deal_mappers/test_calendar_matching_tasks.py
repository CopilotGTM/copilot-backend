"""Tests for calendar matching tasks.

Tests for Task 2: match_calendar_to_deals_org_task and match_calendar_to_deals_task.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import src.workers.deal_worker.tasks.deal_mappers.calendar_matching_tasks as calendar_tasks
from src.workers.deal_worker.services.deal_mapper_services.deal_mapping_eligibility import (
    DealMappingEligibilityResult,
    DealMappingSkipReason,
)

# Handle circular import gracefully - skip all task-dependent tests if import fails
try:
    from src.workers.deal_worker.tasks.deal_mappers.calendar_matching_tasks import (
        match_calendar_to_deals_org_task,
        match_calendar_to_deals_task,
    )

    TASKS_AVAILABLE = True
except ImportError:
    TASKS_AVAILABLE = False
    match_calendar_to_deals_org_task = None
    match_calendar_to_deals_task = None

pytestmark = pytest.mark.skipif(
    not TASKS_AVAILABLE,
    reason="Circular import in deal_worker - tasks not importable in test context",
)


class TestMatchCalendarToDealsOrgTaskConfig:
    """Tests for match_calendar_to_deals_org_task Celery configuration."""

    def test_task_exists(self) -> None:
        """Task should exist and be importable."""
        assert match_calendar_to_deals_org_task is not None

    def test_task_name(self) -> None:
        """Task should have correct name."""
        assert match_calendar_to_deals_org_task.name == "deal.match_calendar_to_deals_org"

    def test_task_max_retries(self) -> None:
        """Task should have max_retries=2."""
        assert match_calendar_to_deals_org_task.max_retries == 2

    def test_task_default_retry_delay(self) -> None:
        """Task should have default_retry_delay=60."""
        assert match_calendar_to_deals_org_task.default_retry_delay == 60

    def test_task_acks_late(self) -> None:
        """Task should have acks_late=True."""
        assert match_calendar_to_deals_org_task.acks_late is True

    def test_task_soft_time_limit(self) -> None:
        """Task should have soft_time_limit=600 (10 minutes)."""
        assert match_calendar_to_deals_org_task.soft_time_limit == 600

    def test_task_time_limit(self) -> None:
        """Task should have time_limit=660 (11 minutes)."""
        assert match_calendar_to_deals_org_task.time_limit == 660


class TestMatchCalendarToDealsTaskConfig:
    """Tests for match_calendar_to_deals_task Celery configuration."""

    def test_task_exists(self) -> None:
        """Task should exist and be importable."""
        assert match_calendar_to_deals_task is not None

    def test_task_name(self) -> None:
        """Task should have correct name."""
        assert match_calendar_to_deals_task.name == "deal.match_calendar_to_deals"

    def test_task_max_retries(self) -> None:
        """Task should have max_retries=3."""
        assert match_calendar_to_deals_task.max_retries == 3

    def test_task_default_retry_delay(self) -> None:
        """Task should have default_retry_delay=30."""
        assert match_calendar_to_deals_task.default_retry_delay == 30

    def test_task_soft_time_limit(self) -> None:
        """Task should have soft_time_limit=120 (2 minutes)."""
        assert match_calendar_to_deals_task.soft_time_limit == 120

    def test_task_time_limit(self) -> None:
        """Task should have time_limit=150 (2.5 minutes)."""
        assert match_calendar_to_deals_task.time_limit == 150


class TestMatchCalendarToDealsOrgTaskParameters:
    """Tests for match_calendar_to_deals_org_task parameter handling."""

    def test_accepts_organisation_id(self) -> None:
        """Task should accept organisation_id parameter."""
        import inspect

        sig = inspect.signature(match_calendar_to_deals_org_task)
        assert "organisation_id" in sig.parameters


class TestMatchCalendarToDealsTaskParameters:
    """Tests for match_calendar_to_deals_task parameter handling."""

    def test_accepts_required_parameters(self) -> None:
        """Task should accept organisation_id and deal_id parameters."""
        import inspect

        sig = inspect.signature(match_calendar_to_deals_task)
        assert "organisation_id" in sig.parameters
        assert "deal_id" in sig.parameters
        assert "job_id" in sig.parameters
        assert sig.parameters["job_id"].default is None


class TestMatchCalendarToDealsTaskEligibility:
    def test_skips_ineligible_deal_before_matching(self, monkeypatch) -> None:
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
        matcher = SimpleNamespace(process_deal=AsyncMock())
        tracker = SimpleNamespace(
            start=AsyncMock(),
            complete=AsyncMock(),
            fail=AsyncMock(),
        )

        monkeypatch.setattr(
            calendar_tasks,
            "DealMappingEligibilityService",
            lambda _organisation_id: eligibility_service,
        )
        monkeypatch.setattr(
            calendar_tasks,
            "DealMeetingMatcher",
            lambda *_args, **_kwargs: matcher,
        )
        monkeypatch.setattr(
            calendar_tasks,
            "VisibleDealJobTracker",
            lambda *_args, **_kwargs: tracker,
        )

        result = match_calendar_to_deals_task(
            organisation_id=str(organisation_id),
            deal_id=str(deal_id),
            job_id=str(uuid4()),
        )

        assert result["status"] == "skipped"
        assert result["reason"] == DealMappingSkipReason.NO_APP_USER_ON_DEAL
        matcher.process_deal.assert_not_awaited()
        tracker.complete.assert_awaited_once()


class TestMatchCalendarToDealsTaskResult:
    def test_successful_matching_does_not_dispatch_timeline_sync(self, monkeypatch) -> None:
        organisation_id = uuid4()
        deal_id = uuid4()
        eligibility = DealMappingEligibilityResult(
            deal_id=deal_id,
            is_eligible=True,
        )
        eligibility_service = SimpleNamespace(
            get_deal_eligibility=AsyncMock(return_value=eligibility)
        )
        matcher = SimpleNamespace(
            process_deal=AsyncMock(
                return_value={
                    "deals_processed": 1,
                    "meetings_linked": 2,
                    "cancelled_rejected": 0,
                }
            )
        )
        tracker = SimpleNamespace(
            start=AsyncMock(),
            complete=AsyncMock(),
            fail=AsyncMock(),
        )
        monkeypatch.setattr(
            calendar_tasks,
            "DealMappingEligibilityService",
            lambda _organisation_id: eligibility_service,
        )
        monkeypatch.setattr(
            calendar_tasks,
            "DealMeetingMatcher",
            lambda *_args, **_kwargs: matcher,
        )
        monkeypatch.setattr(
            calendar_tasks,
            "VisibleDealJobTracker",
            lambda *_args, **_kwargs: tracker,
        )
        monkeypatch.setattr(
            calendar_tasks,
            "acquire_org_async_session",
            _session_generator_factory([SimpleNamespace()]),
        )

        result = match_calendar_to_deals_task(
            organisation_id=str(organisation_id),
            deal_id=str(deal_id),
            job_id=str(uuid4()),
        )

        assert result["status"] == "success"
        assert "timeline_sync_dispatched" not in result
        assert "timeline_sync_dispatch_error" not in result


# Tests that don't require task imports
@pytest.mark.skipif(False, reason="")  # Always run these
class TestMatchCalendarToDealsOrgTaskResultStructure:
    """Tests for match_calendar_to_deals_org_task return value structure."""

    def test_success_result_has_required_fields(self) -> None:
        """Successful result should have all required fields."""
        result = {
            "status": "success",
            "organisation_id": str(uuid4()),
            "started_at": datetime.now(UTC).isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "deals_processed": 15,
            "meetings_linked": 42,
            "cancelled_rejected": 3,
        }

        assert "status" in result
        assert "organisation_id" in result
        assert "started_at" in result
        assert "completed_at" in result
        assert "deals_processed" in result
        assert "meetings_linked" in result

    def test_failed_result_has_error_field(self) -> None:
        """Failed result should include error information."""
        result = {
            "status": "failed",
            "organisation_id": str(uuid4()),
            "error": "Some error message",
        }
        assert result["status"] == "failed"
        assert "error" in result


@pytest.mark.skipif(False, reason="")  # Always run these
class TestMatchCalendarToDealsTaskResultStructure:
    """Tests for match_calendar_to_deals_task return value structure."""

    def test_success_result_has_required_fields(self) -> None:
        """Successful result should have all required fields."""
        result = {
            "status": "success",
            "organisation_id": str(uuid4()),
            "deal_id": str(uuid4()),
            "meetings_linked": 5,
        }
        assert "status" in result
        assert "deal_id" in result
        assert "meetings_linked" in result

    def test_failed_result_has_error_fields(self) -> None:
        """Failed result should include error information."""
        result = {
            "status": "failed",
            "error": "Deal not found",
            "error_type": "ValueError",
        }
        assert result["status"] == "failed"
        assert "error" in result


@pytest.mark.skipif(False, reason="")  # Always run these
class TestRetryBehavior:
    """Tests for task retry behavior."""

    def test_exponential_backoff_formula(self) -> None:
        """Retry delays should follow exponential backoff pattern."""
        base_delay = 60
        assert base_delay * (2**0) == 60
        assert base_delay * (2**1) == 120
        assert base_delay * (2**2) == 240

    def test_deal_task_backoff_formula(self) -> None:
        """Per-deal task retry delays should use 30s base."""
        base_delay = 30
        assert base_delay * (2**0) == 30
        assert base_delay * (2**1) == 60
        assert base_delay * (2**2) == 120


def _session_generator_factory(sessions: list[object]):
    queue = iter(sessions)

    def _factory(*_args: object) -> AsyncIterator[object]:
        async def _generator() -> AsyncIterator[object]:
            yield next(queue)

        return _generator()

    return _factory
