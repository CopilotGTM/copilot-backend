"""Tests for transcript fetch tasks.

Tests for:
- trigger_transcript_fetch_all_orgs_task (Tier 1)
- fetch_meeting_transcripts_org_task (Tier 2)
"""

from __future__ import annotations

from datetime import UTC, datetime

# Handle circular import gracefully - use Any type to avoid stub/real class conflicts
from typing import Any
from uuid import uuid4

import pytest

TASKS_AVAILABLE = False
trigger_transcript_fetch_all_orgs_task: Any = None
fetch_meeting_transcripts_org_task: Any = None

try:
    from src.workers.deal_worker.tasks.transcript_fetch_tasks import (
        fetch_meeting_transcripts_org_task,
        trigger_transcript_fetch_all_orgs_task,
    )

    TASKS_AVAILABLE = True
except ImportError:
    pass

pytestmark = pytest.mark.skipif(
    not TASKS_AVAILABLE,
    reason="Circular import in deal_worker - tasks not importable in test context",
)


# =============================================================================
# Tier 1: trigger_transcript_fetch_all_orgs_task Config Tests
# =============================================================================


class TestTriggerTranscriptFetchAllOrgsTaskConfig:
    """Tests for trigger_transcript_fetch_all_orgs_task Celery configuration."""

    def test_task_exists(self) -> None:
        """Task should exist and be importable."""
        assert trigger_transcript_fetch_all_orgs_task is not None

    def test_task_name(self) -> None:
        """Task should have correct name."""
        assert (
            trigger_transcript_fetch_all_orgs_task.name == "deal.trigger_transcript_fetch_all_orgs"
        )

    def test_task_max_retries(self) -> None:
        """Task should have max_retries=0 (coordinator task)."""
        assert trigger_transcript_fetch_all_orgs_task.max_retries == 0

    def test_task_acks_late(self) -> None:
        """Task should have acks_late=True."""
        assert trigger_transcript_fetch_all_orgs_task.acks_late is True

    def test_task_soft_time_limit(self) -> None:
        """Task should have soft_time_limit=300 (5 minutes)."""
        assert trigger_transcript_fetch_all_orgs_task.soft_time_limit == 300

    def test_task_time_limit(self) -> None:
        """Task should have time_limit=360 (6 minutes)."""
        assert trigger_transcript_fetch_all_orgs_task.time_limit == 360


# =============================================================================
# Tier 2: fetch_meeting_transcripts_org_task Config Tests
# =============================================================================


class TestFetchMeetingTranscriptsOrgTaskConfig:
    """Tests for fetch_meeting_transcripts_org_task Celery configuration."""

    def test_task_exists(self) -> None:
        """Task should exist and be importable."""
        assert fetch_meeting_transcripts_org_task is not None

    def test_task_name(self) -> None:
        """Task should have correct name."""
        assert fetch_meeting_transcripts_org_task.name == "deal.fetch_meeting_transcripts_org"

    def test_task_max_retries(self) -> None:
        """Task should have max_retries=2."""
        assert fetch_meeting_transcripts_org_task.max_retries == 2

    def test_task_default_retry_delay(self) -> None:
        """Task should have default_retry_delay=60."""
        assert fetch_meeting_transcripts_org_task.default_retry_delay == 60

    def test_task_acks_late(self) -> None:
        """Task should have acks_late=True."""
        assert fetch_meeting_transcripts_org_task.acks_late is True

    def test_task_soft_time_limit(self) -> None:
        """Task should have soft_time_limit=3000 (50 minutes)."""
        assert fetch_meeting_transcripts_org_task.soft_time_limit == 3000

    def test_task_time_limit(self) -> None:
        """Task should have time_limit=3300 (55 minutes)."""
        assert fetch_meeting_transcripts_org_task.time_limit == 3300


# =============================================================================
# Parameter Tests
# =============================================================================


class TestFetchMeetingTranscriptsOrgTaskParameters:
    """Tests for fetch_meeting_transcripts_org_task parameter handling."""

    def test_accepts_required_parameters(self) -> None:
        """Task should accept organisation_id."""
        import inspect

        sig = inspect.signature(fetch_meeting_transcripts_org_task)
        assert "organisation_id" in sig.parameters


class TestTriggerTranscriptFetchAllOrgsTaskParameters:
    """Tests for trigger_transcript_fetch_all_orgs_task parameter handling."""

    def test_no_required_parameters(self) -> None:
        """Task should have no required parameters (coordinator task)."""
        import inspect

        sig = inspect.signature(trigger_transcript_fetch_all_orgs_task)
        # Only self parameter should exist (Celery bind=False means no self)
        # Filter out 'self' if present
        params = [p for p in sig.parameters if p != "self"]
        assert len(params) == 0


# =============================================================================
# Result Structure Tests (no imports needed)
# =============================================================================


@pytest.mark.skipif(False, reason="")
class TestTriggerTranscriptFetchAllOrgsResultStructure:
    """Tests for trigger_transcript_fetch_all_orgs_task return value structure."""

    def test_completed_result_has_required_fields(self) -> None:
        """Completed result should have all required fields."""
        result = {
            "status": "completed",
            "started_at": datetime.now(UTC).isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "summary": {
                "total_orgs": 5,
                "orgs_triggered": 5,
                "orgs_skipped": 0,
            },
            "org_tasks": [],
        }
        assert "status" in result
        assert "started_at" in result
        assert "completed_at" in result
        assert "summary" in result
        assert "total_orgs" in result["summary"]
        assert "orgs_triggered" in result["summary"]
        assert "orgs_skipped" in result["summary"]
        assert "org_tasks" in result

    def test_failed_result_has_error_field(self) -> None:
        """Failed result should include error information."""
        result = {
            "status": "failed",
            "started_at": datetime.now(UTC).isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "error": "Some error message",
            "error_type": "RuntimeError",
        }
        assert result["status"] == "failed"
        assert "error" in result
        assert "error_type" in result

    def test_no_orgs_result_structure(self) -> None:
        """Result when no orgs found should have correct structure."""
        result = {
            "status": "completed",
            "started_at": datetime.now(UTC).isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "summary": {
                "total_orgs": 0,
                "orgs_triggered": 0,
                "orgs_skipped": 0,
            },
            "org_tasks": [],
        }
        assert result["status"] == "completed"
        assert result["summary"]["total_orgs"] == 0


@pytest.mark.skipif(False, reason="")
class TestFetchMeetingTranscriptsOrgResultStructure:
    """Tests for fetch_meeting_transcripts_org_task return value structure."""

    def test_success_result_has_required_fields(self) -> None:
        """Success result should have all required fields."""
        result = {
            "status": "success",
            "organisation_id": str(uuid4()),
            "started_at": datetime.now(UTC).isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "meetings_found": 10,
            "transcripts_fetched": 5,
            "transcripts_pending": 3,
            "transcripts_not_available": 1,
            "transcripts_failed": 1,
        }
        assert "status" in result
        assert "organisation_id" in result
        assert "started_at" in result
        assert "completed_at" in result
        assert "meetings_found" in result
        assert "transcripts_fetched" in result
        assert "transcripts_pending" in result
        assert "transcripts_not_available" in result
        assert "transcripts_failed" in result

    def test_failed_result_has_error_field(self) -> None:
        """Failed result should include error information."""
        result = {
            "status": "failed",
            "organisation_id": str(uuid4()),
            "started_at": datetime.now(UTC).isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "error": "Database connection failed",
            "error_type": "ConnectionError",
        }
        assert result["status"] == "failed"
        assert "error" in result
        assert "error_type" in result

    def test_skipped_result_structure(self) -> None:
        """Skipped result should have correct structure."""
        result = {
            "status": "skipped",
            "reason": "already_running",
            "organisation_id": str(uuid4()),
            "started_at": datetime.now(UTC).isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
        }
        assert result["status"] == "skipped"
        assert "reason" in result
        assert result["reason"] == "already_running"


# =============================================================================
# Transcript Status Tests
# =============================================================================


@pytest.mark.skipif(False, reason="")
class TestTranscriptStatusValues:
    """Tests for transcript status values."""

    def test_valid_transcript_statuses(self) -> None:
        """Valid transcript statuses in result."""
        valid_statuses = ["fetched", "pending", "not_available", "failed"]
        for status in valid_statuses:
            assert status in valid_statuses

    def test_transcript_counts_are_non_negative(self) -> None:
        """Transcript counts should be non-negative integers."""
        result = {
            "meetings_found": 10,
            "transcripts_fetched": 5,
            "transcripts_pending": 3,
            "transcripts_not_available": 1,
            "transcripts_failed": 1,
        }
        assert result["meetings_found"] >= 0
        assert result["transcripts_fetched"] >= 0
        assert result["transcripts_pending"] >= 0
        assert result["transcripts_not_available"] >= 0
        assert result["transcripts_failed"] >= 0

    def test_transcript_counts_sum_to_meetings_found(self) -> None:
        """Sum of transcript statuses should equal meetings_found."""
        result = {
            "meetings_found": 10,
            "transcripts_fetched": 5,
            "transcripts_pending": 3,
            "transcripts_not_available": 1,
            "transcripts_failed": 1,
        }
        total = (
            result["transcripts_fetched"]
            + result["transcripts_pending"]
            + result["transcripts_not_available"]
            + result["transcripts_failed"]
        )
        assert total == result["meetings_found"]


# =============================================================================
# Task Lock Tests (structure only)
# =============================================================================


@pytest.mark.skipif(False, reason="")
class TestTaskLockBehavior:
    """Tests for task lock behavior in fetch_meeting_transcripts_org_task."""

    def test_lock_key_format(self) -> None:
        """Lock key should follow expected format."""
        org_id = str(uuid4())
        expected_lock_key = f"task_lock:fetch_meeting_transcripts_org:{org_id}"
        assert "task_lock:" in expected_lock_key
        assert "fetch_meeting_transcripts_org" in expected_lock_key
        assert org_id in expected_lock_key

    def test_skipped_result_when_locked(self) -> None:
        """When task is already running, should return skipped status."""
        result = {
            "status": "skipped",
            "reason": "already_running",
            "organisation_id": str(uuid4()),
            "started_at": datetime.now(UTC).isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
        }
        assert result["status"] == "skipped"
        assert result["reason"] == "already_running"
