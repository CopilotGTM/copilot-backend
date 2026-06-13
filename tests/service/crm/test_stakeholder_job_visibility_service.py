"""Stakeholder job visibility regression tests for visible pipeline jobs."""

from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables.job_models import DealJobType, JobStatus
from src.service.crm.jobs.stakeholder_job_visibility_service import (
    StakeholderJobVisibilityService,
)


class _Session:
    """Minimal async session used to verify side effects in visibility job creation."""

    def __init__(self):
        self.added = []
        self.committed = False
        self.commit_count = 0
        self.rolled_back = False

    def add(self, obj):
        """Track ORM objects scheduled for persistence."""
        self.added.append(obj)

    async def flush(self):
        """No-op flush to satisfy service contract."""
        return None

    async def refresh(self, obj):
        """No-op refresh used by service post-write hooks."""
        return None

    async def commit(self):
        """Mark commit for test assertions."""
        self.committed = True
        self.commit_count += 1

    async def rollback(self):
        """Mark rollback for test assertions."""
        self.rolled_back = True


@pytest.mark.asyncio
async def test_create_stakeholder_processing_jobs_registers_three_visible_jobs(monkeypatch):
    """Stakeholder create gets three user-visible processing jobs and no research job."""
    organisation_id = uuid4()
    user_id = uuid4()
    deal_id = uuid4()
    stakeholder_id = uuid4()
    session = _Session()
    sent_chains = []

    async def fake_send_deal_task_chain_async(task_signatures, *, queue="deal"):
        """Capture outgoing chain payload and queue for assertion."""
        sent_chains.append({"task_signatures": task_signatures, "queue": queue})
        return str(uuid4())

    monkeypatch.setattr(
        "src.service.crm.jobs.stakeholder_job_visibility_service.send_deal_task_chain_async",
        fake_send_deal_task_chain_async,
    )

    service = StakeholderJobVisibilityService(session, organisation_id, user_id)
    jobs = await service.create_stakeholder_processing_jobs(deal_id, stakeholder_id)

    assert [job.job_type for job in jobs] == [
        DealJobType.DEAL_EMAIL_REMATCH,
        DealJobType.DEAL_CALENDAR_REMATCH,
        DealJobType.STAKEHOLDER_SYNC,
    ]
    assert all(job.status == JobStatus.PENDING for job in jobs)
    assert all(job.user_id == user_id for job in jobs)
    assert all(job.organisation_id == organisation_id for job in jobs)
    assert all(job.deal_id == deal_id for job in jobs)
    assert all(job.stakeholder_id == stakeholder_id for job in jobs)
    assert session.added == jobs
    assert session.committed is True

    operation_group_ids = {job.job_metadata["operation_group_id"] for job in jobs}
    assert len(operation_group_ids) == 1
    for job in jobs:
        assert UUID(job.job_metadata["operation_group_id"])
        assert job.job_metadata["trigger"] == "stakeholder_created"
        assert job.job_metadata["deal_id"] == str(deal_id)
        assert job.job_metadata["stakeholder_id"] == str(stakeholder_id)
        assert job.job_metadata["participant_id"] == str(stakeholder_id)

    assert len(sent_chains) == 1
    assert sent_chains[0]["queue"] == "deal"
    assert [task["task_name"] for task in sent_chains[0]["task_signatures"]] == [
        "deal.match_email_to_deals",
        "deal.match_calendar_to_deals",
        "stakeholder.sync_deal_stakeholders",
    ]
    assert all(
        task["kwargs"]["organisation_id"] == str(organisation_id)
        for task in sent_chains[0]["task_signatures"]
    )
    assert all(
        task["kwargs"]["deal_id"] == str(deal_id) for task in sent_chains[0]["task_signatures"]
    )
    assert [task["kwargs"]["job_id"] for task in sent_chains[0]["task_signatures"]] == [
        str(job.job_id) for job in jobs
    ]


@pytest.mark.asyncio
async def test_create_stakeholder_processing_jobs_marks_jobs_failed_when_enqueue_fails(
    monkeypatch,
):
    """Committed job rows are marked failed if the ordered chain cannot be queued."""
    session = _Session()

    async def fake_send_deal_task_chain_async(*args, **kwargs):
        """Force a broker failure so caller rollback path is exercised."""
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(
        "src.service.crm.jobs.stakeholder_job_visibility_service.send_deal_task_chain_async",
        fake_send_deal_task_chain_async,
    )

    service = StakeholderJobVisibilityService(session, uuid4(), uuid4())

    with pytest.raises(RuntimeError, match="broker unavailable"):
        await service.create_stakeholder_processing_jobs(uuid4(), uuid4())

    jobs = session.added
    assert session.commit_count == 2
    assert session.rolled_back is False
    assert all(job.status == JobStatus.FAILED for job in jobs)
    assert all(
        job.error_message == "Failed to enqueue stakeholder processing chain" for job in jobs
    )
