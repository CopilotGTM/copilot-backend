"""Regression tests for post-connect initial sync fanout.

The service is the bridge between OAuth completion and worker tasks. These
tests lock the ordering where provider sync is dispatched before webhook
subscription ensure tasks.
"""

from unittest.mock import MagicMock
from uuid import UUID

from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.service.integrations import initial_sync_service
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
    client = MagicMock()
    client.signature.side_effect = ["calendar-sync", "calendar-discovery"]
    chain_signature = MagicMock()
    monkeypatch.setattr(
        initial_sync_service,
        "get_integrations_celery_client",
        lambda: client,
    )
    monkeypatch.setattr(
        initial_sync_service,
        "chain",
        lambda *args: chain_signature,
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
        "integrations.ensure_microsoft_graph_subscription",
    ]
    assert client.signature.call_count == 2
    client.signature.assert_any_call(
        "integrations.sync_calendar_integration",
        kwargs={
            "organisation_id": str(integration.organisation_id),
            "integration_id": str(integration.integration_id),
        },
        immutable=True,
    )
    client.signature.assert_any_call(
        "deal.discover_calendar_contacts_accounts",
        kwargs={
            "organisation_id": str(integration.organisation_id),
            "source_integration_id": str(integration.integration_id),
        },
        queue="deal",
        routing_key="deal",
    )
    chain_signature.apply_async.assert_called_once_with()
    assert sent_tasks[0]["queue"] == "integrations_priority"
    assert sent_tasks[0]["routing_key"] == "integrations_priority"


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
    client = MagicMock()
    client.signature.side_effect = ["calendar-sync", "calendar-discovery"]
    chain_signature = MagicMock()
    monkeypatch.setattr(
        initial_sync_service,
        "get_integrations_celery_client",
        lambda: client,
    )
    monkeypatch.setattr(
        initial_sync_service,
        "chain",
        lambda *args: chain_signature,
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
    client = MagicMock()
    client.signature.side_effect = ["calendar-sync", "calendar-discovery"]
    chain_signature = MagicMock()
    monkeypatch.setattr(
        "src.service.integrations.initial_sync_service.get_integrations_celery_client",
        lambda: client,
    )
    monkeypatch.setattr(
        "src.service.integrations.initial_sync_service.chain",
        MagicMock(return_value=chain_signature),
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
        "integrations.ensure_google_subscription",
    ]
    assert client.signature.call_count == 2
    client.signature.assert_any_call(
        "integrations.sync_calendar_integration",
        kwargs={
            "organisation_id": str(integration.organisation_id),
            "integration_id": str(integration.integration_id),
        },
        immutable=True,
    )
    client.signature.assert_any_call(
        "deal.discover_calendar_contacts_accounts",
        kwargs={
            "organisation_id": str(integration.organisation_id),
            "source_integration_id": str(integration.integration_id),
        },
        queue="deal",
        routing_key="deal",
    )
    chain_signature.apply_async.assert_called_once_with()
    assert sent_tasks[0]["queue"] == "integrations_priority"
    assert sent_tasks[0]["routing_key"] == "integrations_priority"
