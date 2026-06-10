"""Tests for downstream deal-mapping jobs created by stakeholder backfill."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import src.workers.deal_worker.services.deal_mapper_services.deal_mapping_downstream_job_service as downstream_module
from src.domain.models.organisation_tables.job_models import DealJobType, JobStatus
from src.workers.deal_worker.services.deal_mapper_services.deal_mapping_backfill_claim_service import (
    INITIAL_BACKFILL_MAPPING_PURPOSE,
    INITIAL_BACKFILL_SOURCE,
)
from src.workers.deal_worker.services.deal_mapper_services.deal_mapping_downstream_job_service import (
    DealMappingDownstreamJobService,
)
from src.workers.deal_worker.utils.pipeline_dispatch import AUTOMATED_PIPELINE_USER_ID


@pytest.mark.asyncio
async def test_claim_initial_backfill_rematch_jobs_creates_calendar_and_email_jobs(
    monkeypatch,
) -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    parent_job_id = uuid4()
    session = _FakeSession(existing_jobs=[])
    monkeypatch.setattr(
        downstream_module,
        "acquire_org_async_session",
        _session_generator_factory([session]),
    )

    result = await DealMappingDownstreamJobService(organisation_id).claim_rematch_jobs(
        deal_id=deal_id,
        parent_stakeholder_job_id=parent_job_id,
        change_type="INITIAL_BACKFILL",
    )

    assert result["calendar_job_id"]
    assert result["email_job_id"]
    assert result["calendar_dispatch_required"] is True
    assert result["email_dispatch_required"] is True
    assert [job.job_type for job in session.added] == [
        DealJobType.DEAL_CALENDAR_REMATCH,
        DealJobType.DEAL_EMAIL_REMATCH,
    ]
    assert all(job.organisation_id == organisation_id for job in session.added)
    assert all(job.deal_id == deal_id for job in session.added)
    assert all(job.user_id == AUTOMATED_PIPELINE_USER_ID for job in session.added)
    assert all(job.status == JobStatus.PENDING for job in session.added)
    assert all(job.job_metadata["source"] == INITIAL_BACKFILL_SOURCE for job in session.added)
    assert all(
        job.job_metadata["mapping_purpose"] == INITIAL_BACKFILL_MAPPING_PURPOSE
        for job in session.added
    )
    assert all(
        job.job_metadata["parent_stakeholder_job_id"] == str(parent_job_id) for job in session.added
    )
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_claim_initial_backfill_rematch_jobs_reuses_active_or_completed_jobs(
    monkeypatch,
) -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    calendar_job_id = uuid4()
    email_job_id = uuid4()
    session = _FakeSession(
        existing_jobs=[
            _job(calendar_job_id, deal_id, DealJobType.DEAL_CALENDAR_REMATCH, JobStatus.PENDING),
            _job(email_job_id, deal_id, DealJobType.DEAL_EMAIL_REMATCH, JobStatus.COMPLETED),
        ]
    )
    monkeypatch.setattr(
        downstream_module,
        "acquire_org_async_session",
        _session_generator_factory([session]),
    )

    result = await DealMappingDownstreamJobService(organisation_id).claim_rematch_jobs(
        deal_id=deal_id,
        parent_stakeholder_job_id=uuid4(),
        change_type="INITIAL_BACKFILL",
    )

    assert result["calendar_job_id"] == str(calendar_job_id)
    assert result["email_job_id"] == str(email_job_id)
    assert result["calendar_dispatch_required"] is False
    assert result["email_dispatch_required"] is False
    assert session.added == []
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_initial_backfill_rematch_jobs_reclaims_failed_jobs(
    monkeypatch,
) -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    session = _FakeSession(
        existing_jobs=[
            _job(uuid4(), deal_id, DealJobType.DEAL_CALENDAR_REMATCH, JobStatus.FAILED),
            _job(uuid4(), deal_id, DealJobType.DEAL_EMAIL_REMATCH, JobStatus.FAILED),
        ]
    )
    monkeypatch.setattr(
        downstream_module,
        "acquire_org_async_session",
        _session_generator_factory([session]),
    )

    result = await DealMappingDownstreamJobService(organisation_id).claim_rematch_jobs(
        deal_id=deal_id,
        parent_stakeholder_job_id=uuid4(),
        change_type="INITIAL_BACKFILL",
    )

    assert result["calendar_dispatch_required"] is True
    assert result["email_dispatch_required"] is True
    assert [job.job_type for job in session.added] == [
        DealJobType.DEAL_CALENDAR_REMATCH,
        DealJobType.DEAL_EMAIL_REMATCH,
    ]


@pytest.mark.asyncio
async def test_claim_rematch_jobs_for_event_does_not_reuse_completed_jobs(
    monkeypatch,
) -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    session = _FakeSession(
        existing_jobs=[
            _job(uuid4(), deal_id, DealJobType.DEAL_CALENDAR_REMATCH, JobStatus.COMPLETED),
            _job(uuid4(), deal_id, DealJobType.DEAL_EMAIL_REMATCH, JobStatus.COMPLETED),
        ]
    )
    monkeypatch.setattr(
        downstream_module,
        "acquire_org_async_session",
        _session_generator_factory([session]),
    )

    result = await DealMappingDownstreamJobService(organisation_id).claim_rematch_jobs(
        deal_id=deal_id,
        parent_stakeholder_job_id=uuid4(),
        change_type="PARTICIPANT_ADDED",
    )

    assert result["calendar_dispatch_required"] is True
    assert result["email_dispatch_required"] is True
    assert [job.job_metadata["mapping_purpose"] for job in session.added] == [
        "event_rematch",
        "event_rematch",
    ]
    assert [job.job_metadata["created_from_change_type"] for job in session.added] == [
        "PARTICIPANT_ADDED",
        "PARTICIPANT_ADDED",
    ]


@pytest.mark.asyncio
async def test_claim_rematch_jobs_for_event_reuses_active_jobs(monkeypatch) -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    calendar_job_id = uuid4()
    email_job_id = uuid4()
    session = _FakeSession(
        existing_jobs=[
            _job(
                calendar_job_id,
                deal_id,
                DealJobType.DEAL_CALENDAR_REMATCH,
                JobStatus.PENDING,
                mapping_purpose="event_rematch",
            ),
            _job(
                email_job_id,
                deal_id,
                DealJobType.DEAL_EMAIL_REMATCH,
                JobStatus.IN_PROGRESS,
                mapping_purpose="event_rematch",
            ),
        ]
    )
    monkeypatch.setattr(
        downstream_module,
        "acquire_org_async_session",
        _session_generator_factory([session]),
    )

    result = await DealMappingDownstreamJobService(organisation_id).claim_rematch_jobs(
        deal_id=deal_id,
        parent_stakeholder_job_id=uuid4(),
        change_type="PARTICIPANT_ADDED",
    )

    assert result["calendar_job_id"] == str(calendar_job_id)
    assert result["email_job_id"] == str(email_job_id)
    assert result["calendar_dispatch_required"] is False
    assert result["email_dispatch_required"] is False
    assert session.added == []


def _job(
    job_id,
    deal_id,
    job_type: DealJobType,
    status: JobStatus,
    *,
    mapping_purpose: str = INITIAL_BACKFILL_MAPPING_PURPOSE,
) -> SimpleNamespace:
    return SimpleNamespace(
        job_id=job_id,
        deal_id=deal_id,
        job_type=job_type,
        status=status,
        job_metadata={"mapping_purpose": mapping_purpose},
    )


class _FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self) -> _FakeScalarResult:
        return _FakeScalarResult(self._rows)


class _FakeSession:
    def __init__(self, *, existing_jobs):
        self.existing_jobs = existing_jobs
        self.added = []
        self.commit = AsyncMock()

    async def execute(self, _stmt):
        return _FakeExecuteResult(self.existing_jobs)

    def add(self, job):
        self.added.append(job)


def _session_generator_factory(sessions: list[object]):
    queue = iter(sessions)

    def _factory(*_args: object) -> AsyncIterator[object]:
        async def _generator() -> AsyncIterator[object]:
            yield next(queue)

        return _generator()

    return _factory
