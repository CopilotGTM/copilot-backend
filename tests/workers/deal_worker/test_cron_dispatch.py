"""Cron schedule registry behavior for deal worker heartbeat and dispatch."""

from __future__ import annotations

import pytest
from src.workers.deal_worker.cron_dispatch import dispatch_scheduled_job
from src.workers.deal_worker.scheduled_jobs import get_scheduled_job


def test_get_scheduled_job_returns_deal_mapping_definition() -> None:
    """deal-mapping should resolve to the expected stakeholder sync task definition."""
    job = get_scheduled_job("deal-mapping")

    assert job.name == "deal-mapping"
    assert job.task_name == "stakeholder.sync_all_deals_stakeholders_all_orgs"
    assert job.schedule == "3 */2 * * *"
    assert job.max_lag_seconds == 3 * 60 * 60
    assert job.queue == "deal"
    assert job.kwargs == {}


def test_get_scheduled_job_returns_email_attachment_extraction_definition() -> None:
    """email-attachment-extraction should resolve to the new attachment pipeline task."""
    job = get_scheduled_job("email-attachment-extraction")

    assert job.name == "email-attachment-extraction"
    assert job.task_name == "deal_asset.extract_email_attachments_all_orgs"
    assert job.schedule == "20,50 * * * *"
    assert job.queue == "deal"
    assert job.kwargs == {}


def test_get_scheduled_job_returns_nightly_meeting_bot_fallback_definition() -> None:
    """Meeting bot fallback runs nightly in UTC, not hourly."""
    job = get_scheduled_job("meeting-bot-fallback")

    assert job.name == "meeting-bot-fallback"
    assert job.task_name == "deal.trigger_send_unsent_meeting_recordings_all_orgs"
    assert job.schedule == "0 20 * * *"
    assert job.queue == "deal"
    assert job.kwargs == {}


def test_transcript_fetch_pipeline_schedule_is_disabled() -> None:
    """Recall webhooks now materialize transcripts directly; polling stays disabled."""
    with pytest.raises(KeyError):
        get_scheduled_job("transcript-fetch-pipeline")


def test_timeline_sync_schedule_is_disabled() -> None:
    """Timeline sync now runs deal-scoped after email/calendar mapping."""
    with pytest.raises(KeyError):
        get_scheduled_job("timeline-sync")


def test_get_scheduled_job_rejects_unknown_job_name() -> None:
    """Unknown job names must fail fast with a KeyError."""
    with pytest.raises(KeyError):
        get_scheduled_job("does-not-exist")


def test_dispatch_scheduled_job_enqueues_and_records_heartbeat(mocker) -> None:
    """Successful dispatch should enqueue the task and persist a heartbeat record."""
    send_task = mocker.patch(
        "src.workers.deal_worker.cron_dispatch.send_deal_task",
        return_value="task-123",
    )
    record_dispatch = mocker.patch(
        "src.workers.deal_worker.cron_dispatch.record_scheduler_dispatch"
    )

    task_id = dispatch_scheduled_job("deal-mapping")

    assert task_id == "task-123"
    send_task.assert_called_once_with(
        task_name="stakeholder.sync_all_deals_stakeholders_all_orgs",
        kwargs={},
        queue="deal",
        routing_key="deal",
    )
    record_dispatch.assert_called_once()


def test_dispatch_scheduled_job_records_failure_before_raising(mocker) -> None:
    """Broker failures should be recorded before surfacing the error."""
    mocker.patch(
        "src.workers.deal_worker.cron_dispatch.send_deal_task",
        side_effect=RuntimeError("broker down"),
    )
    record_failure = mocker.patch("src.workers.deal_worker.cron_dispatch.record_scheduler_failure")

    with pytest.raises(RuntimeError, match="broker down"):
        dispatch_scheduled_job("deal-mapping")

    record_failure.assert_called_once()
