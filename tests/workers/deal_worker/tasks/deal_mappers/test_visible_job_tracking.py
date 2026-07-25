"""Visible job tracking tests for match progress and terminal-state frames."""

from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.job_models import DealJobType, JobStatus
from src.workers.deal_worker.tasks.deal_mappers.visible_job_tracking import (
    VisibleDealJobTracker,
)


class _Job:
    """Simple job object used to observe tracker-driven state transitions."""

    def __init__(self):
        self.status = JobStatus.PENDING
        self.result_data = None
        self.error_message = None
        self.retry_count = 0


class _Session:
    """Session fake that tracks commit calls for tracker assertions."""

    def __init__(self, job):
        self.job = job
        self.commits = 0

    async def get(self, model, job_id):
        """Return the synthetic job under test."""
        return self.job

    async def commit(self):
        """Count commit calls to prove state is persisted each transition."""
        self.commits += 1


class _SessionSource:
    """Context-manager-style async iterator for acquire_org_async_session."""

    def __init__(self, session):
        self.session = session

    async def __aiter__(self):
        """Yield a single session object per tracker call."""
        yield self.session


class _Publisher:
    """Collector publisher that records frames sent through the tracker."""

    def __init__(self, organisation_id, job_id, job_type):
        self.organisation_id = organisation_id
        self.job_id = job_id
        self.job_type = job_type
        self.frames = []

    def publish_progress(self, message, stage, progress):
        """Capture progress events so tests can assert stage progression."""
        self.frames.append(
            {
                "kind": "progress",
                "message": message,
                "stage": stage,
                "progress": progress,
            }
        )

    def publish_error(self, message, error_code):
        """Capture terminal error path payload emitted by tracker."""
        self.frames.append({"kind": "error", "message": message, "error_code": error_code})

    def publish_finalize(self, reason="done"):
        """Capture finalize event reason for completion/error closure checks."""
        self.frames.append({"kind": "finalize", "reason": reason})


@pytest.mark.asyncio
async def test_visible_deal_job_tracker_marks_started_and_publishes_progress(monkeypatch):
    """Tracker.start should transition job into progress and emit queued/frame events."""
    job = _Job()
    session = _Session(job)
    publishers = []

    def publisher_factory(*args):
        """Build and keep an assertion-friendly publisher instance."""
        publisher = _Publisher(*args)
        publishers.append(publisher)
        return publisher

    monkeypatch.setattr(
        "src.workers.deal_worker.tasks.deal_mappers.visible_job_tracking.acquire_org_async_session",
        lambda organisation_id: _SessionSource(session),
    )
    monkeypatch.setattr(
        "src.workers.deal_worker.tasks.deal_mappers.visible_job_tracking.DealJobRedisPublisher",
        publisher_factory,
    )

    tracker = VisibleDealJobTracker(
        uuid4(),
        str(uuid4()),
        DealJobType.DEAL_EMAIL_REMATCH,
        emit_events=True,
    )
    await tracker.start("matching_emails", "Updating related emails", 0.25)

    assert job.status == JobStatus.IN_PROGRESS
    assert session.commits == 1
    assert publishers[0].frames == [
        {
            "kind": "progress",
            "message": "Queued",
            "stage": "queued",
            "progress": 0.0,
        },
        {
            "kind": "progress",
            "message": "Updating related emails",
            "stage": "matching_emails",
            "progress": 0.25,
        },
    ]


@pytest.mark.asyncio
async def test_visible_deal_job_tracker_advances_without_finishing_the_job() -> None:
    """A worker handoff publishes one stage while leaving terminal ownership downstream."""
    publisher = _Publisher(uuid4(), uuid4(), DealJobType.STAKEHOLDER_SYNC.value)
    tracker = VisibleDealJobTracker(
        uuid4(),
        str(uuid4()),
        DealJobType.STAKEHOLDER_SYNC,
        emit_events=True,
    )
    tracker.publisher = publisher

    await tracker.progress(
        "hydrating_communications",
        "Matching communication history",
        0.55,
    )

    assert publisher.frames == [
        {
            "kind": "progress",
            "message": "Matching communication history",
            "stage": "hydrating_communications",
            "progress": 0.55,
        }
    ]


@pytest.mark.asyncio
async def test_visible_deal_job_tracker_completes_and_finalizes(monkeypatch):
    """Tracker.complete should persist final payload and emit completion frames."""
    job = _Job()
    session = _Session(job)
    publisher = _Publisher(uuid4(), uuid4(), DealJobType.DEAL_EMAIL_REMATCH.value)

    monkeypatch.setattr(
        "src.workers.deal_worker.tasks.deal_mappers.visible_job_tracking.acquire_org_async_session",
        lambda organisation_id: _SessionSource(session),
    )

    tracker = VisibleDealJobTracker(
        uuid4(),
        str(uuid4()),
        DealJobType.DEAL_EMAIL_REMATCH,
        emit_events=True,
    )
    tracker.publisher = publisher
    result_data = {"emails_matched": 2}

    await tracker.complete(
        result_data,
        "saving_email_links",
        "Saving email links",
    )

    assert job.status == JobStatus.COMPLETED
    assert job.result_data == result_data
    assert session.commits == 1
    assert publisher.frames == [
        {
            "kind": "progress",
            "message": "Saving email links",
            "stage": "saving_email_links",
            "progress": 0.9,
        },
        {
            "kind": "progress",
            "message": "Complete",
            "stage": "complete",
            "progress": 1.0,
        },
        {"kind": "finalize", "reason": "done"},
    ]


@pytest.mark.asyncio
async def test_visible_deal_job_tracker_fails_and_finalizes(monkeypatch):
    """Tracker.fail should persist error state and emit error/finalize frames."""
    job = _Job()
    session = _Session(job)
    publisher = _Publisher(uuid4(), uuid4(), DealJobType.DEAL_EMAIL_REMATCH.value)

    monkeypatch.setattr(
        "src.workers.deal_worker.tasks.deal_mappers.visible_job_tracking.acquire_org_async_session",
        lambda organisation_id: _SessionSource(session),
    )

    tracker = VisibleDealJobTracker(
        uuid4(),
        str(uuid4()),
        DealJobType.DEAL_EMAIL_REMATCH,
        emit_events=True,
    )
    tracker.publisher = publisher

    await tracker.fail("boom")

    assert job.status == JobStatus.FAILED
    assert job.error_message == "boom"
    assert job.retry_count == 1
    assert session.commits == 1
    assert publisher.frames == [
        {"kind": "error", "message": "boom", "error_code": "job_failed"},
        {"kind": "finalize", "reason": "error"},
    ]


@pytest.mark.asyncio
async def test_internal_job_updates_status_without_publishing_events(monkeypatch) -> None:
    """A recovery job remains durable but silent unless its caller explicitly opted in."""
    job = _Job()
    session = _Session(job)
    monkeypatch.setattr(
        "src.workers.deal_worker.tasks.deal_mappers.visible_job_tracking.acquire_org_async_session",
        lambda organisation_id: _SessionSource(session),
    )

    tracker = VisibleDealJobTracker(
        uuid4(),
        str(uuid4()),
        DealJobType.STAKEHOLDER_SYNC,
    )
    await tracker.start("syncing", "Syncing stakeholders", 0.2)
    await tracker.complete({"status": "success"}, "saving", "Saving")

    assert job.status == JobStatus.COMPLETED
    assert session.commits == 2
    assert tracker.publisher is None
