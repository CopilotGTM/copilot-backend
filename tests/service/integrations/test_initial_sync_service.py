"""Regression tests for post-connect initial sync fanout.

The service is the bridge between OAuth completion and worker tasks. These
tests lock the ordering where provider sync is dispatched before webhook
subscription ensure tasks.
"""

from uuid import UUID

from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.service.integrations.initial_sync_service import InitialSyncService


def test_outlook_calendar_connect_triggers_sync_and_subscription(monkeypatch) -> None:
    """Outlook Calendar keeps the existing Microsoft sync-then-subscribe contract."""
    sent_tasks: list[dict] = []

    def fake_send_integrations_task(
        task_name,
        args=None,
        kwargs=None,
        queue="integrations",
        routing_key=None,
        headers=None,
        countdown=None,
    ):
        """Capture task dispatches without importing Celery in the service test."""
        sent_tasks.append(
            {
                "task_name": task_name,
                "kwargs": kwargs,
                "queue": queue,
                "routing_key": routing_key,
            }
        )
        return "task-id"

    monkeypatch.setattr(
        "src.service.integrations.initial_sync_service.send_integrations_task",
        fake_send_integrations_task,
    )

    integration = KnowledgeIntegration(
        integration_id=UUID("22222222-2222-2222-2222-222222222222"),
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        created_by_user_id=UUID("33333333-3333-3333-3333-333333333333"),
        integration_type=IntegrationType.OUTLOOK_CALENDAR,
        name="Outlook Calendar",
        credentials={},
        connection_id="conn",
    )

    InitialSyncService().trigger_initial_sync(
        organisation_id=integration.organisation_id,
        integration=integration,
    )

    assert [task["task_name"] for task in sent_tasks] == [
        "integrations.sync_calendar_integration",
        "integrations.ensure_microsoft_graph_subscription",
    ]
    assert sent_tasks[1]["queue"] == "integrations_priority"
    assert sent_tasks[1]["routing_key"] == "integrations_priority"


def test_gmail_connect_triggers_sync_and_subscription(monkeypatch) -> None:
    """Gmail must run initial email sync before ensuring the Gmail watch."""
    sent_tasks: list[dict] = []

    def fake_send_integrations_task(
        task_name,
        args=None,
        kwargs=None,
        queue="integrations",
        routing_key=None,
        headers=None,
        countdown=None,
    ):
        """Capture task dispatches so the test can assert ordering and queue."""
        sent_tasks.append(
            {
                "task_name": task_name,
                "kwargs": kwargs,
                "queue": queue,
                "routing_key": routing_key,
            }
        )
        return "task-id"

    monkeypatch.setattr(
        "src.service.integrations.initial_sync_service.send_integrations_task",
        fake_send_integrations_task,
    )

    integration = KnowledgeIntegration(
        integration_id=UUID("22222222-2222-2222-2222-222222222222"),
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        created_by_user_id=UUID("33333333-3333-3333-3333-333333333333"),
        integration_type=IntegrationType.GMAIL,
        name="Gmail",
        credentials={},
        connection_id="conn",
    )

    InitialSyncService().trigger_initial_sync(
        organisation_id=integration.organisation_id,
        integration=integration,
    )

    assert [task["task_name"] for task in sent_tasks] == [
        "integrations.sync_email_integration",
        "integrations.ensure_google_subscription",
    ]
    assert sent_tasks[1]["queue"] == "integrations_priority"
    assert sent_tasks[1]["routing_key"] == "integrations_priority"


def test_google_calendar_connect_triggers_sync_and_subscription(monkeypatch) -> None:
    """Google Calendar must run initial sync before creating the channel watch."""
    sent_tasks: list[dict] = []

    def fake_send_integrations_task(
        task_name,
        args=None,
        kwargs=None,
        queue="integrations",
        routing_key=None,
        headers=None,
        countdown=None,
    ):
        """Capture task dispatches so the test can assert ordering and queue."""
        sent_tasks.append(
            {
                "task_name": task_name,
                "kwargs": kwargs,
                "queue": queue,
                "routing_key": routing_key,
            }
        )
        return "task-id"

    monkeypatch.setattr(
        "src.service.integrations.initial_sync_service.send_integrations_task",
        fake_send_integrations_task,
    )

    integration = KnowledgeIntegration(
        integration_id=UUID("22222222-2222-2222-2222-222222222222"),
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        created_by_user_id=UUID("33333333-3333-3333-3333-333333333333"),
        integration_type=IntegrationType.GOOGLE_CALENDAR,
        name="Google Calendar",
        credentials={},
        connection_id="conn",
    )

    InitialSyncService().trigger_initial_sync(
        organisation_id=integration.organisation_id,
        integration=integration,
    )

    assert [task["task_name"] for task in sent_tasks] == [
        "integrations.sync_calendar_integration",
        "integrations.ensure_google_subscription",
    ]
    assert sent_tasks[1]["queue"] == "integrations_priority"
    assert sent_tasks[1]["routing_key"] == "integrations_priority"
