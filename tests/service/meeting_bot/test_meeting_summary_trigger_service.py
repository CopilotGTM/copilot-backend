"""Tests for Recall transcript-ready meeting summary triggers."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.job_models import DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.calendar import GenerationStatus
from src.service.meeting_bot.summary_trigger_service import MeetingSummaryTriggerService


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _ExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _ScalarResult(self._rows)


class _SummaryJobSession:
    def __init__(self, execute_rows):
        self._execute_rows = list(execute_rows)
        self.added = []
        self.commits = 0

    async def execute(self, _stmt):
        rows = self._execute_rows.pop(0) if self._execute_rows else []
        return _ExecuteResult(rows)

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_handle_transcript_ready_returns_materialization_error(monkeypatch) -> None:
    organisation_id = uuid4()
    event_id = uuid4()
    service = MeetingSummaryTriggerService(organisation_id)

    class Result:
        success = False
        should_retry = False
        error = "Recall transcript artifact is missing download_url"

    async def fake_materialize(value):
        assert value == event_id
        return Result()

    monkeypatch.setattr(
        "src.service.meeting_bot.summary_trigger_service.acquire_task_lock",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "src.service.meeting_bot.summary_trigger_service.release_task_lock",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        service.transcript_service, "materialize_recall_transcript", fake_materialize
    )

    result = await service.handle_transcript_ready(event_id)

    assert result["transcript_materialized"] is False
    assert result["summary_jobs_created"] == 0
    assert result["summary_jobs_skipped"] == 0
    assert result["transcript_materialization_error"] == (
        "Recall transcript artifact is missing download_url"
    )


@pytest.mark.asyncio
async def test_created_summary_job_is_scoped_to_transcript_event(monkeypatch) -> None:
    organisation_id = uuid4()
    event_id = uuid4()
    job_id = uuid4()
    service = MeetingSummaryTriggerService(organisation_id)
    created_events = []
    enqueued_jobs = []

    class Result:
        success = True
        should_retry = False
        error = None

    async def fake_materialize(value):
        assert value == event_id
        return Result()

    async def fake_create_jobs(value):
        assert value == event_id
        created_events.append(value)
        return {"created": 1, "skipped": 0, "job_ids_to_enqueue": [job_id]}

    async def fake_send_task(task_name, args, queue, routing_key):
        enqueued_jobs.append((task_name, args, queue, routing_key))

    monkeypatch.setattr(
        "src.service.meeting_bot.summary_trigger_service.acquire_task_lock",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "src.service.meeting_bot.summary_trigger_service.release_task_lock",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        service.transcript_service, "materialize_recall_transcript", fake_materialize
    )
    monkeypatch.setattr(service, "_create_meeting_summary_jobs", fake_create_jobs)
    monkeypatch.setattr(
        "src.service.meeting_bot.summary_trigger_service.send_deal_task_async",
        fake_send_task,
    )

    result = await service.handle_transcript_ready(event_id)

    assert result["transcript_materialized"] is True
    assert result["summary_jobs_created"] == 1
    assert created_events == [event_id]
    assert enqueued_jobs == [
        (
            "meeting_summary_task.process",
            [str(organisation_id), str(job_id)],
            "deal_jobs",
            "deal_jobs",
        )
    ]


@pytest.mark.asyncio
async def test_create_summary_jobs_creates_non_deal_job_when_no_active_association(
    monkeypatch,
) -> None:
    """Recall transcripts should still generate a meeting summary when no deal is linked."""
    organisation_id = uuid4()
    event_id = uuid4()
    service = MeetingSummaryTriggerService(organisation_id)
    session = _SummaryJobSession(
        [
            [],  # active deal associations
            [],  # active non-deal summary jobs
        ]
    )

    async def fake_acquire_org_session(_organisation_id):
        yield session

    monkeypatch.setattr(
        "src.service.meeting_bot.summary_trigger_service.acquire_org_async_session",
        fake_acquire_org_session,
    )

    result = await service._create_meeting_summary_jobs(event_id)

    assert result["created"] == 1
    assert result["skipped"] == 0
    assert len(result["job_ids_to_enqueue"]) == 1
    assert len(session.added) == 1
    job = session.added[0]
    assert job.deal_id is None
    assert job.event_id == event_id
    assert job.job_type == DealJobType.MEETING_SUMMARY
    assert job.status == JobStatus.PENDING
    assert job.job_metadata["source"] == "recall_webhook"
    assert job.job_metadata["trigger"] == "transcript_done"
    assert job.job_metadata["send_summary_notification"] is True
    assert job.job_metadata["summary_scope"] == "meeting"


@pytest.mark.asyncio
async def test_create_summary_jobs_creates_one_canonical_job_for_multiple_deals(
    monkeypatch,
) -> None:
    """One calendar event must produce one summary job regardless of deal links."""
    organisation_id = uuid4()
    event_id = uuid4()
    service = MeetingSummaryTriggerService(organisation_id)
    associations = [
        SimpleNamespace(
            association_id=uuid4(),
            deal_id=uuid4(),
            meeting_summary_status=GenerationStatus.NOT_STARTED,
            meeting_summary_data=None,
        )
        for _ in range(2)
    ]

    class MultiDealSession(_SummaryJobSession):
        async def execute(self, statement):
            if "calendar_event_deal_associations" in str(statement):
                return _ExecuteResult(associations)
            return _ExecuteResult([])

    session = MultiDealSession([])

    async def fake_acquire_org_session(_organisation_id):
        yield session

    monkeypatch.setattr(
        "src.service.meeting_bot.summary_trigger_service.acquire_org_async_session",
        fake_acquire_org_session,
    )

    result = await service._create_meeting_summary_jobs(event_id)

    assert result["created"] == 1
    assert len(result["job_ids_to_enqueue"]) == 1
    assert len(session.added) == 1
    assert session.added[0].deal_id is None
    assert session.added[0].event_id == event_id
