"""Tests for DealMeetingJobService transcript verification and job creation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import src.service.crm.jobs.deal_meeting_job_service as job_service_module
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.meeting_intelligence import MeetingStatus
from src.service.crm.jobs.deal_meeting_job_service import (
    DealMeetingJobService,
    DealMeetingJobServiceError,
)
from src.workers.deal_worker.services.meeting_insight_services import (
    transcript_upload_service as upload_module,
)
from src.workers.deal_worker.services.meeting_insight_services.transcript_upload_service import (
    TranscriptUploadService,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transcript_upload_reuses_the_event_level_summary_job(monkeypatch) -> None:
    """Manual uploads must participate in the shared meeting summary lock."""
    organisation_id = uuid4()
    deal_id = uuid4()
    event_id = uuid4()
    active_job = DealJobs(
        organisation_id=organisation_id,
        user_id=uuid4(),
        job_type=DealJobType.MEETING_SUMMARY,
        deal_id=deal_id,
        event_id=event_id,
        status=JobStatus.PENDING,
    )

    class Session:
        def __init__(self) -> None:
            self.added: list[object] = []

        def add(self, value: object) -> None:
            self.added.append(value)

        async def commit(self) -> None:
            return None

        async def refresh(self, _value: object) -> None:
            return None

    session = Session()

    async def session_scope(_organisation_id):
        yield session

    lock_job = AsyncMock(return_value=active_job)
    enqueue = AsyncMock()
    monkeypatch.setattr(upload_module, "acquire_org_async_session", session_scope)
    monkeypatch.setattr(
        upload_module,
        "lock_and_get_active_meeting_summary_job",
        lock_job,
        raising=False,
    )
    monkeypatch.setattr(
        "src.workers.deal_worker.client.send_deal_task_async",
        enqueue,
    )
    service = TranscriptUploadService(
        organisation_id,
        deal_id,
        event_id,
        uuid4(),
        SimpleNamespace(),
    )

    summary_job_id = await service._trigger_meeting_summary()

    assert summary_job_id == active_job.job_id
    assert session.added == []
    lock_job.assert_awaited_once_with(session, organisation_id, event_id)
    enqueue.assert_awaited_once()


class _ScalarResult:
    """Wraps `.scalars().first()` chain for session.execute()."""

    def __init__(self, value):
        self._value = value

    def scalars(self):
        return self

    def first(self):
        return self._value


class _FakeSession:
    """Minimal async session stub for _verify_transcript_for_summary."""

    def __init__(self, *, event=None, meeting=None):
        self._event = event
        self._meeting = meeting
        self._execute_count = 0

    async def get(self, _model, _id):
        return self._event

    async def execute(self, _stmt):
        self._execute_count += 1
        if self._execute_count == 1:
            return _ScalarResult(self._event)
        return _ScalarResult(self._meeting)


def _make_meeting(status: str, *, transcript: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        full_transcript=transcript,
        status_message="fetch timed out" if status == MeetingStatus.FAILED else None,
    )


def _make_event() -> SimpleNamespace:
    return SimpleNamespace(event_id=uuid4())


# ---------------------------------------------------------------------------
# TestVerifyTranscriptForSummary
# ---------------------------------------------------------------------------


class TestVerifyTranscriptForSummary:
    """Only FETCHING blocks; all other states allow through."""

    @pytest.fixture
    def org_id(self):
        return uuid4()

    @pytest.fixture
    def user_id(self):
        return uuid4()

    @pytest.fixture
    def event_id(self):
        return uuid4()

    def _make_service(self, session, org_id, user_id):
        return DealMeetingJobService(session, org_id, user_id)

    # -- states that pass through (ensure_transcript handles fetch) --

    @pytest.mark.asyncio
    async def test_ready_with_transcript_passes(self, org_id, user_id, event_id):
        meeting = _make_meeting(MeetingStatus.READY, transcript="Hello world")
        session = _FakeSession(event=_make_event(), meeting=meeting)
        svc = self._make_service(session, org_id, user_id)

        await svc._verify_transcript_for_summary(event_id)  # no exception

    @pytest.mark.asyncio
    async def test_failed_status_allows_regeneration(self, org_id, user_id, event_id):
        meeting = _make_meeting(MeetingStatus.FAILED)
        session = _FakeSession(event=_make_event(), meeting=meeting)
        svc = self._make_service(session, org_id, user_id)

        await svc._verify_transcript_for_summary(event_id)  # no exception

    @pytest.mark.asyncio
    async def test_not_available_status_allows_regeneration(self, org_id, user_id, event_id):
        meeting = _make_meeting(MeetingStatus.NOT_AVAILABLE)
        session = _FakeSession(event=_make_event(), meeting=meeting)
        svc = self._make_service(session, org_id, user_id)

        await svc._verify_transcript_for_summary(event_id)  # no exception

    @pytest.mark.asyncio
    async def test_pending_status_passes(self, org_id, user_id, event_id):
        meeting = _make_meeting(MeetingStatus.PENDING)
        session = _FakeSession(event=_make_event(), meeting=meeting)
        svc = self._make_service(session, org_id, user_id)

        await svc._verify_transcript_for_summary(event_id)  # no exception

    @pytest.mark.asyncio
    async def test_processing_status_raises_409(self, org_id, user_id, event_id):
        meeting = _make_meeting(MeetingStatus.PROCESSING)
        session = _FakeSession(event=_make_event(), meeting=meeting)
        svc = self._make_service(session, org_id, user_id)

        with pytest.raises(DealMeetingJobServiceError) as exc_info:
            await svc._verify_transcript_for_summary(event_id)
        assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_no_meeting_record_passes(self, org_id, user_id, event_id):
        session = _FakeSession(event=_make_event(), meeting=None)
        svc = self._make_service(session, org_id, user_id)

        await svc._verify_transcript_for_summary(event_id)  # no exception

    @pytest.mark.asyncio
    async def test_ready_without_transcript_passes(self, org_id, user_id, event_id):
        meeting = _make_meeting(MeetingStatus.READY, transcript=None)
        session = _FakeSession(event=_make_event(), meeting=meeting)
        svc = self._make_service(session, org_id, user_id)

        await svc._verify_transcript_for_summary(event_id)  # no exception

    # -- only FETCHING blocks --

    @pytest.mark.asyncio
    async def test_fetching_status_raises_409(self, org_id, user_id, event_id):
        meeting = _make_meeting(MeetingStatus.FETCHING)
        session = _FakeSession(event=_make_event(), meeting=meeting)
        svc = self._make_service(session, org_id, user_id)

        with pytest.raises(DealMeetingJobServiceError) as exc_info:
            await svc._verify_transcript_for_summary(event_id)
        assert exc_info.value.status_code == 409
        assert "currently being fetched" in str(exc_info.value).lower()

    # -- event not found --

    @pytest.mark.asyncio
    async def test_event_not_found_raises_404(self, org_id, user_id, event_id):
        session = _FakeSession(event=None, meeting=None)
        svc = self._make_service(session, org_id, user_id)

        with pytest.raises(DealMeetingJobServiceError) as exc_info:
            await svc._verify_transcript_for_summary(event_id)
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# TestCreateMeetingJobTranscriptCheck
# ---------------------------------------------------------------------------


class TestCreateMeetingJobTranscriptCheck:
    """Verify that create_meeting_job only calls _verify_transcript_for_summary for summaries."""

    @pytest.fixture
    def org_id(self):
        return uuid4()

    @pytest.fixture
    def user_id(self):
        return uuid4()

    @pytest.fixture
    def deal_id(self):
        return uuid4()

    @pytest.fixture
    def event_id(self):
        return uuid4()

    @pytest.mark.asyncio
    async def test_active_job_returns_existing_without_verify(
        self, monkeypatch, org_id, user_id, deal_id, event_id
    ):
        """Active job found → return it immediately, skip transcript verify."""
        session = AsyncMock()
        svc = DealMeetingJobService(session, org_id, user_id)

        existing_job = SimpleNamespace(job_id=uuid4(), deal_id=deal_id)
        svc._require_active_association = AsyncMock()
        svc.get_active_job = AsyncMock(return_value=existing_job)
        svc._verify_transcript_for_summary = AsyncMock()
        locked_lookup = AsyncMock(return_value=existing_job)
        monkeypatch.setattr(
            job_service_module,
            "lock_and_get_active_meeting_summary_job",
            locked_lookup,
            raising=False,
        )

        result = await svc.create_meeting_job(deal_id, event_id, DealJobType.MEETING_SUMMARY)

        assert result == existing_job
        locked_lookup.assert_awaited_once_with(session, org_id, event_id)
        svc.get_active_job.assert_not_awaited()
        svc._verify_transcript_for_summary.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_association_is_required_before_active_job_reuse(
        self, org_id, user_id, deal_id, event_id
    ):
        """A same-org event cannot be processed under an unrelated deal context."""
        session = AsyncMock()
        svc = DealMeetingJobService(session, org_id, user_id)
        svc.get_active_job = AsyncMock(return_value=SimpleNamespace(job_id=uuid4()))
        svc._require_active_association = AsyncMock(
            side_effect=DealMeetingJobServiceError("Meeting not found", status_code=404)
        )

        with pytest.raises(DealMeetingJobServiceError) as exc_info:
            await svc.create_meeting_job(deal_id, event_id, DealJobType.MEETING_BRIEFING)

        assert exc_info.value.status_code == 404
        svc._require_active_association.assert_awaited_once_with(deal_id, event_id)
        svc.get_active_job.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_active_job_calls_verify_for_summary(
        self, monkeypatch, org_id, user_id, deal_id, event_id
    ):
        """No active job + summary type → verify transcript before creating."""
        session = AsyncMock()
        svc = DealMeetingJobService(session, org_id, user_id)

        svc._require_active_association = AsyncMock()
        svc.get_active_job = AsyncMock()
        svc._verify_transcript_for_summary = AsyncMock()
        svc.create_job = AsyncMock(return_value=SimpleNamespace(job_id=uuid4()))
        svc._has_completed_meeting_summary = AsyncMock(return_value=False)
        locked_lookup = AsyncMock(return_value=None)
        monkeypatch.setattr(
            job_service_module,
            "lock_and_get_active_meeting_summary_job",
            locked_lookup,
            raising=False,
        )

        await svc.create_meeting_job(deal_id, event_id, DealJobType.MEETING_SUMMARY)

        locked_lookup.assert_awaited_once_with(session, org_id, event_id)
        svc.get_active_job.assert_not_awaited()
        svc._verify_transcript_for_summary.assert_awaited_once_with(event_id)
        svc.create_job.assert_awaited_once_with(
            job_type=DealJobType.MEETING_SUMMARY,
            deal_id=deal_id,
            event_id=event_id,
            metadata={"event_id": str(event_id)},
        )

    @pytest.mark.asyncio
    async def test_summary_regeneration_marks_job_to_skip_pipeline(
        self, monkeypatch, org_id, user_id, deal_id, event_id
    ):
        """Existing completed summary should create a regeneration job that skips insight fan-out."""
        session = AsyncMock()
        svc = DealMeetingJobService(session, org_id, user_id)

        svc._require_active_association = AsyncMock()
        svc.get_active_job = AsyncMock()
        svc._verify_transcript_for_summary = AsyncMock()
        svc.create_job = AsyncMock(return_value=SimpleNamespace(job_id=uuid4()))
        svc._has_completed_meeting_summary = AsyncMock(return_value=True)
        locked_lookup = AsyncMock(return_value=None)
        monkeypatch.setattr(
            job_service_module,
            "lock_and_get_active_meeting_summary_job",
            locked_lookup,
            raising=False,
        )

        await svc.create_meeting_job(deal_id, event_id, DealJobType.MEETING_SUMMARY)

        svc.create_job.assert_awaited_once_with(
            job_type=DealJobType.MEETING_SUMMARY,
            deal_id=deal_id,
            event_id=event_id,
            metadata={
                "event_id": str(event_id),
                "trigger_type": "manual_regeneration",
                "skip_insight_pipeline": True,
            },
        )

    @pytest.mark.asyncio
    async def test_active_summary_for_another_deal_returns_conflict(
        self, monkeypatch, org_id, user_id, deal_id, event_id
    ):
        """A shared event job must not be returned through an incompatible deal SSE route."""
        session = AsyncMock()
        svc = DealMeetingJobService(session, org_id, user_id)
        existing_job = SimpleNamespace(job_id=uuid4(), deal_id=uuid4())
        svc._require_active_association = AsyncMock()
        svc.get_active_job = AsyncMock(return_value=existing_job)
        locked_lookup = AsyncMock(return_value=existing_job)
        monkeypatch.setattr(
            job_service_module,
            "lock_and_get_active_meeting_summary_job",
            locked_lookup,
            raising=False,
        )

        with pytest.raises(DealMeetingJobServiceError) as exc_info:
            await svc.create_meeting_job(deal_id, event_id, DealJobType.MEETING_SUMMARY)

        assert exc_info.value.status_code == 409
        locked_lookup.assert_awaited_once_with(session, org_id, event_id)

    @pytest.mark.asyncio
    async def test_briefing_job_skips_verify_transcript(self, org_id, user_id, deal_id, event_id):
        """Briefing jobs never call verify transcript."""
        session = AsyncMock()
        svc = DealMeetingJobService(session, org_id, user_id)

        svc._require_active_association = AsyncMock()
        svc._verify_transcript_for_summary = AsyncMock()
        svc.get_active_job = AsyncMock(return_value=None)
        svc.create_job = AsyncMock(return_value=SimpleNamespace(job_id=uuid4()))

        await svc.create_meeting_job(deal_id, event_id, DealJobType.MEETING_BRIEFING)

        svc._verify_transcript_for_summary.assert_not_awaited()
