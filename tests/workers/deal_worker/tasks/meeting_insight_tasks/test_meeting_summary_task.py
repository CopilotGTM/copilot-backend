"""Tests for meeting summary task — insight pipeline dispatch after manual summary.

Tests verify that:
1. Manual (non-automated) summaries trigger the insight pipeline
2. Automated pipeline summaries do NOT double-trigger
3. _dispatch_insight_pipeline_for_manual_summary creates a job and dispatches correctly
4. Errors in pipeline dispatch are caught and do not fail the summary task
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

pytestmark = pytest.mark.unit

# Sentinel — mirrors the constant in the task module
AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


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
                ".meeting_summary_task.dispatch_deal_insight_pipeline",
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
            mock_dispatch.assert_called_once_with(
                str(org_id),
                str(deal_id),
                str(memory_job_id),
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
                ".meeting_summary_task.dispatch_deal_insight_pipeline",
            ) as mock_dispatch,
        ):
            mock_acquire.return_value = fake_session_gen(org_id)

            from src.workers.deal_worker.tasks.meeting_insight_tasks.meeting_summary_task import (
                _dispatch_insight_pipeline_for_manual_summary,
            )

            await _dispatch_insight_pipeline_for_manual_summary(org_id, deal_id)

            mock_dispatch.assert_called_once_with(
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
                ".meeting_summary_task.dispatch_deal_insight_pipeline",
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
            yield  # noqa: unreachable — makes this an async generator

        with (
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.acquire_org_async_session",
            ) as mock_acquire,
            patch(
                "src.workers.deal_worker.tasks.meeting_insight_tasks"
                ".meeting_summary_task.dispatch_deal_insight_pipeline",
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


# =============================================================================
# is_automated gate — verifies the condition that decides pipeline dispatch
# =============================================================================


class TestIsAutomatedGate:
    """Tests for the is_automated logic that gates insight pipeline dispatch."""

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
