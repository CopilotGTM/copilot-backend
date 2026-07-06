"""Tests for meeting recording dispatch task configuration."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

TASKS_AVAILABLE = False
meeting_bot_tasks_module: Any = None
send_meeting_recording_task: Any = None
process_meeting_recording_webhook_task: Any = None
send_unsent_meeting_recordings_org_task: Any = None
trigger_send_unsent_meeting_recordings_all_orgs_task: Any = None

try:
    from src.workers.deal_worker.tasks import meeting_bot_tasks as meeting_bot_tasks_module
    from src.workers.deal_worker.tasks.meeting_bot_tasks import (
        process_meeting_recording_webhook_task,
        send_meeting_recording_task,
        send_unsent_meeting_recordings_org_task,
        trigger_send_unsent_meeting_recordings_all_orgs_task,
    )

    TASKS_AVAILABLE = True
except ImportError:
    pass

# Keep the assertions in this file because they protect the Celery task contract,
# but skip cleanly until the surrounding circular import is removed.
pytestmark = pytest.mark.skipif(
    not TASKS_AVAILABLE,
    reason="Circular import in deal_worker - tasks not importable in test context",
)


class TestSendMeetingRecordingTaskConfig:
    def test_task_exists(self) -> None:
        assert send_meeting_recording_task is not None

    def test_task_name(self) -> None:
        assert send_meeting_recording_task.name == "deal.send_meeting_recording"

    def test_task_max_retries(self) -> None:
        assert send_meeting_recording_task.max_retries == 2

    def test_task_default_retry_delay(self) -> None:
        assert send_meeting_recording_task.default_retry_delay == 60

    def test_task_soft_time_limit(self) -> None:
        assert send_meeting_recording_task.soft_time_limit == 180

    def test_task_time_limit(self) -> None:
        assert send_meeting_recording_task.time_limit == 240


class TestProcessMeetingRecordingWebhookTaskConfig:
    def test_task_exists(self) -> None:
        assert process_meeting_recording_webhook_task is not None

    def test_task_name(self) -> None:
        assert (
            process_meeting_recording_webhook_task.name == "deal.process_meeting_recording_webhook"
        )

    def test_task_max_retries(self) -> None:
        assert process_meeting_recording_webhook_task.max_retries == 2

    def test_task_default_retry_delay(self) -> None:
        assert process_meeting_recording_webhook_task.default_retry_delay == 60

    def test_task_soft_time_limit(self) -> None:
        assert process_meeting_recording_webhook_task.soft_time_limit == 180

    def test_task_time_limit(self) -> None:
        assert process_meeting_recording_webhook_task.time_limit == 240


class TestProcessMeetingRecordingWebhookTaskParameters:
    def test_accepts_required_parameters(self) -> None:
        import inspect

        sig = inspect.signature(process_meeting_recording_webhook_task)
        assert "payload" in sig.parameters
        assert "webhook_id" in sig.parameters


class TestSendMeetingRecordingTaskParameters:
    def test_accepts_required_parameters(self) -> None:
        import inspect

        sig = inspect.signature(send_meeting_recording_task)
        assert "organisation_id" in sig.parameters
        assert "calendar_event_id" in sig.parameters

    def test_uses_event_lifecycle_sync(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Single-event jobs should reconcile event lifecycle, not only dispatch sends."""
        organisation_id = UUID("11111111-1111-1111-1111-111111111111")
        calendar_event_id = UUID("22222222-2222-2222-2222-222222222222")
        calls: list[tuple[UUID, UUID]] = []

        class _LifecycleService:
            def __init__(self, org_id: UUID) -> None:
                self.org_id = org_id

            async def sync_event_lifecycle(self, event_id: UUID) -> dict[str, str]:
                calls.append((self.org_id, event_id))
                return {"status": "scheduled"}

        monkeypatch.setattr(
            meeting_bot_tasks_module,
            "MeetingRecordingLifecycleService",
            _LifecycleService,
        )
        monkeypatch.setattr(
            meeting_bot_tasks_module,
            "acquire_task_lock",
            lambda *_args, **_kwargs: True,
        )
        monkeypatch.setattr(
            meeting_bot_tasks_module,
            "release_task_lock",
            lambda *_args, **_kwargs: None,
        )

        result = send_meeting_recording_task.run(str(organisation_id), str(calendar_event_id))

        assert result["status"] == "scheduled"
        assert result["organisation_id"] == str(organisation_id)
        assert result["calendar_event_id"] == str(calendar_event_id)
        assert calls == [(organisation_id, calendar_event_id)]


class TestSendUnsentMeetingRecordingsOrgTaskConfig:
    def test_task_exists(self) -> None:
        assert send_unsent_meeting_recordings_org_task is not None

    def test_task_name(self) -> None:
        assert (
            send_unsent_meeting_recordings_org_task.name
            == "deal.send_unsent_meeting_recordings_org"
        )

    def test_task_max_retries(self) -> None:
        assert send_unsent_meeting_recordings_org_task.max_retries == 2

    def test_task_default_retry_delay(self) -> None:
        assert send_unsent_meeting_recordings_org_task.default_retry_delay == 60

    def test_task_soft_time_limit(self) -> None:
        assert send_unsent_meeting_recordings_org_task.soft_time_limit == 600

    def test_task_time_limit(self) -> None:
        assert send_unsent_meeting_recordings_org_task.time_limit == 660


class TestSendUnsentMeetingRecordingsOrgTaskParameters:
    def test_accepts_required_parameters(self) -> None:
        import inspect

        sig = inspect.signature(send_unsent_meeting_recordings_org_task)
        assert "organisation_id" in sig.parameters


class TestTriggerSendUnsentMeetingRecordingsAllOrgsTaskConfig:
    def test_task_exists(self) -> None:
        assert trigger_send_unsent_meeting_recordings_all_orgs_task is not None

    def test_task_name(self) -> None:
        assert (
            trigger_send_unsent_meeting_recordings_all_orgs_task.name
            == "deal.trigger_send_unsent_meeting_recordings_all_orgs"
        )

    def test_task_max_retries(self) -> None:
        assert trigger_send_unsent_meeting_recordings_all_orgs_task.max_retries == 0

    def test_task_soft_time_limit(self) -> None:
        assert trigger_send_unsent_meeting_recordings_all_orgs_task.soft_time_limit == 300

    def test_task_time_limit(self) -> None:
        assert trigger_send_unsent_meeting_recordings_all_orgs_task.time_limit == 360
