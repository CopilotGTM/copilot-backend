"""Boundary tests for task presentation fields consumed by the Actions UI."""

from datetime import UTC, datetime

import pytest
from src.api.presenters.crm.task_presenter import compute_task_is_overdue

REFERENCE = datetime(2026, 7, 30, 3, 30, tzinfo=UTC)


@pytest.mark.parametrize("status", ["COMPLETED", "CANCELLED"])
def test_terminal_tasks_are_never_overdue(status: str) -> None:
    """Terminal workflow states must suppress overdue even after the due instant."""
    assert (
        compute_task_is_overdue(
            datetime(2026, 7, 29, 3, 30, tzinfo=UTC),
            status,
            reference=REFERENCE,
        )
        is False
    )


def test_timed_task_becomes_overdue_after_its_instant() -> None:
    """Timed tasks should compare exact instants rather than calendar dates."""
    assert (
        compute_task_is_overdue(
            datetime(2026, 7, 30, 3, 29, tzinfo=UTC),
            "NOT_STARTED",
            reference=REFERENCE,
        )
        is True
    )


def test_all_day_task_is_not_overdue_on_its_local_due_date() -> None:
    """An all-day task remains current for the complete due date in its timezone."""
    assert (
        compute_task_is_overdue(
            datetime(2026, 7, 30, tzinfo=UTC),
            "NOT_STARTED",
            is_all_day=True,
            due_local_date="2026-07-30",
            timezone_name="Asia/Kolkata",
            reference=REFERENCE,
        )
        is False
    )


def test_all_day_task_becomes_overdue_after_its_local_due_date() -> None:
    """An all-day task becomes overdue when the viewer's local calendar advances."""
    assert (
        compute_task_is_overdue(
            datetime(2026, 7, 29, tzinfo=UTC),
            "NOT_STARTED",
            is_all_day=True,
            due_local_date="2026-07-29",
            timezone_name="Asia/Kolkata",
            reference=REFERENCE,
        )
        is True
    )
