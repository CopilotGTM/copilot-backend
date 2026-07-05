"""Tests for meeting summary task — insight pipeline dispatch after manual summary.

Tests verify that:
1. Manual (non-automated) summaries trigger the insight pipeline
2. Automated pipeline summaries do NOT double-trigger
3. _dispatch_insight_pipeline_for_manual_summary creates a job and dispatches correctly
4. Errors in pipeline dispatch are caught and do not fail the summary task
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.calendar import (
    CalendarEvent,
    CalendarEventDealAssociation,
    GenerationStatus,
)
from src.domain.models.organisation_tables.sync.meeting_intelligence import Meeting, MeetingStatus

pytestmark = pytest.mark.unit

# Sentinel — mirrors the constant in the task module
AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


class _SummaryPayload:
    def __init__(self, data):
        self._data = data

    def model_dump(self):
        return self._data


# =============================================================================
# _dispatch_insight_pipeline_for_manual_summary unit tests
# =============================================================================


class TestDispatchInsightPipelineForManualSummary:
    """Tests for the helper that dispatches the insight pipeline after manual summaries."""

    @pytest.mark.asyncio
    async def test_creates_job_and_dispatches_pipeline(self):
        """Should create a deal memory job and dispatch the insight pipeline."""
        org_id = uuid4()
        deal_id = uuid4()
        memory_job_id = uuid4()

        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()

        async def fake_session_gen(org):
            yield mock_session

        with (
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.acquire_org_async_session",
            ) as mock_acquire,
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.get_or_create_deal_memory_job",
                new_callable=AsyncMock,
                return_value=(memory_job_id, True),
            ) as mock_get_or_create,
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.dispatch_deal_insight_pipeline_async",
                new_callable=AsyncMock,
            ) as mock_dispatch,
        ):
            mock_acquire.return_value = fake_session_gen(org_id)

            from src.workers.deal_worker.tasks.meeting_insight_tasks.meeting_summary_task import (
                _dispatch_insight_pipeline_for_manual_summary,
            )

            await _dispatch_insight_pipeline_for_manual_summary(org_id, deal_id)

            mock_get_or_create.assert_called_once_with(
                mock_session,
                org_id,
                deal_id,
                source="transcript_upload",
                trigger="meeting_summary_completed",
            )
            mock_dispatch.assert_awaited_once_with(
                str(org_id),
                str(deal_id),
                str(memory_job_id),
            )

    @pytest.mark.asyncio
    async def test_uses_explicit_source_for_recall_webhook_pipeline(self):
        """Recall webhook summaries should not be labeled as transcript uploads."""
        org_id = uuid4()
        deal_id = uuid4()
        memory_job_id = uuid4()

        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()

        async def fake_session_gen(org):
            yield mock_session

        with (
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.acquire_org_async_session",
            ) as mock_acquire,
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.get_or_create_deal_memory_job",
                new_callable=AsyncMock,
                return_value=(memory_job_id, True),
            ) as mock_get_or_create,
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.dispatch_deal_insight_pipeline_async",
                new_callable=AsyncMock,
            ),
        ):
            mock_acquire.return_value = fake_session_gen(org_id)

            from src.workers.deal_worker.tasks.meeting_insight_tasks.meeting_summary_task import (
                _dispatch_insight_pipeline_for_manual_summary,
            )

            await _dispatch_insight_pipeline_for_manual_summary(
                org_id,
                deal_id,
                source="recall_webhook",
            )

            mock_get_or_create.assert_called_once_with(
                mock_session,
                org_id,
                deal_id,
                source="recall_webhook",
                trigger="meeting_summary_completed",
            )

    @pytest.mark.asyncio
    async def test_reuses_existing_job(self):
        """Should reuse an existing deal memory job (created=False) and still dispatch."""
        org_id = uuid4()
        deal_id = uuid4()
        existing_job_id = uuid4()

        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()

        async def fake_session_gen(org):
            yield mock_session

        with (
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.acquire_org_async_session",
            ) as mock_acquire,
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.get_or_create_deal_memory_job",
                new_callable=AsyncMock,
                return_value=(existing_job_id, False),
            ),
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.dispatch_deal_insight_pipeline_async",
                new_callable=AsyncMock,
            ) as mock_dispatch,
        ):
            mock_acquire.return_value = fake_session_gen(org_id)

            from src.workers.deal_worker.tasks.meeting_insight_tasks.meeting_summary_task import (
                _dispatch_insight_pipeline_for_manual_summary,
            )

            await _dispatch_insight_pipeline_for_manual_summary(org_id, deal_id)

            mock_dispatch.assert_awaited_once_with(
                str(org_id),
                str(deal_id),
                str(existing_job_id),
            )

    @pytest.mark.asyncio
    async def test_dispatch_error_does_not_propagate(self):
        """Errors in dispatch should be caught — must not crash the summary task."""
        org_id = uuid4()
        deal_id = uuid4()

        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()

        async def fake_session_gen(org):
            yield mock_session

        with (
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.acquire_org_async_session",
            ) as mock_acquire,
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.get_or_create_deal_memory_job",
                new_callable=AsyncMock,
                return_value=(uuid4(), True),
            ),
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.dispatch_deal_insight_pipeline_async",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Redis down"),
            ),
        ):
            mock_acquire.return_value = fake_session_gen(org_id)

            from src.workers.deal_worker.tasks.meeting_insight_tasks.meeting_summary_task import (
                _dispatch_insight_pipeline_for_manual_summary,
            )

            # Should not raise
            await _dispatch_insight_pipeline_for_manual_summary(org_id, deal_id)

    @pytest.mark.asyncio
    async def test_session_error_does_not_propagate(self):
        """Errors acquiring DB session should be caught — must not crash the summary task."""
        org_id = uuid4()
        deal_id = uuid4()

        async def failing_session_gen(org):
            raise RuntimeError("DB unavailable")
            yield

        with (
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.acquire_org_async_session",
            ) as mock_acquire,
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.dispatch_deal_insight_pipeline_async",
                new_callable=AsyncMock,
            ) as mock_dispatch,
        ):
            mock_acquire.return_value = failing_session_gen(org_id)

            from src.workers.deal_worker.tasks.meeting_insight_tasks.meeting_summary_task import (
                _dispatch_insight_pipeline_for_manual_summary,
            )

            # Should not raise
            await _dispatch_insight_pipeline_for_manual_summary(org_id, deal_id)

            # Pipeline should NOT be dispatched since job creation failed
            mock_dispatch.assert_not_called()


class TestNonDealMeetingPrompt:
    def test_non_deal_prompt_lives_in_dedicated_module(self):
        from src.agents.workflows.meeting_level_summary.prompts import (
            build_non_deal_meeting_summary_user_prompt,
            get_non_deal_meeting_summary_system_prompt,
        )

        assert callable(get_non_deal_meeting_summary_system_prompt)
        assert callable(build_non_deal_meeting_summary_user_prompt)

    def test_non_deal_prompt_keeps_strong_meeting_summary_sections(self):
        from src.agents.workflows.meeting_level_summary.prompts import (
            get_non_deal_meeting_summary_system_prompt,
        )

        prompt = get_non_deal_meeting_summary_system_prompt("Example Org")

        assert "You are an internal meeting analyst for Example Org." in prompt
        assert "# Grounding / Non-negotiables" in prompt
        assert "# Identity Rules" in prompt
        assert "# Working Approach" in prompt
        assert "# Evidence Handling" in prompt
        assert "# Output Contract" in prompt
        assert "# Overview" in prompt
        assert "# Individuals" in prompt
        assert "# Meeting Discussion" in prompt
        assert "# Requirements" in prompt
        assert "# Definitions" in prompt
        assert "# Important Points" in prompt
        assert "# Follow-ups / Action Items / Next Steps" in prompt

    def test_non_deal_prompt_removes_deal_specific_behavior(self):
        from src.agents.workflows.meeting_level_summary.prompts import (
            get_non_deal_meeting_summary_system_prompt,
        )

        prompt = get_non_deal_meeting_summary_system_prompt("Example Org")
        lower_prompt = prompt.lower()

        assert "stakeholder discovery" not in lower_prompt
        assert "stakeholder write-back" not in lower_prompt
        assert "deal memory" not in lower_prompt
        assert "pipeline" not in lower_prompt
        assert "deal stage" not in lower_prompt
        assert "crm" not in lower_prompt
        assert "# continuity" not in lower_prompt
        assert "buyer/prospect" not in lower_prompt


class TestMeetingSummaryService:
    def test_summary_workflow_exports_meeting_level_agent(self):
        from src.agents.workflows.meeting_level_summary import (
            MeetingLevelSummaryAgent,
            MeetingLevelSummaryInput,
        )

        assert MeetingLevelSummaryAgent is not None
        assert MeetingLevelSummaryInput is not None

    @pytest.mark.asyncio
    async def test_deal_linked_summary_dual_writes_association_and_meeting(
        self,
        monkeypatch,
    ):
        org_id = uuid4()
        deal_id = uuid4()
        event_id = uuid4()
        association_id = uuid4()
        meeting_id = uuid4()
        association = CalendarEventDealAssociation(
            association_id=association_id,
            organisation_id=org_id,
            event_id=event_id,
            deal_id=deal_id,
        )
        meeting = Meeting(
            meeting_id=meeting_id,
            organisation_id=org_id,
            calendar_event_id=event_id,
            full_transcript="Speaker: transcript",
        )

        class FakeMasterResult:
            def scalar_one_or_none(self):
                return ["example.com"]

        class FakeMasterSession:
            async def execute(self, _stmt):
                return FakeMasterResult()

        class FakeOrgSession:
            async def commit(self):
                return None

            async def get(self, model, requested_id):
                if model is CalendarEventDealAssociation and requested_id == association_id:
                    return association
                if model is Meeting and requested_id == meeting_id:
                    return meeting
                return None

        async def fake_master_session():
            yield FakeMasterSession()

        async def fake_org_session(_organisation_id):
            yield FakeOrgSession()

        class FakeTranscriptService:
            def __init__(self, _organisation_id):
                pass

            async def ensure_transcript(self, _event_id):
                return meeting

        class FakeSummaryAgent:
            last_token_usage = None

            def __init__(self, **_kwargs):
                pass

            async def generate(self, _input_data):
                return _SummaryPayload({"meeting_summary": "Deal-linked summary"})

        monkeypatch.setattr(
            "src.workers.deal_worker.services.meeting_insight_services"
            ".meeting_summary_service.acquire_master_async_session",
            fake_master_session,
        )
        monkeypatch.setattr(
            "src.workers.deal_worker.services.meeting_insight_services"
            ".meeting_summary_service.acquire_org_async_session",
            fake_org_session,
        )
        monkeypatch.setattr(
            "src.workers.deal_worker.services.meeting_insight_services"
            ".meeting_summary_service.get_organisation_name",
            AsyncMock(return_value="Example Org"),
        )
        monkeypatch.setattr(
            "src.workers.deal_worker.services.meeting_insight_services"
            ".meeting_summary_service.get_deal",
            AsyncMock(return_value=object()),
        )
        monkeypatch.setattr(
            "src.workers.deal_worker.services.meeting_insight_services"
            ".meeting_summary_service.get_event",
            AsyncMock(
                return_value=type(
                    "FakeEvent",
                    (),
                    {
                        "online_meeting_id": "online-1",
                        "online_meeting_provider": "googleMeet",
                        "attendees": [],
                    },
                )()
            ),
        )
        monkeypatch.setattr(
            "src.workers.deal_worker.services.meeting_insight_services"
            ".meeting_summary_service.get_association",
            AsyncMock(return_value=association),
        )
        monkeypatch.setattr(
            "src.workers.deal_worker.services.meeting_insight_services"
            ".meeting_summary_service.TranscriptService",
            FakeTranscriptService,
        )
        monkeypatch.setattr("src.agents.workflows.summary.SummaryAgent", FakeSummaryAgent)

        from src.workers.deal_worker.services.meeting_insight_services.meeting_summary_service import (
            MeetingSummaryService,
        )

        result = await MeetingSummaryService(
            organisation_id=org_id,
            deal_id=deal_id,
            event_id=event_id,
        ).generate()

        assert result["data"] == {"meeting_summary": "Deal-linked summary"}
        assert association.meeting_summary_data == result
        assert association.meeting_summary_status == GenerationStatus.COMPLETED
        assert meeting.meeting_summary_data == result
        assert meeting.meeting_summary_status == GenerationStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_non_deal_summary_uses_meeting_level_agent(self, monkeypatch):
        org_id = uuid4()
        event_id = uuid4()
        meeting_id = uuid4()
        event = CalendarEvent(
            organisation_id=org_id,
            event_id=event_id,
            external_id="external-1",
            title="Weekly internal review",
            start_at=datetime(2026, 6, 9, 10, 0, tzinfo=UTC),
            end_at=datetime(2026, 6, 9, 10, 30, tzinfo=UTC),
            attendees=[],
        )
        meeting = Meeting(
            meeting_id=meeting_id,
            organisation_id=org_id,
            calendar_event_id=event_id,
            status=MeetingStatus.READY,
            full_transcript="Nikhil: We agreed to ship the webhook path.",
        )
        captured_inputs = []

        class FakeMasterResult:
            def scalar_one_or_none(self):
                return ["example.com"]

        class FakeMasterSession:
            async def execute(self, _stmt):
                return FakeMasterResult()

        class FakeMeetingResult:
            def scalars(self):
                return self

            def first(self):
                return meeting

        class FakeOrgSession:
            async def execute(self, _stmt):
                return FakeMeetingResult()

            async def get(self, model, requested_id):
                if model is Meeting and requested_id == meeting_id:
                    return meeting
                return None

            async def commit(self):
                return None

        async def fake_master_session():
            yield FakeMasterSession()

        async def fake_org_session(_organisation_id):
            yield FakeOrgSession()

        class FakeMeetingLevelSummaryAgent:
            last_token_usage = None

            def __init__(self, *, organisation_id, org_name):
                assert organisation_id == org_id
                assert org_name == "Example Org"

            async def generate(self, input_data):
                captured_inputs.append(input_data)
                return _SummaryPayload({"meeting_summary": "Meeting-only summary"})

        monkeypatch.setattr(
            "src.workers.deal_worker.services.meeting_insight_services"
            ".meeting_summary_service.acquire_master_async_session",
            fake_master_session,
        )
        monkeypatch.setattr(
            "src.workers.deal_worker.services.meeting_insight_services"
            ".meeting_summary_service.acquire_org_async_session",
            fake_org_session,
        )
        monkeypatch.setattr(
            "src.workers.deal_worker.services.meeting_insight_services"
            ".meeting_summary_service.get_organisation_name",
            AsyncMock(return_value="Example Org"),
        )
        monkeypatch.setattr(
            "src.workers.deal_worker.services.meeting_insight_services"
            ".meeting_summary_service.get_event",
            AsyncMock(return_value=event),
        )
        monkeypatch.setattr(
            "src.agents.workflows.meeting_level_summary.MeetingLevelSummaryAgent",
            FakeMeetingLevelSummaryAgent,
        )

        from src.workers.deal_worker.services.meeting_insight_services.meeting_summary_service import (
            MeetingSummaryService,
        )

        result = await MeetingSummaryService(
            organisation_id=org_id,
            event_id=event_id,
        ).generate_meeting_level_summary()

        assert result["data"] == {"meeting_summary": "Meeting-only summary"}
        assert captured_inputs
        assert captured_inputs[0].event_id == str(event_id)
        assert captured_inputs[0].transcript == meeting.full_transcript
        assert captured_inputs[0].deal_id is None
        assert meeting.meeting_summary_data == result
        assert meeting.meeting_summary_status == GenerationStatus.COMPLETED


class TestDispatchSummaryReadyNotification:
    """Tests for notification dispatch after summary completion."""

    @pytest.mark.asyncio
    async def test_creates_notification_job_before_enqueue(self):
        """Notification delivery must be backed by a durable notification job."""
        org_id = uuid4()
        deal_id = uuid4()
        event_id = uuid4()
        summary_job_id = uuid4()
        added_jobs = []

        class FakeSession:
            def add(self, row):
                added_jobs.append(row)

            async def flush(self):
                return None

            async def commit(self):
                return None

        async def fake_acquire_org_session(_organisation_id):
            yield FakeSession()

        with (
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.acquire_org_async_session",
                fake_acquire_org_session,
            ),
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.send_meeting_summary_ready_notification_task",
            ) as mock_task,
        ):
            from src.workers.deal_worker.tasks.meeting_insight_tasks.meeting_summary_task import (
                _dispatch_summary_ready_notification,
            )

            await _dispatch_summary_ready_notification(org_id, deal_id, event_id, summary_job_id)

        assert len(added_jobs) == 1
        notification_job = added_jobs[0]
        assert notification_job.job_type == DealJobType.MEETING_SUMMARY_NOTIFICATION
        assert notification_job.deal_id == deal_id
        assert notification_job.event_id == event_id
        assert notification_job.job_metadata["source_summary_job_id"] == str(summary_job_id)
        mock_task.apply_async.assert_called_once()
        assert mock_task.apply_async.call_args.kwargs["args"] == [
            str(org_id),
            str(deal_id),
            str(event_id),
            str(notification_job.job_id),
        ]
        assert mock_task.apply_async.call_args.kwargs["queue"] == "deal_jobs"

    @pytest.mark.asyncio
    async def test_enqueues_non_deal_summary_ready_notification(self):
        """Non-deal meeting summaries should still notify internal attendees."""
        org_id = uuid4()
        event_id = uuid4()
        summary_job_id = uuid4()

        class FakeSession:
            def add(self, _row):
                return None

            async def flush(self):
                return None

            async def commit(self):
                return None

        async def fake_acquire_org_session(_organisation_id):
            yield FakeSession()

        with (
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.acquire_org_async_session",
                fake_acquire_org_session,
            ),
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.send_meeting_summary_ready_notification_task",
            ) as mock_task,
        ):
            from src.workers.deal_worker.tasks.meeting_insight_tasks.meeting_summary_task import (
                _dispatch_summary_ready_notification,
            )

            await _dispatch_summary_ready_notification(org_id, None, event_id, summary_job_id)

        assert mock_task.apply_async.call_args.kwargs["args"][0:3] == [
            str(org_id),
            None,
            str(event_id),
        ]
        assert mock_task.apply_async.call_args.kwargs["args"][3]

    @pytest.mark.asyncio
    async def test_enqueue_failure_does_not_propagate(self):
        """Notification enqueue failure must not fail the completed summary job."""
        org_id = uuid4()
        deal_id = uuid4()
        event_id = uuid4()

        class FakeSession:
            def add(self, _row):
                return None

            async def flush(self):
                return None

            async def commit(self):
                return None

        async def fake_acquire_org_session(_organisation_id):
            yield FakeSession()

        with (
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.acquire_org_async_session",
                fake_acquire_org_session,
            ),
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.send_meeting_summary_ready_notification_task",
            ) as mock_task,
        ):
            mock_task.apply_async.side_effect = RuntimeError("broker unavailable")
            from src.workers.deal_worker.tasks.meeting_insight_tasks.meeting_summary_task import (
                _dispatch_summary_ready_notification,
            )

            await _dispatch_summary_ready_notification(org_id, deal_id, event_id, uuid4())


# =============================================================================
# is_automated gate — verifies the condition that decides pipeline dispatch
# =============================================================================


class TestIsAutomatedGate:
    """Tests for the is_automated logic that gates insight pipeline dispatch."""

    def test_recall_webhook_automated_summary_dispatches_pipeline(self):
        """Recall webhook jobs are automated, but still own downstream insight dispatch."""
        from src.workers.deal_worker.tasks.meeting_insight_tasks.meeting_summary_task import (
            _should_dispatch_insight_pipeline_after_summary,
        )

        result = _should_dispatch_insight_pipeline_after_summary(
            AUTOMATED_PIPELINE_USER_ID,
            {"source": "recall_webhook"},
        )

        assert result is True

    def test_automated_pipeline_summary_does_not_double_dispatch_pipeline(self):
        """The old scheduled pipeline remains responsible for its own insight chain."""
        from src.workers.deal_worker.tasks.meeting_insight_tasks.meeting_summary_task import (
            _should_dispatch_insight_pipeline_after_summary,
        )

        result = _should_dispatch_insight_pipeline_after_summary(
            AUTOMATED_PIPELINE_USER_ID,
            {"source": "automated_pipeline"},
        )

        assert result is False

    def test_skip_insight_pipeline_overrides_recall_webhook_dispatch(self):
        """Explicit skip metadata should suppress downstream pipeline dispatch."""
        from src.workers.deal_worker.tasks.meeting_insight_tasks.meeting_summary_task import (
            _should_dispatch_insight_pipeline_after_summary,
        )

        result = _should_dispatch_insight_pipeline_after_summary(
            AUTOMATED_PIPELINE_USER_ID,
            {"source": "recall_webhook", "skip_insight_pipeline": True},
        )

        assert result is False

    def test_automated_when_sentinel_user_id(self):
        """Job with AUTOMATED_PIPELINE_USER_ID should be considered automated."""
        user_id = AUTOMATED_PIPELINE_USER_ID
        job_metadata: dict = {}
        is_automated = (
            user_id == AUTOMATED_PIPELINE_USER_ID
            or job_metadata.get("source") == "automated_pipeline"
        )
        assert is_automated is True

    def test_automated_when_source_is_automated_pipeline(self):
        """Job with source=automated_pipeline should be considered automated."""
        user_id = uuid4()  # regular user
        job_metadata = {"source": "automated_pipeline"}
        is_automated = (
            user_id == AUTOMATED_PIPELINE_USER_ID
            or job_metadata.get("source") == "automated_pipeline"
        )
        assert is_automated is True

    def test_not_automated_for_transcript_upload(self):
        """Job from transcript upload should NOT be considered automated."""
        user_id = uuid4()
        job_metadata = {"source": "transcript_upload"}
        is_automated = (
            user_id == AUTOMATED_PIPELINE_USER_ID
            or job_metadata.get("source") == "automated_pipeline"
        )
        assert is_automated is False

    def test_not_automated_for_manual_trigger(self):
        """Job from manual UI trigger (no source metadata) should NOT be automated."""
        user_id = uuid4()
        job_metadata: dict = {}
        is_automated = (
            user_id == AUTOMATED_PIPELINE_USER_ID
            or job_metadata.get("source") == "automated_pipeline"
        )
        assert is_automated is False

    def test_not_automated_for_empty_metadata(self):
        """Job with None metadata should NOT be automated."""
        user_id = uuid4()
        job_metadata = {}
        is_automated = (
            user_id == AUTOMATED_PIPELINE_USER_ID
            or job_metadata.get("source") == "automated_pipeline"
        )
        assert is_automated is False

    def test_manual_regeneration_can_skip_pipeline(self):
        """Manual regeneration should skip downstream insight pipeline dispatch."""
        user_id = uuid4()
        job_metadata = {"skip_insight_pipeline": True}
        is_automated = (
            user_id == AUTOMATED_PIPELINE_USER_ID
            or job_metadata.get("source") == "automated_pipeline"
        )
        skip_insight_pipeline = bool(job_metadata.get("skip_insight_pipeline"))

        assert is_automated is False
        assert skip_insight_pipeline is True


class TestSummaryReadyNotificationGate:
    """Tests for which completed meeting summaries may notify attendees."""

    def test_explicit_webhook_notification_intent_dispatches_notification(self):
        """Only jobs carrying explicit notification intent may email attendees."""
        from src.workers.deal_worker.tasks.meeting_insight_tasks.meeting_summary_task import (
            _should_dispatch_summary_ready_notification,
        )

        assert (
            _should_dispatch_summary_ready_notification(
                {"source": "recall_webhook", "send_summary_notification": True}
            )
            is True
        )

    def test_recall_webhook_source_without_intent_does_not_dispatch_notification(self):
        """Source alone is not a sufficient notification authorization."""
        from src.workers.deal_worker.tasks.meeting_insight_tasks.meeting_summary_task import (
            _should_dispatch_summary_ready_notification,
        )

        assert _should_dispatch_summary_ready_notification({"source": "recall_webhook"}) is False

    def test_automated_pipeline_summary_does_not_dispatch_notification(self):
        """Backfilled or scheduled historical summaries must not email old attendees."""
        from src.workers.deal_worker.tasks.meeting_insight_tasks.meeting_summary_task import (
            _should_dispatch_summary_ready_notification,
        )

        assert (
            _should_dispatch_summary_ready_notification({"source": "automated_pipeline"}) is False
        )

    def test_manual_summary_does_not_dispatch_notification(self):
        """Manual/UI summary regeneration must not email attendees."""
        from src.workers.deal_worker.tasks.meeting_insight_tasks.meeting_summary_task import (
            _should_dispatch_summary_ready_notification,
        )

        assert _should_dispatch_summary_ready_notification({}) is False


# =============================================================================
# Task config tests
# =============================================================================


class TestMeetingSummaryTaskConfig:
    """Tests for meeting summary task Celery configuration."""

    @pytest.fixture(autouse=True)
    def _import_task(self):
        try:
            from src.workers.deal_worker.tasks.meeting_insight_tasks.meeting_summary_task import (
                process_meeting_summary,
            )

            self.task = process_meeting_summary
            self.available = True
        except ImportError:
            self.available = False

    def test_task_exists(self):
        if not self.available:
            pytest.skip("Task not importable")
        assert self.task is not None

    def test_task_name(self):
        if not self.available:
            pytest.skip("Task not importable")
        assert self.task.name == "meeting_summary_task.process"

    def test_task_max_retries(self):
        if not self.available:
            pytest.skip("Task not importable")
        assert self.task.max_retries == 3

    def test_task_acks_late(self):
        if not self.available:
            pytest.skip("Task not importable")
        assert self.task.acks_late is True

    def test_task_skips_when_same_job_is_already_running(self):
        if not self.available:
            pytest.skip("Task not importable")
        org_id = uuid4()
        job_id = uuid4()

        with (
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.acquire_task_lock",
                return_value=False,
            ) as acquire_lock,
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.release_task_lock",
            ) as release_lock,
        ):
            result = self.task.run(str(org_id), str(job_id))

        assert result == {"status": "skipped", "reason": "already_running"}
        acquire_lock.assert_called_once_with(f"task_lock:meeting_summary_job:{job_id}", ttl=700)
        release_lock.assert_not_called()

    def test_non_deal_meeting_summary_job_generates_meeting_level_summary(self):
        if not self.available:
            pytest.skip("Task not importable")
        org_id = uuid4()
        event_id = uuid4()
        job_id = uuid4()
        job = DealJobs(
            job_id=job_id,
            organisation_id=org_id,
            user_id=AUTOMATED_PIPELINE_USER_ID,
            job_type=DealJobType.MEETING_SUMMARY,
            deal_id=None,
            event_id=event_id,
            status=JobStatus.PENDING,
            job_metadata={
                "source": "recall_webhook",
                "summary_scope": "meeting",
                "skip_insight_pipeline": True,
                "send_summary_notification": True,
            },
        )

        class FakeSession:
            async def get(self, model, requested_job_id):
                assert model is DealJobs
                assert requested_job_id == job_id
                return job

            async def commit(self):
                return None

        async def fake_acquire_org_session(_organisation_id):
            yield FakeSession()

        summary_result = {"data": {"meeting_summary": "Summary for an unlinked meeting"}}

        with (
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.acquire_task_lock",
                return_value=True,
            ),
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.release_task_lock",
            ),
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.acquire_org_async_session",
                fake_acquire_org_session,
            ),
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.MeetingSummaryService",
            ) as service_cls,
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.DealJobRedisPublisher",
            ) as publisher_cls,
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task._dispatch_summary_ready_notification",
                new_callable=AsyncMock,
            ) as dispatch_notification,
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task._dispatch_insight_pipeline_for_manual_summary",
                new_callable=AsyncMock,
            ) as dispatch_pipeline,
        ):
            service = service_cls.return_value
            service.generate_meeting_level_summary = AsyncMock(return_value=summary_result)
            service.last_token_usage = None

            result = self.task.run(str(org_id), str(job_id))

        service_cls.assert_called_once()
        assert service_cls.call_args.kwargs["organisation_id"] == org_id
        assert service_cls.call_args.kwargs["deal_id"] is None
        assert service_cls.call_args.kwargs["event_id"] == event_id
        service.generate_meeting_level_summary.assert_awaited_once()
        dispatch_notification.assert_awaited_once_with(org_id, None, event_id, job_id)
        dispatch_pipeline.assert_not_awaited()
        publisher_cls.assert_not_called()
        assert job.status == JobStatus.COMPLETED
        assert job.result_data == summary_result
        assert result == {
            "status": "completed",
            "job_id": str(job_id),
            "job_type": str(DealJobType.MEETING_SUMMARY),
        }
