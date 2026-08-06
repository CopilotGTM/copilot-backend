"""Display-state projections for saved automation triggers."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.service.automation.triggers.serializers import trigger_summary


def _trigger(**overrides):
    values = {
        "trigger_id": uuid4(),
        "name": "Scheduled review",
        "target_type": "agent",
        "target_id": uuid4(),
        "target_revision": 1,
        "trigger_key": "time.schedule",
        "family": "schedule",
        "match_event_keys": ["schedule.fired"],
        "execution_principal_type": "user",
        "status": "active",
        "next_trigger_at": None,
        "last_triggered_at": None,
        "trigger_count": 1,
        "consecutive_failures": 0,
        "last_error_code": None,
        "last_error": None,
        "trigger_config": {"recurrence": "none"},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("overrides", "display_status"),
    [
        ({}, "completed"),
        ({"trigger_config": {"recurrence": "daily"}}, "active"),
        ({"last_error": "Scheduler failed"}, "needs_attention"),
    ],
)
def test_trigger_summary_derives_truthful_display_status(overrides, display_status) -> None:
    summary = trigger_summary(_trigger(**overrides), current_target_revision=1)

    assert summary.display_status == display_status
