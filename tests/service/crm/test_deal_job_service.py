"""Tests for `StakeholderJob` filtering and status helpers in DealJobService."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.service.crm.jobs import deal_job_service as deal_job_service_module
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
        self.commit_count = 0
        self.added: list[DealJobs] = []
        self.rolled_back = False

    def add(self, job: DealJobs) -> None:
        """Capture new jobs created by command-side service methods."""
        self.added.append(job)

    async def flush(self) -> None:
        """Satisfy the AsyncSession write contract without a database."""
        return None

    async def refresh(self, _job: DealJobs) -> None:
        """Keep generated in-memory IDs unchanged after a synthetic flush."""
        return None

    async def execute(self, _stmt):
        """Return next fake result for one execute invocation."""
        return _ExecuteResult(self._results.pop(0))

    async def commit(self):
        """Track commits made by recovery paths."""
        self.commit_count += 1

    async def rollback(self) -> None:
        """Record unexpected rollback paths for failure assertions."""
        self.rolled_back = True


def _job(
    *,
    organisation_id,
    user_id,
    deal_id,
    job_type: DealJobType,
    status: JobStatus,
    stakeholder_id=None,
    metadata: dict | None = None,
) -> DealJobs:
    """Build a synthetic job with a fixed creation timestamp."""
    job = DealJobs(
        organisation_id=organisation_id,
        user_id=user_id,
        deal_id=deal_id,
        stakeholder_id=stakeholder_id,
        job_type=job_type,
        status=status,
        job_metadata=metadata or {},
    )
    job.job_id = uuid4()
    job.created_at = datetime.now(UTC) - timedelta(minutes=1)
    return job


def _stakeholder_processing_group(
    *,
    organisation_id,
    user_id,
    deal_id,
    stakeholder_id,
    enqueue_status: str | None,
) -> list[DealJobs]:
    operation_group_id = str(uuid4())
    jobs = []
    for job_type in (DealJobType.STAKEHOLDER_SYNC,):
        metadata = {
            "trigger": "stakeholder_created",
            "operation_group_id": operation_group_id,
            "stakeholder_id": str(stakeholder_id),
            "participant_id": str(stakeholder_id),
            "deal_id": str(deal_id),
        }
        if enqueue_status is not None:
            metadata["enqueue_status"] = enqueue_status
        jobs.append(
            _job(
                organisation_id=organisation_id,
                user_id=user_id,
                deal_id=deal_id,
                stakeholder_id=stakeholder_id,
                job_type=job_type,
                status=JobStatus.PENDING,
                metadata=metadata,
            )
        )
    return jobs


def test_stakeholder_visibility_job_types_are_defined() -> None:
    """Deal job visibility types should keep stable API values."""
    assert DealJobType.DEAL_EMAIL_REMATCH.value == "deal_email_rematch"
    assert DealJobType.DEAL_CALENDAR_REMATCH.value == "deal_calendar_rematch"
    assert DealJobType.STAKEHOLDER_SYNC.value == "stakeholder_sync"


@pytest.mark.asyncio
async def test_create_stakeholder_processing_jobs_opts_user_request_into_events(
    monkeypatch,
) -> None:
    """A user-triggered job explicitly opts into SSE before entering the worker."""
    organisation_id = uuid4()
    user_id = uuid4()
    deal_id = uuid4()
    stakeholder_id = uuid4()
    session = _Session()
    sent_chains = []

    async def fake_send_deal_task_chain_async(task_signatures, *, queue="deal"):
        sent_chains.append({"task_signatures": task_signatures, "queue": queue})
        return str(uuid4())

    monkeypatch.setattr(
        "src.service.crm.jobs.deal_job_service.send_deal_task_chain_async",
        fake_send_deal_task_chain_async,
    )

    service = DealJobService(session, organisation_id, user_id)
    jobs = await service.create_stakeholder_processing_jobs(deal_id, stakeholder_id)

    assert jobs == session.added
    assert [job.job_type for job in jobs] == [DealJobType.STAKEHOLDER_SYNC]
    assert session.commit_count == 2
    assert sent_chains[0]["task_signatures"][0]["kwargs"]["emit_events"] is True
    assert sent_chains[0]["task_signatures"][0]["kwargs"]["job_id"] == str(jobs[0].job_id)
    assert UUID(jobs[0].job_metadata["operation_group_id"])
    assert jobs[0].job_metadata["enqueue_status"] == "enqueued"


@pytest.mark.asyncio
async def test_create_stakeholder_processing_jobs_marks_committed_job_failed_on_publish_error(
    monkeypatch,
) -> None:
    """A failed broker publish leaves a durable failed job instead of an invisible orphan."""
    session = _Session()

    async def fail_publish(*_args, **_kwargs):
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(
        "src.service.crm.jobs.deal_job_service.send_deal_task_chain_async",
        fail_publish,
    )
    service = DealJobService(session, uuid4(), uuid4())

    with pytest.raises(RuntimeError, match="broker unavailable"):
        await service.create_stakeholder_processing_jobs(uuid4(), uuid4())

    assert session.commit_count == 2
    assert session.rolled_back is False
    assert [job.status for job in session.added] == [JobStatus.FAILED]


@pytest.mark.asyncio
async def test_create_communication_hydration_job_opts_user_request_into_events(
    monkeypatch,
) -> None:
    organisation_id = uuid4()
    user_id = uuid4()
    deal_id = uuid4()
    session = _Session()
    dispatches = []

    async def fake_dispatch(**kwargs):
        dispatches.append(kwargs)
        return ["hydration-task"]

    monkeypatch.setattr(
        deal_job_service_module,
        "dispatch_contact_communication_hydration_async",
        fake_dispatch,
        raising=False,
    )

    job = await DealJobService(
        session,
        organisation_id,
        user_id,
    ).create_communication_hydration_job(deal_id)

    assert job.job_type == DealJobType.DEAL_COMMUNICATION_HYDRATION
    assert session.commit_count == 2
    assert dispatches == [
        {
            "organisation_id": organisation_id,
            "deal_ids": [deal_id],
            "trigger": "deal_created",
            "job_id": job.job_id,
            "job_type": DealJobType.DEAL_COMMUNICATION_HYDRATION,
            "emit_events": True,
        }
    ]


@pytest.mark.asyncio
async def test_create_communication_hydration_job_marks_publish_failure(
    monkeypatch,
) -> None:
    session = _Session()

    async def fail_dispatch(**_kwargs):
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(
        deal_job_service_module,
        "dispatch_contact_communication_hydration_async",
        fail_dispatch,
    )

    with pytest.raises(RuntimeError, match="broker unavailable"):
        await DealJobService(
            session,
            uuid4(),
            uuid4(),
        ).create_communication_hydration_job(uuid4())

    assert session.commit_count == 2
    assert session.added[0].status == JobStatus.FAILED
    assert session.added[0].job_metadata["enqueue_status"] == "failed"


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
            job_type=DealJobType.CONTACT_RESEARCH,
            status=JobStatus.IN_PROGRESS,
        ),
        _job(
            organisation_id=org_id,
            user_id=user_id,
            deal_id=deal_id,
            stakeholder_id=stakeholder_id,
            job_type=DealJobType.STAKEHOLDER_SYNC,
            status=JobStatus.PENDING,
        ),
    ]
    service = DealJobService(_Session(jobs), org_id, user_id)

    active = await service.get_active_stakeholder_processing_jobs(deal_id)

    assert [job["job_type"] for job in active] == ["stakeholder_sync"]
    assert {job["status"] for job in active} == {"pending"}


@pytest.mark.asyncio
async def test_get_active_stakeholder_processing_jobs_recovers_unenqueued_group(
    monkeypatch,
) -> None:
    """Committed stakeholder jobs with no Celery publish are re-enqueued before display."""
    org_id = uuid4()
    user_id = uuid4()
    deal_id = uuid4()
    stakeholder_id = uuid4()
    jobs = _stakeholder_processing_group(
        organisation_id=org_id,
        user_id=user_id,
        deal_id=deal_id,
        stakeholder_id=stakeholder_id,
        enqueue_status=None,
    )
    sent_chains = []

    async def fake_send_deal_task_chain_async(task_signatures, *, queue="deal"):
        sent_chains.append({"task_signatures": task_signatures, "queue": queue})
        return str(uuid4())

    monkeypatch.setattr(
        "src.service.crm.jobs.deal_job_service.send_deal_task_chain_async",
        fake_send_deal_task_chain_async,
    )
    session = _Session(jobs)
    service = DealJobService(session, org_id, user_id)

    active = await service.get_active_stakeholder_processing_jobs(deal_id)

    assert [job["job_type"] for job in active] == ["stakeholder_sync"]
    assert session.commit_count == 1
    assert len(sent_chains) == 1
    assert sent_chains[0]["queue"] == "deal"
    assert [task["task_name"] for task in sent_chains[0]["task_signatures"]] == [
        "stakeholder.sync_deal_stakeholders",
    ]
    assert [task["kwargs"]["job_id"] for task in sent_chains[0]["task_signatures"]] == [
        str(job.job_id) for job in jobs
    ]
    assert all(job.job_metadata["enqueue_status"] == "enqueued" for job in jobs)
    assert all(job.job_metadata["enqueue_recovered"] is True for job in jobs)


@pytest.mark.asyncio
async def test_get_active_stakeholder_processing_jobs_hides_incomplete_unenqueued_group(
    monkeypatch,
) -> None:
    """An incomplete committed group should not be shown as live runnable work."""
    org_id = uuid4()
    user_id = uuid4()
    deal_id = uuid4()
    stakeholder_id = uuid4()
    jobs = _stakeholder_processing_group(
        organisation_id=org_id,
        user_id=user_id,
        deal_id=deal_id,
        stakeholder_id=stakeholder_id,
        enqueue_status="pending",
    )
    duplicate = _job(
        organisation_id=org_id,
        user_id=user_id,
        deal_id=deal_id,
        stakeholder_id=stakeholder_id,
        job_type=DealJobType.STAKEHOLDER_SYNC,
        status=JobStatus.PENDING,
        metadata=dict(jobs[0].job_metadata),
    )
    jobs.append(duplicate)

    async def fake_send_deal_task_chain_async(*args, **kwargs):
        raise AssertionError("incomplete group must not be enqueued")

    monkeypatch.setattr(
        "src.service.crm.jobs.deal_job_service.send_deal_task_chain_async",
        fake_send_deal_task_chain_async,
    )
    session = _Session(jobs)
    service = DealJobService(session, org_id, user_id)

    active = await service.get_active_stakeholder_processing_jobs(deal_id)

    assert active == []
    assert session.commit_count == 0


@pytest.mark.asyncio
async def test_get_active_contact_research_job_is_scoped_to_contact() -> None:
    """Research lookup should filter by contact id in job metadata."""
    org_id = uuid4()
    user_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()
    research_job = _job(
        organisation_id=org_id,
        user_id=user_id,
        deal_id=deal_id,
        job_type=DealJobType.CONTACT_RESEARCH,
        status=JobStatus.IN_PROGRESS,
        metadata={"contact_id": str(contact_id)},
    )
    service = DealJobService(_Session([research_job]), org_id, user_id)

    active = await service.get_active_contact_research_job(contact_id)

    assert active == {
        "job_id": research_job.job_id,
        "job_type": "contact_research",
        "status": "in_progress",
    }


@pytest.mark.asyncio
async def test_get_active_deal_jobs_status_includes_communication_hydration() -> None:
    org_id = uuid4()
    user_id = uuid4()
    deal_id = uuid4()
    hydration_job = _job(
        organisation_id=org_id,
        user_id=user_id,
        deal_id=deal_id,
        job_type=DealJobType.DEAL_COMMUNICATION_HYDRATION,
        status=JobStatus.IN_PROGRESS,
    )
    service = DealJobService(_Session([], [hydration_job], []), org_id, user_id)

    active = await service.get_active_deal_jobs_status(deal_id)

    assert active == {
        "communication_hydration": {
            "job_id": hydration_job.job_id,
            "job_type": "deal_communication_hydration",
            "status": "in_progress",
        }
    }
