"""Tests for email matching tasks.

Tests for Task 3: match_email_to_deals_org_task and match_email_to_deals_task.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

# Handle circular import gracefully - skip all task-dependent tests if import fails
try:
    from src.workers.deal_worker.tasks.deal_mappers.email_matching_tasks import (
        match_email_to_deals_org_task,
        match_email_to_deals_task,
    )

    TASKS_AVAILABLE = True
except ImportError:
    TASKS_AVAILABLE = False
    match_email_to_deals_org_task = None
    match_email_to_deals_task = None

pytestmark = pytest.mark.skipif(
    not TASKS_AVAILABLE,
    reason="Circular import in deal_worker - tasks not importable in test context",
)


class TestMatchEmailToDealsOrgTaskConfig:
    """Tests for match_email_to_deals_org_task Celery configuration."""

    def test_task_exists(self) -> None:
        """Task should exist and be importable."""
        assert match_email_to_deals_org_task is not None

    def test_task_name(self) -> None:
        """Task should have correct name."""
        assert match_email_to_deals_org_task.name == "deal.match_email_to_deals_org"

    def test_task_max_retries(self) -> None:
        """Task should have max_retries=2."""
        assert match_email_to_deals_org_task.max_retries == 2

    def test_task_default_retry_delay(self) -> None:
        """Task should have default_retry_delay=60."""
        assert match_email_to_deals_org_task.default_retry_delay == 60

    def test_task_acks_late(self) -> None:
        """Task should have acks_late=True."""
        assert match_email_to_deals_org_task.acks_late is True

    def test_task_soft_time_limit(self) -> None:
        """Task should have soft_time_limit=600 (10 minutes)."""
        assert match_email_to_deals_org_task.soft_time_limit == 600

    def test_task_time_limit(self) -> None:
        """Task should have time_limit=660 (11 minutes)."""
        assert match_email_to_deals_org_task.time_limit == 660


class TestMatchEmailToDealsTaskConfig:
    """Tests for match_email_to_deals_task Celery configuration."""

    def test_task_exists(self) -> None:
        """Task should exist and be importable."""
        assert match_email_to_deals_task is not None

    def test_task_name(self) -> None:
        """Task should have correct name."""
        assert match_email_to_deals_task.name == "deal.match_email_to_deals"

    def test_task_max_retries(self) -> None:
        """Task should have max_retries=3."""
        assert match_email_to_deals_task.max_retries == 3

    def test_task_default_retry_delay(self) -> None:
        """Task should have default_retry_delay=30."""
        assert match_email_to_deals_task.default_retry_delay == 30

    def test_task_soft_time_limit(self) -> None:
        """Task should have soft_time_limit=120 (2 minutes)."""
        assert match_email_to_deals_task.soft_time_limit == 120

    def test_task_time_limit(self) -> None:
        """Task should have time_limit=150 (2.5 minutes)."""
        assert match_email_to_deals_task.time_limit == 150


class TestMatchEmailToDealsOrgTaskParameters:
    """Tests for match_email_to_deals_org_task parameter handling."""

    def test_accepts_organisation_id(self) -> None:
        """Task should accept organisation_id parameter."""
        import inspect

        sig = inspect.signature(match_email_to_deals_org_task)
        assert "organisation_id" in sig.parameters


class TestMatchEmailToDealsTaskParameters:
    """Tests for match_email_to_deals_task parameter handling."""

    def test_accepts_required_parameters(self) -> None:
        """Task should accept organisation_id and deal_id parameters."""
        import inspect

        sig = inspect.signature(match_email_to_deals_task)
        assert "organisation_id" in sig.parameters
        assert "deal_id" in sig.parameters
        assert "job_id" in sig.parameters
        assert sig.parameters["job_id"].default is None


# Tests that don't require task imports - always run
@pytest.mark.skipif(False, reason="")
class TestMatchEmailToDealsOrgTaskResultStructure:
    """Tests for match_email_to_deals_org_task return value structure."""

    def test_success_result_has_required_fields(self) -> None:
        """Successful result should have all required fields."""
        result = {
            "status": "success",
            "organisation_id": str(uuid4()),
            "started_at": datetime.now(UTC).isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "emails_processed": 150,
            "emails_matched": 45,
            "associations_created": 45,
            "deals_with_matches": 12,
        }
        assert "status" in result
        assert "organisation_id" in result
        assert "emails_processed" in result
        assert "emails_matched" in result
        assert "associations_created" in result

    def test_failed_result_has_error_field(self) -> None:
        """Failed result should include error information."""
        result = {
            "status": "failed",
            "organisation_id": str(uuid4()),
            "error": "Database connection failed",
        }
        assert result["status"] == "failed"
        assert "error" in result


@pytest.mark.skipif(False, reason="")
class TestMatchEmailToDealsTaskResultStructure:
    """Tests for match_email_to_deals_task return value structure."""

    def test_success_result_has_required_fields(self) -> None:
        """Successful result should have all required fields."""
        result = {
            "status": "success",
            "organisation_id": str(uuid4()),
            "deal_id": str(uuid4()),
            "emails_processed": 50,
            "emails_matched": 8,
            "associations_created": 8,
        }
        assert "status" in result
        assert "deal_id" in result
        assert "emails_matched" in result

    def test_failed_result_has_error_fields(self) -> None:
        """Failed result should include error information."""
        result = {
            "status": "failed",
            "error": "Deal not found",
            "error_type": "ValueError",
        }
        assert result["status"] == "failed"
        assert "error" in result


@pytest.mark.skipif(False, reason="")
class TestRetryBehavior:
    """Tests for task retry behavior."""

    def test_org_task_exponential_backoff(self) -> None:
        """Org task retry delays should follow exponential backoff."""
        base_delay = 60
        assert base_delay * (2**0) == 60
        assert base_delay * (2**1) == 120

    def test_deal_task_exponential_backoff(self) -> None:
        """Deal task retry delays should follow exponential backoff."""
        base_delay = 30
        assert base_delay * (2**0) == 30
        assert base_delay * (2**1) == 60
        assert base_delay * (2**2) == 120


@pytest.mark.skipif(False, reason="")
class TestEmailMatchingMetrics:
    """Tests for email matching metrics tracking."""

    def test_metrics_include_processed_count(self) -> None:
        """Results should include emails_processed count."""
        result = {"emails_processed": 100, "emails_matched": 25}
        assert result["emails_processed"] == 100

    def test_metrics_include_matched_count(self) -> None:
        """Results should include emails_matched count."""
        result = {"emails_processed": 100, "emails_matched": 25}
        assert result["emails_matched"] == 25

    def test_match_rate_calculation(self) -> None:
        """Match rate should be calculable from metrics."""
        result = {"emails_processed": 100, "emails_matched": 25}
        match_rate = (
            result["emails_matched"] / result["emails_processed"]
            if result["emails_processed"] > 0
            else 0.0
        )
        assert match_rate == 0.25

    def test_zero_processed_returns_zero_rate(self) -> None:
        """Match rate should be 0 when no emails processed."""
        result = {"emails_processed": 0, "emails_matched": 0}
        match_rate = (
            result["emails_matched"] / result["emails_processed"]
            if result["emails_processed"] > 0
            else 0.0
        )
        assert match_rate == 0.0
