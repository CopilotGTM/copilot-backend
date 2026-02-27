"""Tests for reminder recurrence prefix stripping."""

import pytest
from src.agents.tools.reminders.create_reminder import _strip_recurrence_prefix


@pytest.mark.parametrize(
    "input_expr,expected",
    [
        # Day-name recurrence
        ("Every Monday at 9pm IST", "at 9pm IST"),
        ("every tuesday at 3 PM", "at 3 PM"),
        ("every wednesday at 2pm", "at 2pm"),
        ("every fri at noon", "at noon"),
        ("each Monday at 8 AM", "at 8 AM"),
        ("each Tuesday at 4 PM", "at 4 PM"),
        # Frequency keywords
        ("daily at 10 AM IST", "at 10 AM IST"),
        ("weekly at 3 PM", "at 3 PM"),
        ("biweekly at 2 PM", "at 2 PM"),
        ("monthly at 8 AM", "at 8 AM"),
        # "every" + frequency
        ("every day at 10 AM", "at 10 AM"),
        ("each day at 3 PM", "at 3 PM"),
        ("every week at 9 AM", "at 9 AM"),
        ("every month at 5 PM", "at 5 PM"),
        ("every other week at 3 PM", "at 3 PM"),
        # Time-of-day recurrence
        ("every morning at 7 AM", "at 7 AM"),
        ("every evening at 6 PM", "at 6 PM"),
        ("every weekday at 9 AM", "at 9 AM"),
        # Pass-through (no recurrence prefix)
        ("at 3 PM", "at 3 PM"),
        ("tomorrow at 9 AM", "tomorrow at 9 AM"),
        ("in 5 minutes", "in 5 minutes"),
        ("9pm IST", "9pm IST"),
        ("2026-03-01T15:00:00", "2026-03-01T15:00:00"),
    ],
)
def test_strip_recurrence_prefix(input_expr: str, expected: str) -> None:
    """Recurrence prefixes should be stripped; plain time expressions pass through."""
    assert _strip_recurrence_prefix(input_expr) == expected
