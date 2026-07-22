"""Shared calendar cursor behavior for partially persisted provider pages."""

import inspect
from pathlib import Path
from types import SimpleNamespace

from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.calendar_cursor_metadata import (
    CALENDAR_LAST_QUARANTINE_KEY,
    CALENDAR_PARTIAL_RETRY_KEY,
    apply_calendar_sync_outcome,
    get_calendar_delta_link,
)
from src.workers.integrations_worker.tasks.calendar import integration_task


def _state() -> SimpleNamespace:
    return SimpleNamespace(
        cursor_metadata={"calendar_delta_link": "old-cursor"},
        sync_metadata={},
        last_sync_status=None,
        last_sync_error=None,
        consecutive_sync_failures=0,
        last_synced_at=None,
    )


def _integration() -> SimpleNamespace:
    return SimpleNamespace(
        connection_metadata={},
        last_sync_status=None,
        last_sync_error=None,
        consecutive_sync_failures=0,
        last_synced_at=None,
    )


def _partial_result(failed_id: str = "bad-event") -> dict[str, object]:
    return {
        "status": "partial",
        "records_failed": 1,
        "failed_record_ids": [failed_id],
    }


def test_partial_sync_quarantines_third_identical_page() -> None:
    """Two replays protect data; the third consumes a poison page to unblock the mailbox."""
    state = _state()
    integration = _integration()

    first = apply_calendar_sync_outcome(
        integration=integration,
        state=state,
        integration_type=IntegrationType.GOOGLE_CALENDAR,
        sync_result=_partial_result(),
        input_cursor="old-cursor",
        next_cursor="next-cursor",
    )
    second = apply_calendar_sync_outcome(
        integration=integration,
        state=state,
        integration_type=IntegrationType.GOOGLE_CALENDAR,
        sync_result=_partial_result(),
        input_cursor="old-cursor",
        next_cursor="next-cursor",
    )
    third = apply_calendar_sync_outcome(
        integration=integration,
        state=state,
        integration_type=IntegrationType.GOOGLE_CALENDAR,
        sync_result=_partial_result(),
        input_cursor="old-cursor",
        next_cursor="next-cursor",
    )

    assert first.cursor_advanced is False
    assert second.cursor_advanced is False
    assert third.cursor_advanced is True
    assert third.quarantined is True
    assert get_calendar_delta_link(state.cursor_metadata, IntegrationType.GOOGLE_CALENDAR) == (
        "next-cursor"
    )
    assert CALENDAR_PARTIAL_RETRY_KEY not in state.sync_metadata
    assert state.sync_metadata[CALENDAR_LAST_QUARANTINE_KEY]["failed_record_ids"] == ["bad-event"]
    assert state.last_sync_status == "partial"


def test_changed_partial_fingerprint_resets_attempt_count() -> None:
    """A different bad record is a new page failure and receives its own retry budget."""
    state = _state()
    integration = _integration()
    for _ in range(2):
        apply_calendar_sync_outcome(
            integration=integration,
            state=state,
            integration_type=IntegrationType.GOOGLE_CALENDAR,
            sync_result=_partial_result("first-bad-event"),
            input_cursor="old-cursor",
            next_cursor="next-cursor",
        )

    changed = apply_calendar_sync_outcome(
        integration=integration,
        state=state,
        integration_type=IntegrationType.GOOGLE_CALENDAR,
        sync_result=_partial_result("different-bad-event"),
        input_cursor="old-cursor",
        next_cursor="next-cursor",
    )

    assert changed.partial_attempts == 1
    assert changed.cursor_advanced is False
    assert state.sync_metadata[CALENDAR_PARTIAL_RETRY_KEY]["attempts"] == 1


def test_success_clears_retry_and_advances_cursor() -> None:
    """A fully persisted replay clears poison-page state and advances normally."""
    state = _state()
    state.sync_metadata = {CALENDAR_PARTIAL_RETRY_KEY: {"attempts": 2}}

    outcome = apply_calendar_sync_outcome(
        integration=_integration(),
        state=state,
        integration_type=IntegrationType.GOOGLE_CALENDAR,
        sync_result={"status": "success", "records_failed": 0},
        input_cursor="old-cursor",
        next_cursor="next-cursor",
    )

    assert outcome.cursor_advanced is True
    assert CALENDAR_PARTIAL_RETRY_KEY not in state.sync_metadata
    assert state.last_sync_status == "success"
    assert state.last_sync_error is None


def test_manual_and_scheduled_workers_use_shared_cursor_policy() -> None:
    """The active task and retained fanout task must not diverge on cursor safety."""
    assert "apply_calendar_sync_outcome(" in inspect.getsource(
        integration_task.sync_calendar_integration_task
    )
    retained_org_task = (
        Path(__file__).parents[3]
        / "src/workers/integrations_worker/tasks/deprecated/calendar/org_task.py"
    ).read_text()
    assert "apply_calendar_sync_outcome(" in retained_org_task
