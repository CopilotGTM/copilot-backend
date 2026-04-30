"""Tests for `StakeholderJob` filtering and status helpers in DealJobService."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.service.crm.jobs.deal_job_service import DealJobService


class _ScalarRows:
    """Container for scalar rows used by fake query results."""

    def __init__(self, rows: list[DealJobs]) -> None:
        self._rows = rows

    def first(self) -> DealJobs | None:
        """Return first row or None when query result is empty."""
        return self._rows[0] if self._rows else None

    def all(self) -> list[DealJobs]:
        """Return all rows from fake scalar result."""
        return self._rows


class _ExecuteResult:
    """Container representing `session.execute` return shape."""

    def __init__(self, rows: list[DealJobs]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarRows:
        """Expose scalar row API expected by service logic."""
        return _ScalarRows(self._rows)


class _Session:
    """Fake AsyncSession that yields canned query results."""

    def __init__(self, *results: list[DealJobs]) -> None:
        self._results = list(results)

    async def execute(self, _stmt):
        """Return next fake result for one execute invocation."""
        return _ExecuteResult(self._results.pop(0))


def _job(
    *,
    organisation_id,
    user_id,
    deal_id,
    job_type: DealJobType,
    status: JobStatus,
    stakeholder_id=None,
) -> DealJobs:
    """Build a synthetic job with a fixed creation timestamp."""
    job = DealJobs(
        organisation_id=organisation_id,
        user_id=user_id,
        deal_id=deal_id,
        stakeholder_id=stakeholder_id,
        job_type=job_type,
        status=status,
    )
    job.job_id = uuid4()
    job.created_at = datetime.now(UTC) - timedelta(minutes=1)
    return job


def test_stakeholder_visibility_job_types_are_defined() -> None:
    """Deal job visibility types should keep stable API values."""
    assert DealJobType.DEAL_EMAIL_REMATCH.value == "deal_email_rematch"
    assert DealJobType.DEAL_CALENDAR_REMATCH.value == "deal_calendar_rematch"
    assert DealJobType.STAKEHOLDER_SYNC.value == "stakeholder_sync"


@pytest.mark.asyncio
async def test_get_active_stakeholder_processing_jobs_returns_visible_jobs_only() -> None:
    """Only active visible rematch jobs should be included in stake-holder visibility."""
    org_id = uuid4()
    user_id = uuid4()
    deal_id = uuid4()
    stakeholder_id = uuid4()
    jobs = [
        _job(
            organisation_id=org_id,
            user_id=user_id,
            deal_id=deal_id,
            stakeholder_id=stakeholder_id,
            job_type=DealJobType.DEAL_EMAIL_REMATCH,
            status=JobStatus.PENDING,
        ),
        _job(
            organisation_id=org_id,
            user_id=user_id,
            deal_id=deal_id,
            stakeholder_id=stakeholder_id,
            job_type=DealJobType.DEAL_CALENDAR_REMATCH,
            status=JobStatus.IN_PROGRESS,
        ),
        _job(
            organisation_id=org_id,
            user_id=user_id,
            deal_id=deal_id,
            stakeholder_id=stakeholder_id,
            job_type=DealJobType.STAKEHOLDER_RESEARCH,
            status=JobStatus.IN_PROGRESS,
        ),
        _job(
            organisation_id=org_id,
            user_id=user_id,
            deal_id=deal_id,
            stakeholder_id=stakeholder_id,
            job_type=DealJobType.STAKEHOLDER_SYNC,
            status=JobStatus.COMPLETED,
        ),
    ]
    service = DealJobService(_Session(jobs), org_id, user_id)

    active = await service.get_active_stakeholder_processing_jobs(deal_id)

    assert [job["job_type"] for job in active] == [
        "deal_email_rematch",
        "deal_calendar_rematch",
    ]
    assert {job["status"] for job in active} == {"pending", "in_progress"}


@pytest.mark.asyncio
async def test_get_active_stakeholder_research_job_is_scoped_to_stakeholder() -> None:
    """Research lookup should filter by stakeholder id for the requested deal."""
    org_id = uuid4()
    user_id = uuid4()
    deal_id = uuid4()
    stakeholder_id = uuid4()
    research_job = _job(
        organisation_id=org_id,
        user_id=user_id,
        deal_id=deal_id,
        stakeholder_id=stakeholder_id,
        job_type=DealJobType.STAKEHOLDER_RESEARCH,
        status=JobStatus.IN_PROGRESS,
    )
    service = DealJobService(_Session([research_job]), org_id, user_id)

    active = await service.get_active_stakeholder_research_job(deal_id, stakeholder_id)

    assert active == {
        "job_id": research_job.job_id,
        "job_type": "stakeholder_research",
        "status": "in_progress",
    }
