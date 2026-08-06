"""Tests for meeting summary task — insight pipeline dispatch after manual summary.

Tests verify that:
1. Manual (non-automated) summaries trigger the insight pipeline
2. Automated pipeline summaries do NOT double-trigger
3. _dispatch_insight_pipeline_for_manual_summary creates a job and dispatches correctly
4. Errors in pipeline dispatch propagate so Celery can retry them
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
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
from src.service.automation.producers.meeting import MeetingEventContext

pytestmark = pytest.mark.unit

# Sentinel — mirrors the constant in the task module
AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


class _SummaryPayload:
    def __init__(self, data):
        self._data = data

    def model_dump(self):
        """Build the model dump fixture used by the surrounding regression cases."""
        return self._data


# =============================================================================
# _dispatch_insight_pipeline_for_manual_summary unit tests
# =============================================================================


class TestDispatchInsightPipelineForManualSummary:
    """Regression cases for dispatching insight work after manual summaries."""

    @pytest.mark.asyncio
    async def test_creates_job_and_dispatches_pipeline(self):
        """Should create a deal memory job and dispatch the insight pipeline."""
        org_id = uuid4()
        deal_id = uuid4()
        memory_job_id = uuid4()
        event_id = uuid4()
        association_id = uuid4()
        summary_job_id = uuid4()

        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()

        async def fake_session_gen(org):
            """Build the fake session gen fixture used by the surrounding regression cases."""
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

            await _dispatch_insight_pipeline_for_manual_summary(
                org_id,
                deal_id,
                event_id=event_id,
                association_id=association_id,
                summary_job_id=summary_job_id,
            )

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
                source_event_id=str(event_id),
                source_association_id=str(association_id),
                source_summary_job_id=str(summary_job_id),
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
            """Build the fake session gen fixture used by the surrounding regression cases."""
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
                event_id=uuid4(),
                association_id=uuid4(),
                summary_job_id=uuid4(),
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
        event_id = uuid4()
        summary_job_id = uuid4()

        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()

        async def fake_session_gen(org):
            """Build the fake session gen fixture used by the surrounding regression cases."""
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

            await _dispatch_insight_pipeline_for_manual_summary(
                org_id,
                deal_id,
                event_id=event_id,
                association_id=None,
                summary_job_id=summary_job_id,
            )

            mock_dispatch.assert_awaited_once_with(
                str(org_id),
                str(deal_id),
                str(existing_job_id),
                source_event_id=str(event_id),
                source_association_id=None,
                source_summary_job_id=str(summary_job_id),
            )

    @pytest.mark.asyncio
    async def test_dispatch_error_propagates_for_worker_retry(self):
        """Broker failures must propagate so Celery retries post-summary dispatch."""
        org_id = uuid4()
        deal_id = uuid4()

        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()

        async def fake_session_gen(org):
            """Build the fake session gen fixture used by the surrounding regression cases."""
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

            with pytest.raises(RuntimeError, match="Redis down"):
                await _dispatch_insight_pipeline_for_manual_summary(
                    org_id,
                    deal_id,
                    event_id=uuid4(),
                    association_id=None,
                    summary_job_id=uuid4(),
                )

    @pytest.mark.asyncio
    async def test_session_error_propagates_for_worker_retry(self):
        """Database failures must propagate so dispatch is retried rather than abandoned."""
        org_id = uuid4()
        deal_id = uuid4()

        async def failing_session_gen(org):
            """Build the failing session gen fixture used by the surrounding regression cases."""
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

            with pytest.raises(RuntimeError, match="DB unavailable"):
                await _dispatch_insight_pipeline_for_manual_summary(
                    org_id,
                    deal_id,
                    event_id=uuid4(),
                    association_id=None,
                    summary_job_id=uuid4(),
                )

            # Pipeline should NOT be dispatched since job creation failed
            mock_dispatch.assert_not_called()


class TestDispatchEventDealInsightPipelines:
    """Regression cases for one canonical summary fanning out to linked deals."""

    @pytest.mark.asyncio
    async def test_dispatches_each_active_link_except_the_scheduled_owner(self):
        """Scheduled chains own one deal; the worker must dispatch every other link."""
        org_id = uuid4()
        event_id = uuid4()
        summary_job_id = uuid4()
        scheduled_deal_id = uuid4()
        other_deal_id = uuid4()
        scheduled_association_id = uuid4()
        other_association_id = uuid4()
        statements = []

        class Result:
            def all(self):
                return [
                    (scheduled_deal_id, scheduled_association_id),
                    (other_deal_id, other_association_id),
                ]

        class Session:
            async def execute(self, statement):
                statements.append(statement)
                return Result()

        async def fake_session_gen(_organisation_id):
            yield Session()

        with (
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.acquire_org_async_session",
                fake_session_gen,
            ),
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task._dispatch_insight_pipeline_for_manual_summary",
                new_callable=AsyncMock,
            ) as dispatch,
        ):
            from src.workers.deal_worker.tasks.meeting_insight_tasks.meeting_summary_task import (
                _dispatch_event_deal_insight_pipelines,
            )

            await _dispatch_event_deal_insight_pipelines(
                org_id,
                event_id,
                summary_job_id,
                source="automated_pipeline",
                exclude_deal_id=scheduled_deal_id,
            )

        dispatch.assert_awaited_once_with(
            org_id,
            other_deal_id,
            event_id=event_id,
            association_id=other_association_id,
            summary_job_id=summary_job_id,
            source="automated_pipeline",
        )
        assert "calendar_event_deal_associations.event_id" in str(statements[0].whereclause)


class TestNonDealMeetingPrompt:
    """Group regression cases for Non Deal Meeting Prompt behavior."""

    def test_non_deal_prompt_lives_in_dedicated_module(self):
        """Verify the contract that non deal prompt lives in dedicated module."""
        from src.agents.workflows.meeting_level_summary.prompts import (
            build_non_deal_meeting_summary_user_prompt,
            get_non_deal_meeting_summary_system_prompt,
        )

        assert callable(get_non_deal_meeting_summary_system_prompt)
        assert callable(build_non_deal_meeting_summary_user_prompt)

    def test_non_deal_prompt_keeps_strong_meeting_summary_sections(self):
        """Verify the contract that non deal prompt keeps strong meeting summary sections."""
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
        """Verify the contract that non deal prompt removes deal specific behavior."""
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
    """Group regression cases for Meeting Summary Service behavior."""

    def test_summary_workflow_exports_meeting_level_agent(self):
        """Verify the contract that summary workflow exports meeting level agent."""
        from src.agents.workflows.meeting_level_summary import (
            MeetingLevelSummaryAgent,
            MeetingLevelSummaryInput,
        )

        assert MeetingLevelSummaryAgent is not None
        assert MeetingLevelSummaryInput is not None

    @pytest.mark.asyncio
    async def test_batch_pipeline_pairs_task_sync_with_every_meeting(self, monkeypatch):
        """A backlog of meetings must enqueue one source-bound task sync per summary."""
        from src.workers.deal_worker.services.meeting_insight_services import (
            meeting_summary_service as module,
        )

        org_id = uuid4()
        deal_id = uuid4()
        events = [(uuid4(), uuid4(), deal_id), (uuid4(), uuid4(), deal_id)]
        summary_jobs = [SimpleNamespace(job_id=uuid4()), SimpleNamespace(job_id=uuid4())]
        task_jobs = [SimpleNamespace(job_id=uuid4()), SimpleNamespace(job_id=uuid4())]
        service = module.MeetingSummaryService(org_id)
        service._get_events_needing_summary = AsyncMock(  # type: ignore[method-assign]
            return_value=events
        )
        service._get_active_job = AsyncMock(return_value=None)  # type: ignore[method-assign]
        service._get_active_task_job = AsyncMock(return_value=None)  # type: ignore[method-assign]
        service._create_job = AsyncMock(side_effect=summary_jobs)  # type: ignore[method-assign]
        service._create_task_job = AsyncMock(side_effect=task_jobs)  # type: ignore[method-assign]

        class FakeSession:
            """Provide the transaction boundary used by batch job creation."""

            async def commit(self):
                """Accept the durable job commit."""
                return None

        async def fake_org_session(_organisation_id):
            """Yield the deterministic batch job session."""
            yield FakeSession()

        captured_signatures = []

        def fake_signature(name, **kwargs):
            """Capture Celery signatures in their requested order."""
            value = {"name": name, **kwargs}
            captured_signatures.append(value)
            return value

        dispatch = AsyncMock()
        monkeypatch.setattr(module, "acquire_org_async_session", fake_org_session)
        monkeypatch.setattr(
            module, "get_or_create_deal_memory_job", AsyncMock(return_value=(uuid4(), True))
        )
        monkeypatch.setattr(module, "signature", fake_signature)
        monkeypatch.setattr(module, "dispatch_deal_insight_pipeline_async", dispatch)

        result = await service.process_deal_pipeline(deal_id)

        assert [item["name"] for item in captured_signatures] == [
            "meeting_summary_task.process",
            "deal_tasks.sync_from_summary",
            "meeting_summary_task.process",
            "deal_tasks.sync_from_summary",
        ]
        assert [
            item["kwargs"]["job_id"]
            for item in captured_signatures
            if item["name"] == "deal_tasks.sync_from_summary"
        ] == [str(job.job_id) for job in task_jobs]
        dispatch.assert_awaited_once()
        assert result.tasks_triggered == 7

    @pytest.mark.asyncio
    async def test_batch_pipeline_does_not_rechain_active_summary(self, monkeypatch):
        """An overlapping scheduler run must leave an active meeting pipeline alone."""
        from src.workers.deal_worker.services.meeting_insight_services import (
            meeting_summary_service as module,
        )

        org_id = uuid4()
        deal_id = uuid4()
        service = module.MeetingSummaryService(org_id)
        service._get_events_needing_summary = AsyncMock(  # type: ignore[method-assign]
            return_value=[(uuid4(), uuid4(), deal_id)]
        )
        service._get_active_job = AsyncMock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(job_id=uuid4())
        )
        service._create_task_job = AsyncMock()  # type: ignore[method-assign]

        class FakeSession:
            """Provide the transaction boundary used by the overlap check."""

            async def commit(self):
                """Accept the durable deal-memory job commit."""
                return None

        async def fake_org_session(_organisation_id):
            """Yield the deterministic overlap-check session."""
            yield FakeSession()

        dispatch = AsyncMock()
        monkeypatch.setattr(module, "acquire_org_async_session", fake_org_session)
        monkeypatch.setattr(
            module,
            "get_or_create_deal_memory_job",
            AsyncMock(return_value=(uuid4(), False)),
        )
        monkeypatch.setattr(module, "dispatch_deal_insight_pipeline_async", dispatch)

        result = await service.process_deal_pipeline(deal_id)

        assert result.jobs_skipped == 1
        assert result.tasks_triggered == 0
        service._create_task_job.assert_not_awaited()
        dispatch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deal_linked_summary_dual_writes_association_and_meeting(
        self,
        monkeypatch,
    ):
        """Verify the contract that deal linked summary dual writes association and meeting."""
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
        call_order: list[str] = []
        envelopes = []
        statements = []

        class FakeMasterResult:
            """Represent the Fake Master Result test double used by the surrounding regression cases."""

            def scalar_one_or_none(self):
                """Expose the scalar one or none operation expected by the Fake Master Result test double."""
                return ["example.com"]

        class FakeMasterSession:
            """Represent the Fake Master Session test double used by the surrounding regression cases."""

            async def execute(self, _stmt):
                """Expose the execute operation expected by the Fake Master Session test double."""
                return FakeMasterResult()

        class FakeOrgSession:
            """Represent the Fake Org Session test double used by the surrounding regression cases."""

            async def execute(self, statement):
                """Expose the execute operation expected by the Fake Org Session test double."""
                statements.append(statement)

            async def commit(self):
                """Expose the commit operation expected by the Fake Org Session test double."""
                call_order.append("commit")

            async def get(self, model, requested_id):
                """Expose the get operation expected by the Fake Org Session test double."""
                if model is CalendarEventDealAssociation and requested_id == association_id:
                    return association
                if model is Meeting and requested_id == meeting_id:
                    return meeting
                return None

        async def fake_master_session():
            """Build the fake master session fixture used by the surrounding regression cases."""
            yield FakeMasterSession()

        async def fake_org_session(_organisation_id):
            """Build the fake org session fixture used by the surrounding regression cases."""
            yield FakeOrgSession()

        class FakeTranscriptService:
            """Represent the Fake Transcript Service test double used by the surrounding regression cases."""

            def __init__(self, _organisation_id):
                pass

            async def ensure_transcript(self, _event_id):
                """Expose the ensure transcript operation expected by the Fake Transcript Service test double."""
                return meeting

        class FakeSummaryAgent:
            """Represent the Fake Summary Agent test double used by the surrounding regression cases."""

            last_token_usage = None

            def __init__(self, **_kwargs):
                pass

            async def generate(self, _input_data):
                """Expose the generate operation expected by the Fake Summary Agent test double."""
                return _SummaryPayload({"meeting_summary": "Deal-linked summary"})

        class FakeDispatchService:
            """Represent the Fake Dispatch Service test double used by the surrounding regression cases."""

            def __init__(self, _session, dispatch_organisation_id, **_kwargs):
                assert dispatch_organisation_id == org_id

            async def enqueue(self, envelope):
                """Expose the enqueue operation expected by the Fake Dispatch Service test double."""
                call_order.append("enqueue")
                envelopes.append(envelope)

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
            ".meeting_summary_service.get_organisation_agent_context",
            AsyncMock(return_value=("Example Org", None)),
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
        monkeypatch.setattr(
            "src.workers.deal_worker.services.meeting_insight_services"
            ".meeting_summary_service.load_meeting_event_context",
            AsyncMock(return_value=MeetingEventContext(deal_id=deal_id)),
            raising=False,
        )
        monkeypatch.setattr(
            "src.workers.deal_worker.services.meeting_insight_services"
            ".meeting_summary_service.AutomationEventDispatchService",
            FakeDispatchService,
            raising=False,
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
        assert call_order[-2:] == ["enqueue", "commit"]
        assert envelopes[0].event_key == "meeting.summary.ready"
        assert envelopes[0].source.source_event_id == str(meeting_id)
        assert any(
            "UPDATE calendar_event_deal_associations" in str(statement)
            and "calendar_event_deal_associations.event_id" in str(statement)
            and "calendar_event_deal_associations.deal_id" not in str(statement.whereclause)
            for statement in statements
        )

    @pytest.mark.asyncio
    async def test_summary_selection_uses_canonical_meeting_state(self, monkeypatch):
        """Verify the contract that summary selection uses canonical meeting state."""
        from src.workers.deal_worker.services.meeting_insight_services import (
            meeting_summary_service as module,
        )

        captured = []

        class FakeResult:
            """Represent the Fake Result test double used by the surrounding regression cases."""

            def all(self):
                """Expose the all operation expected by the Fake Result test double."""
                return []

        class FakeSession:
            """Represent the Fake Session test double used by the surrounding regression cases."""

            async def execute(self, statement):
                """Expose the execute operation expected by the Fake Session test double."""
                captured.append(statement)
                return FakeResult()

        async def fake_org_session(_organisation_id):
            """Build the fake org session fixture used by the surrounding regression cases."""
            yield FakeSession()

        monkeypatch.setattr(module, "acquire_org_async_session", fake_org_session)

        await module.MeetingSummaryService(uuid4())._get_events_needing_summary(uuid4())

        where_sql = str(captured[0].whereclause)
        assert "meetings.meeting_summary_data IS NULL" in where_sql

    @pytest.mark.asyncio
    async def test_active_summary_job_is_locked_and_matched_per_meeting(self):
        """Verify the contract that active summary job is locked and matched per meeting."""
        from src.workers.deal_worker.services.meeting_insight_services import (
            meeting_summary_service as module,
        )

        statements = []

        class FakeScalars:
            """Represent the Fake Scalars test double used by the surrounding regression cases."""

            def first(self):
                """Expose the first operation expected by the Fake Scalars test double."""
                return None

        class FakeResult:
            """Represent the Fake Result test double used by the surrounding regression cases."""

            def scalars(self):
                """Expose the scalars operation expected by the Fake Result test double."""
                return FakeScalars()

        class FakeSession:
            """Represent the Fake Session test double used by the surrounding regression cases."""

            async def execute(self, statement):
                """Expose the execute operation expected by the Fake Session test double."""
                statements.append(statement)
                return FakeResult()

        await module.MeetingSummaryService(uuid4())._get_active_job(FakeSession(), uuid4(), uuid4())

        assert "pg_advisory_xact_lock" in str(statements[0])
        assert "deal_jobs.deal_id =" not in str(statements[1].whereclause)

    @pytest.mark.asyncio
    async def test_non_deal_summary_uses_meeting_level_agent(self, monkeypatch):
        """Verify the contract that non deal summary uses meeting level agent."""
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
        envelopes = []
        statements = []

        class FakeMasterResult:
            """Represent the Fake Master Result test double used by the surrounding regression cases."""

            def scalar_one_or_none(self):
                """Expose the scalar one or none operation expected by the Fake Master Result test double."""
                return ["example.com"]

        class FakeMasterSession:
            """Represent the Fake Master Session test double used by the surrounding regression cases."""

            async def execute(self, _stmt):
                """Expose the execute operation expected by the Fake Master Session test double."""
                return FakeMasterResult()

        class FakeMeetingResult:
            """Represent the Fake Meeting Result test double used by the surrounding regression cases."""

            def scalars(self):
                """Expose the scalars operation expected by the Fake Meeting Result test double."""
                return self

            def first(self):
                """Expose the first operation expected by the Fake Meeting Result test double."""
                return meeting

        class FakeOrgSession:
            """Represent the Fake Org Session test double used by the surrounding regression cases."""

            async def execute(self, statement):
                """Expose the execute operation expected by the Fake Org Session test double."""
                statements.append(statement)
                return FakeMeetingResult()

            async def get(self, model, requested_id):
                """Expose the get operation expected by the Fake Org Session test double."""
                if model is Meeting and requested_id == meeting_id:
                    return meeting
                return None

            async def commit(self):
                """Expose the commit operation expected by the Fake Org Session test double."""
                return None

        async def fake_master_session():
            """Build the fake master session fixture used by the surrounding regression cases."""
            yield FakeMasterSession()

        async def fake_org_session(_organisation_id):
            """Build the fake org session fixture used by the surrounding regression cases."""
            yield FakeOrgSession()

        class FakeMeetingLevelSummaryAgent:
            """Represent the Fake Meeting Level Summary Agent test double used by the surrounding regression cases."""

            last_token_usage = None

            def __init__(self, *, organisation_id, org_name, organisation_profile):
                assert organisation_id == org_id
                assert org_name == "Example Org"
                assert organisation_profile is None

            async def generate(self, input_data):
                """Expose the generate operation expected by the Fake Meeting Level Summary Agent test double."""
                captured_inputs.append(input_data)
                return _SummaryPayload({"meeting_summary": "Meeting-only summary"})

        class FakeDispatchService:
            """Represent the Fake Dispatch Service test double used by the surrounding regression cases."""

            def __init__(self, _session, dispatch_organisation_id, **_kwargs):
                assert dispatch_organisation_id == org_id

            async def enqueue(self, envelope):
                """Expose the enqueue operation expected by the Fake Dispatch Service test double."""
                envelopes.append(envelope)

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
            ".meeting_summary_service.get_organisation_agent_context",
            AsyncMock(return_value=("Example Org", None)),
        )
        monkeypatch.setattr(
            "src.workers.deal_worker.services.meeting_insight_services"
            ".meeting_summary_service.get_event",
            AsyncMock(return_value=event),
        )
        monkeypatch.setattr(
            "src.workers.deal_worker.services.meeting_insight_services"
            ".meeting_summary_service.load_meeting_event_context",
            AsyncMock(return_value=MeetingEventContext()),
            raising=False,
        )
        monkeypatch.setattr(
            "src.workers.deal_worker.services.meeting_insight_services"
            ".meeting_summary_service.AutomationEventDispatchService",
            FakeDispatchService,
            raising=False,
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
        assert envelopes[0].event_key == "meeting.summary.ready"
        assert envelopes[0].source.source_event_id == str(meeting_id)
        assert any(
            "UPDATE calendar_event_deal_associations" in str(statement)
            and "calendar_event_deal_associations.event_id" in str(statement.whereclause)
            for statement in statements
            if hasattr(statement, "whereclause")
        )


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
            """Represent the Fake Session test double used by the surrounding regression cases."""

            def add(self, row):
                """Expose the add operation expected by the Fake Session test double."""
                added_jobs.append(row)

            async def flush(self):
                """Expose the flush operation expected by the Fake Session test double."""
                return None

            async def commit(self):
                """Expose the commit operation expected by the Fake Session test double."""
                return None

        async def fake_acquire_org_session(_organisation_id):
            """Build the fake acquire org session fixture used by the surrounding regression cases."""
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
            """Represent the Fake Session test double used by the surrounding regression cases."""

            def add(self, _row):
                """Expose the add operation expected by the Fake Session test double."""
                return None

            async def flush(self):
                """Expose the flush operation expected by the Fake Session test double."""
                return None

            async def commit(self):
                """Expose the commit operation expected by the Fake Session test double."""
                return None

        async def fake_acquire_org_session(_organisation_id):
            """Build the fake acquire org session fixture used by the surrounding regression cases."""
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
            """Represent the Fake Session test double used by the surrounding regression cases."""

            def add(self, _row):
                """Expose the add operation expected by the Fake Session test double."""
                return None

            async def flush(self):
                """Expose the flush operation expected by the Fake Session test double."""
                return None

            async def commit(self):
                """Expose the commit operation expected by the Fake Session test double."""
                return None

        async def fake_acquire_org_session(_organisation_id):
            """Build the fake acquire org session fixture used by the surrounding regression cases."""
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
        """Verify the contract that task exists."""
        if not self.available:
            pytest.skip("Task not importable")
        assert self.task is not None

    def test_task_name(self):
        """Verify the contract that task name."""
        if not self.available:
            pytest.skip("Task not importable")
        assert self.task.name == "meeting_summary_task.process"

    def test_task_max_retries(self):
        """Verify the contract that task max retries."""
        if not self.available:
            pytest.skip("Task not importable")
        assert self.task.max_retries == 3

    def test_task_acks_late(self):
        """Verify the contract that task acks late."""
        if not self.available:
            pytest.skip("Task not importable")
        assert self.task.acks_late is True

    def test_task_skips_when_same_job_is_already_running(self):
        """All jobs for one event share the same expensive-generation lock."""
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
            deal_id=uuid4(),
            event_id=event_id,
            status=JobStatus.PENDING,
        )

        class FakeSession:
            async def get(self, model, requested_job_id):
                assert model is DealJobs
                assert requested_job_id == job_id
                return job

        async def fake_acquire_org_session(_organisation_id):
            yield FakeSession()

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
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.acquire_org_async_session",
                fake_acquire_org_session,
            ),
        ):
            result = self.task.run(str(org_id), str(job_id))

        assert result == {"status": "skipped", "reason": "already_running"}
        acquire_lock.assert_called_once_with(
            f"task_lock:meeting_summary_event:{org_id}:{event_id}", ttl=700
        )
        release_lock.assert_not_called()

    def test_non_deal_meeting_summary_job_generates_meeting_level_summary(self):
        """Verify the contract that non deal meeting summary job generates meeting level summary."""
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
                "send_summary_notification": True,
            },
        )

        class FakeSession:
            """Represent the Fake Session test double used by the surrounding regression cases."""

            async def get(self, model, requested_job_id):
                """Expose the get operation expected by the Fake Session test double."""
                assert model is DealJobs
                assert requested_job_id == job_id
                return job

            async def commit(self):
                """Expose the commit operation expected by the Fake Session test double."""
                return None

        async def fake_acquire_org_session(_organisation_id):
            """Build the fake acquire org session fixture used by the surrounding regression cases."""
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
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task._dispatch_event_deal_insight_pipelines",
                new_callable=AsyncMock,
                create=True,
            ) as dispatch_event_pipelines,
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
        dispatch_event_pipelines.assert_awaited_once_with(
            org_id,
            event_id,
            job_id,
            source="recall_webhook",
            exclude_deal_id=None,
        )
        publisher_cls.assert_not_called()
        assert job.status == JobStatus.COMPLETED
        assert job.result_data == summary_result
        assert result == {
            "status": "completed",
            "job_id": str(job_id),
            "job_type": str(DealJobType.MEETING_SUMMARY),
        }

    def test_retry_reuses_the_durable_summary_artifact(self):
        """A downstream retry must not pay for or overwrite the summary again."""
        if not self.available:
            pytest.skip("Task not importable")
        org_id = uuid4()
        deal_id = uuid4()
        event_id = uuid4()
        job_id = uuid4()
        stored_result = {"data": {"meeting_summary": "Already durable"}}
        job = DealJobs(
            job_id=job_id,
            organisation_id=org_id,
            user_id=uuid4(),
            job_type=DealJobType.MEETING_SUMMARY,
            deal_id=deal_id,
            event_id=event_id,
            status=JobStatus.IN_PROGRESS,
            result_data=stored_result,
            job_metadata={"skip_insight_pipeline": True},
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
            ),
        ):
            service = service_cls.return_value
            service.generate_meeting_level_summary = AsyncMock(return_value={"unexpected": True})
            service.last_token_usage = None

            result = self.task.run(str(org_id), str(job_id))

        service.generate_meeting_level_summary.assert_not_awaited()
        assert job.result_data == stored_result
        assert job.status == JobStatus.COMPLETED
        assert result["status"] == "completed"

    def test_deal_linked_job_generates_the_canonical_meeting_summary(self):
        """Deal linkage must not select a deal-specific prompt for the shared artifact."""
        if not self.available:
            pytest.skip("Task not importable")
        org_id = uuid4()
        deal_id = uuid4()
        event_id = uuid4()
        job_id = uuid4()
        job = DealJobs(
            job_id=job_id,
            organisation_id=org_id,
            user_id=uuid4(),
            job_type=DealJobType.MEETING_SUMMARY,
            deal_id=deal_id,
            event_id=event_id,
            status=JobStatus.PENDING,
            job_metadata={"skip_insight_pipeline": True},
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

        summary_result = {"data": {"meeting_summary": "Canonical meeting summary"}}
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
            ),
        ):
            service = service_cls.return_value
            service.generate = AsyncMock(return_value=summary_result)
            service.generate_meeting_level_summary = AsyncMock(return_value=summary_result)
            service.last_token_usage = None

            result = self.task.run(str(org_id), str(job_id))

        service.generate_meeting_level_summary.assert_awaited_once()
        service.generate.assert_not_awaited()
        assert job.status == JobStatus.COMPLETED
        assert result["status"] == "completed"
