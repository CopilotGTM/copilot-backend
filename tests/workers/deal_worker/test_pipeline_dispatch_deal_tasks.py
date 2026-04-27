"""Tests for the deal-worker pipeline dispatch behavior.

These tests lock the sequencing and metadata contracts used when the deal pipeline
enqueues risk, stakeholder, and task-sync jobs for a single deal.
"""

from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4

from src.domain.models.organisation_tables.job_models import DealJobType
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
    risk_job_id, stakeholder_job_id, task_job_id, deal_update_job_id = (
        pipeline_dispatch._create_pipeline_jobs(str(uuid4()), str(uuid4()))
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
        "source": "automated_pipeline",
        "chained_from": "deal_insights",
    }
    assert session.committed is True


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
