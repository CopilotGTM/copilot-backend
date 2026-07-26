"""Tests for the deal-worker pipeline dispatch behavior.

These tests lock the sequencing and metadata contracts used when the deal pipeline
enqueues risk, stakeholder, and task-sync jobs for a single deal.
"""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.job_models import DealJobType, JobStatus
from src.workers.deal_worker.utils import pipeline_dispatch


class _FakeSession:
    """In-memory session double for asserting database writes in pipeline tests."""

    def __init__(self) -> None:
        """Track staged entities and commit state without touching a real DB."""
        self.added = []
        self.committed = False

    def add(self, item) -> None:
        """Collect ORM-like objects so tests can assert job ordering and payload."""
        self.added.append(item)

    def flush(self) -> None:
        """No-op placeholder: production sessions flush SQL before commit."""
        return None

    def commit(self) -> None:
        """Mark the transaction as complete to match worker side effects."""
        self.committed = True


class _FakeCelery:
    """Minimal signal-capture object for Celery graph construction assertions."""

    def __init__(self) -> None:
        self.signatures = []

    def signature(self, name: str, **kwargs):
        """Capture a requested signature without running Celery internals."""
        signature = {"name": name, **kwargs}
        self.signatures.append(signature)
        return signature


class _FakeAsyncSession(_FakeSession):
    """Async job-session double that verifies durable commit before dispatch."""

    async def flush(self) -> None:
        """Model the async job insert flush."""
        return None

    async def refresh(self, _item) -> None:
        """Accept job refresh calls after generated IDs are available."""
        return None

    async def commit(self) -> None:
        """Record that async pipeline jobs became durable before dispatch."""
        self.committed = True


def test_dispatch_does_not_run_redis_result_cleanup(monkeypatch) -> None:
    """Fire-and-forget dispatch must not unsubscribe unused results from Redis."""
    cleanup_calls = []

    class ResultBackend:
        def remove_pending_result(self, result) -> None:
            cleanup_calls.append(result)

    class PublishedResult:
        def __init__(self, *, parent=None, results=None) -> None:
            self.backend = ResultBackend()
            self.parent = parent
            self.results = results or []

        def __del__(self) -> None:
            if self.backend is not None:
                self.backend.remove_pending_result(self)

    class Pipeline:
        def apply_async(self):
            parent = PublishedResult()
            child = PublishedResult()
            return PublishedResult(parent=parent, results=[child])

    monkeypatch.setattr(
        pipeline_dispatch,
        "build_deal_insight_pipeline",
        lambda *_args, **_kwargs: Pipeline(),
    )

    pipeline_dispatch.dispatch_deal_insight_pipeline(
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
    )

    assert cleanup_calls == []


@pytest.mark.asyncio
async def test_create_pipeline_jobs_async_commits_exact_meeting_task_job(monkeypatch) -> None:
    """Recall's async path must persist source-bound jobs before Celery receives IDs."""
    monkeypatch.delenv("ENABLE_DEAL_UPDATE_AGENT_PIPELINE", raising=False)
    session = _FakeAsyncSession()
    event_id = uuid4()

    @asynccontextmanager
    async def fake_org_session_scope(_organisation_id):
        """Yield the async job session used by Recall's dispatch path."""
        yield session

    monkeypatch.setattr(
        pipeline_dispatch,
        "org_async_session_scope",
        fake_org_session_scope,
    )

    _, _, task_job_id, _ = await pipeline_dispatch._create_pipeline_jobs_async(
        str(uuid4()),
        str(uuid4()),
        source_event_id=str(event_id),
    )

    assert task_job_id
    assert session.added[2].event_id == event_id
    assert session.committed is True


@pytest.mark.asyncio
async def test_async_dispatch_marks_created_jobs_failed_when_broker_publish_fails(
    monkeypatch,
) -> None:
    """Committed pipeline jobs must not remain pending after a broker rejection."""
    job_ids = [uuid4(), uuid4(), uuid4()]
    jobs = {
        job_id: type(
            "Job",
            (),
            {"status": JobStatus.PENDING, "error_message": None},
        )()
        for job_id in job_ids
    }

    class FailingCanvas:
        """Model a Celery canvas that cannot publish to the broker."""

        def apply_async(self):
            """Raise the same boundary error as a failed broker publish."""
            raise ConnectionError("broker unavailable")

    class FailureSession:
        """Capture failure-state updates for already-committed job rows."""

        committed = False

        async def get(self, _model, job_id):
            """Return the durable job selected for failure marking."""
            return jobs.get(job_id)

        async def commit(self):
            """Record that failure statuses became durable."""
            self.committed = True

    session = FailureSession()

    @asynccontextmanager
    async def fake_org_session_scope(_organisation_id):
        """Yield the failure-update transaction."""
        yield session

    monkeypatch.setattr(
        pipeline_dispatch,
        "_create_pipeline_jobs_async",
        AsyncMock(return_value=tuple(str(job_id) for job_id in job_ids) + (None,)),
    )
    monkeypatch.setattr(
        pipeline_dispatch,
        "_build_deal_insight_canvas",
        lambda *_args: FailingCanvas(),
    )
    monkeypatch.setattr(
        pipeline_dispatch,
        "org_async_session_scope",
        fake_org_session_scope,
    )

    with pytest.raises(ConnectionError, match="broker unavailable"):
        await pipeline_dispatch.dispatch_deal_insight_pipeline_async(
            str(uuid4()), str(uuid4()), str(uuid4())
        )

    assert session.committed is True
    assert all(job.status == JobStatus.FAILED for job in jobs.values())
    assert all("broker unavailable" in job.error_message for job in jobs.values())


def test_create_pipeline_jobs_creates_deal_task_sync_job(monkeypatch) -> None:
    """Verify _create_pipeline_jobs always emits DEAL_TASK_SYNC for deal insights."""

    monkeypatch.delenv("ENABLE_DEAL_UPDATE_AGENT_PIPELINE", raising=False)
    session = _FakeSession()

    @contextmanager
    def fake_org_session_scope(_organisation_id):
        """Yield the fake DB session so pipeline logic is deterministic in tests."""
        yield session

    monkeypatch.setattr(pipeline_dispatch, "org_session_scope", fake_org_session_scope)

    # The pipeline should return three jobs in a fixed sequence so retries are stable
    # across test, queueing, and worker replay.
    source_event_id = uuid4()
    source_association_id = uuid4()
    source_summary_job_id = uuid4()
    risk_job_id, stakeholder_job_id, task_job_id, deal_update_job_id = (
        pipeline_dispatch._create_pipeline_jobs(
            str(uuid4()),
            str(uuid4()),
            source_event_id=str(source_event_id),
            source_association_id=str(source_association_id),
            source_summary_job_id=str(source_summary_job_id),
        )
    )

    assert risk_job_id
    assert stakeholder_job_id
    assert task_job_id
    assert deal_update_job_id is None
    assert [job.job_type for job in session.added] == [
        DealJobType.RISK_BLOCKER_EXTRACTION,
        DealJobType.STAKEHOLDER_ANALYSIS,
        DealJobType.DEAL_TASK_SYNC,
    ]
    assert session.added[2].job_metadata == {
        "source": "meeting_summary_completed",
        "chained_from": "deal_insights",
        "source_association_id": str(source_association_id),
        "source_summary_job_id": str(source_summary_job_id),
    }
    assert session.added[2].event_id == source_event_id
    assert session.committed is True


def test_create_pipeline_jobs_skips_task_sync_without_meeting_source(monkeypatch) -> None:
    """Non-meeting insight refreshes must not trigger the post-meeting task agent."""
    monkeypatch.delenv("ENABLE_DEAL_UPDATE_AGENT_PIPELINE", raising=False)
    session = _FakeSession()

    @contextmanager
    def fake_org_session_scope(_organisation_id):
        """Yield the sync job session for a non-meeting insight refresh."""
        yield session

    monkeypatch.setattr(pipeline_dispatch, "org_session_scope", fake_org_session_scope)

    _, _, task_job_id, _ = pipeline_dispatch._create_pipeline_jobs(
        str(uuid4()),
        str(uuid4()),
    )

    assert task_job_id is None
    assert [job.job_type for job in session.added] == [
        DealJobType.RISK_BLOCKER_EXTRACTION,
        DealJobType.STAKEHOLDER_ANALYSIS,
    ]


def test_build_deal_insight_canvas_appends_deal_task_sync(monkeypatch) -> None:
    """Verify the final task chain appends task sync after insight jobs."""

    celery = _FakeCelery()

    def fake_group(*signatures):
        """Mirror celery.group result shape so we can assert graph structure safely."""
        return {"kind": "group", "items": list(signatures)}

    def fake_chain(*signatures):
        """Mirror celery.chain result shape so downstream payload assertions stay stable."""
        return {"kind": "chain", "items": list(signatures)}

    monkeypatch.setattr(pipeline_dispatch, "get_deal_celery_client", lambda: celery)
    monkeypatch.setattr(pipeline_dispatch, "group", fake_group)
    monkeypatch.setattr(pipeline_dispatch, "chain", fake_chain)

    canvas = pipeline_dispatch._build_deal_insight_canvas(
        organisation_id="org-1",
        deal_id="deal-1",
        job_id="memory-job",
        risk_job_id="risk-job",
        stakeholder_job_id="stakeholder-job",
        task_job_id="task-job",
    )

    assert canvas["kind"] == "chain"
    assert [item["name"] for item in canvas["items"] if item.get("name")] == [
        "deal_memory.process_job",
        "deal_tasks.sync_from_summary",
    ]
    assert canvas["items"][1]["kind"] == "group"
    assert [item["name"] for item in canvas["items"][1]["items"]] == [
        "risk_blocker.extract_deal_risks_blockers_task",
        "stakeholder.analyze_deal_stakeholders_task",
    ]
    assert canvas["items"][2]["kwargs"] == {
        "organisation_id": "org-1",
        "deal_id": "deal-1",
        "job_id": "task-job",
    }
    assert canvas["items"][2]["queue"] == "deal_jobs"
    assert canvas["items"][2]["immutable"] is True
