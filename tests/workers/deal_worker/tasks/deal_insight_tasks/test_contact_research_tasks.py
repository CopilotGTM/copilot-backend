"""Regression tests for visible contact-research job progress."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

from src.domain.models.organisation_tables.job_models import DealJobType, JobStatus
from src.workers.deal_worker.tasks.deal_insight_tasks import contact_research_tasks


def test_contact_research_publishes_progress_and_completion(monkeypatch, mocker) -> None:
    """A user-visible research job must close its SSE stream after durable completion."""
    organisation_id = uuid4()
    job_id = uuid4()
    contact_id = uuid4()
    job = SimpleNamespace(
        job_metadata={"contact_id": str(contact_id)},
        status=JobStatus.PENDING,
        updated_at=None,
        result_data=None,
    )

    class FakeSession:
        """Return the same durable job across the task's start and finish sessions."""

        async def get(self, _model, _job_id):
            """Return the research job requested by the worker."""
            return job

        async def commit(self):
            """Accept the worker's lease update without external persistence."""
            return None

    class FakeSessionSource:
        """Yield one org-scoped session per acquisition."""

        async def __aiter__(self):
            yield FakeSession()

    tracker = SimpleNamespace(
        start=mocker.AsyncMock(),
        complete=mocker.AsyncMock(),
        fail=mocker.AsyncMock(),
    )
    tracker_factory = mocker.Mock(return_value=tracker)
    research = mocker.AsyncMock(
        return_value=SimpleNamespace(
            provider_status="completed",
            summary_status="completed",
            summary_markdown="Research summary",
        )
    )

    monkeypatch.setattr(
        contact_research_tasks,
        "acquire_org_async_session",
        lambda _organisation_id: FakeSessionSource(),
    )
    monkeypatch.setattr(
        contact_research_tasks,
        "ContactResearchWorkflowService",
        lambda _organisation_id: SimpleNamespace(research_contact=research),
    )
    monkeypatch.setattr(
        contact_research_tasks,
        "VisibleDealJobTracker",
        tracker_factory,
        raising=False,
    )
    monkeypatch.setattr(contact_research_tasks, "acquire_task_lock", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(contact_research_tasks, "release_task_lock", lambda *_args: None)
    monkeypatch.setattr(
        contact_research_tasks,
        "run_async",
        lambda coroutine: asyncio.run(coroutine),
    )

    result = contact_research_tasks.process_contact_research_job.run(
        organisation_id=str(organisation_id),
        job_id=str(job_id),
    )

    assert result == {"status": "completed", "job_id": str(job_id)}
    tracker_factory.assert_called_once_with(
        organisation_id,
        str(job_id),
        DealJobType.CONTACT_RESEARCH,
        emit_events=True,
    )
    tracker.start.assert_awaited_once_with(
        "researching_contact",
        "Researching stakeholder",
        0.1,
    )
    tracker.complete.assert_awaited_once_with(
        {
            "provider_status": "completed",
            "summary_status": "completed",
            "research_length": 16,
        },
        "saving_contact_research",
        "Saving stakeholder research",
    )


def test_contact_research_redelivery_skips_completed_job(monkeypatch, mocker) -> None:
    """Redelivery after commit must not repeat paid provider work or reopen the job."""
    organisation_id = uuid4()
    job_id = uuid4()
    job = SimpleNamespace(
        job_metadata={"contact_id": str(uuid4())},
        # Production maps this legacy String column as either the enum or its raw value.
        status="completed",
        updated_at=None,
        result_data={"summary_status": "completed"},
    )

    class FakeSession:
        """Expose one already-completed durable research job."""

        async def get(self, _model, _job_id):
            """Return the terminal job to the redelivered task."""
            return job

        async def commit(self):
            """Fail if redelivery tries to reopen the completed job."""
            raise AssertionError("completed job must not be committed again")

    class FakeSessionSource:
        """Yield the terminal job's org-scoped session once."""

        async def __aiter__(self):
            yield FakeSession()

    tracker = SimpleNamespace(
        start=mocker.AsyncMock(),
        complete=mocker.AsyncMock(),
        fail=mocker.AsyncMock(),
    )
    research = mocker.AsyncMock()
    monkeypatch.setattr(
        contact_research_tasks,
        "acquire_org_async_session",
        lambda _organisation_id: FakeSessionSource(),
    )
    monkeypatch.setattr(
        contact_research_tasks,
        "ContactResearchWorkflowService",
        lambda _organisation_id: SimpleNamespace(research_contact=research),
    )
    monkeypatch.setattr(
        contact_research_tasks,
        "VisibleDealJobTracker",
        mocker.Mock(return_value=tracker),
    )
    monkeypatch.setattr(contact_research_tasks, "acquire_task_lock", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(contact_research_tasks, "release_task_lock", lambda *_args: None)
    monkeypatch.setattr(
        contact_research_tasks,
        "run_async",
        lambda coroutine: asyncio.run(coroutine),
    )

    result = contact_research_tasks.process_contact_research_job.run(
        organisation_id=str(organisation_id),
        job_id=str(job_id),
    )

    assert result == {
        "status": "skipped",
        "reason": "already_completed",
        "job_id": str(job_id),
    }
    research.assert_not_awaited()
    tracker.start.assert_not_awaited()
    tracker.complete.assert_not_awaited()
