"""Tests for stakeholder research task SSE progress publishing."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from src.domain.models.organisation_tables.job_models import JobStatus
from src.workers.deal_worker.tasks.deal_insight_tasks import stakeholder_research_tasks


class _Session:
    """Fake org session returning one research job."""

    def __init__(self, job):
        self.job = job
        self.commits = 0

    async def get(self, model, job_id):
        """Return the configured job object."""
        return self.job

    async def commit(self):
        """Track commits performed by the task."""
        self.commits += 1


class _SessionSource:
    """Async iterator matching acquire_org_async_session's usage pattern."""

    def __init__(self, session):
        self.session = session

    async def __aiter__(self):
        """Yield one session."""
        yield self.session


class _Publisher:
    """Collector publisher for research progress frames."""

    frames: list[dict] = []

    def __init__(self, organisation_id, job_id, job_type):
        self.organisation_id = organisation_id
        self.job_id = job_id
        self.job_type = job_type

    def publish_progress(self, message, stage, progress):
        """Capture progress frame fields."""
        self.frames.append(
            {
                "type": "progress",
                "message": message,
                "stage": stage,
                "progress": progress,
            }
        )

    def publish_result(self, result_data):
        """Capture result frame fields."""
        self.frames.append({"type": "result", "content": result_data})

    def publish_error(self, message, error_code):
        """Capture error frame fields."""
        self.frames.append({"type": "error", "message": message, "error_code": error_code})

    def publish_finalize(self, reason="done"):
        """Capture terminal frame reason."""
        self.frames.append({"type": "finalize", "reason": reason})


def test_stakeholder_research_job_publishes_sse_frames(monkeypatch) -> None:
    """Research jobs should update DB status and publish progress/finalize frames."""
    organisation_id = uuid4()
    job_id = uuid4()
    stakeholder_id = uuid4()
    deal_id = uuid4()
    job = SimpleNamespace(
        stakeholder_id=stakeholder_id,
        deal_id=deal_id,
        status=JobStatus.PENDING,
        result_data=None,
        token_usage=None,
        model_name=None,
        cost=None,
        latency_ms=None,
    )
    session = _Session(job)
    _Publisher.frames = []

    class FakeResearchService:
        """Fake research service that completes successfully."""

        last_token_usage = None

        def __init__(self, organisation_id):
            self.organisation_id = organisation_id

        async def research_stakeholder(self, *, stakeholder_id, deal_id):
            return "research markdown"

    monkeypatch.setattr(
        stakeholder_research_tasks, "acquire_task_lock", lambda *args, **kwargs: True
    )
    monkeypatch.setattr(
        stakeholder_research_tasks, "release_task_lock", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        stakeholder_research_tasks,
        "acquire_org_async_session",
        lambda organisation_id: _SessionSource(session),
    )
    monkeypatch.setattr(
        stakeholder_research_tasks, "StakeholderResearchService", FakeResearchService
    )
    monkeypatch.setattr(
        stakeholder_research_tasks,
        "DealJobRedisPublisher",
        _Publisher,
        raising=False,
    )

    result = stakeholder_research_tasks.process_stakeholder_research_job.run(
        str(organisation_id),
        str(job_id),
    )

    assert result == {"status": "completed", "job_id": str(job_id)}
    assert job.status == JobStatus.COMPLETED
    assert job.result_data == {"research_length": len("research markdown")}
    assert [frame["type"] for frame in _Publisher.frames] == [
        "progress",
        "progress",
        "result",
        "progress",
        "finalize",
    ]
    assert _Publisher.frames[-1] == {"type": "finalize", "reason": "done"}
