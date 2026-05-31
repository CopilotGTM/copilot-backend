"""Regression tests for Google subscription Celery task adapters.

The task module should stay a thin Celery boundary: parse ids, delegate to the
service layer, and fan out renewal work on the priority queue.
"""

from uuid import UUID

from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.webhooks.google.metadata import GoogleSubscriptionKind
from src.workers.integrations_worker.tasks import google_subscription_tasks as tasks


def test_kind_for_integration_type_maps_only_google_v1_types() -> None:
    """Only Gmail and Google Calendar should map to Google webhook kinds."""
    assert tasks._kind_for_integration_type(IntegrationType.GMAIL) == GoogleSubscriptionKind.GMAIL
    assert (
        tasks._kind_for_integration_type(IntegrationType.GOOGLE_CALENDAR)
        == GoogleSubscriptionKind.CALENDAR
    )
    assert tasks._kind_for_integration_type(IntegrationType.OUTLOOK_EMAIL) is None


def test_ensure_google_subscription_task_returns_success(monkeypatch) -> None:
    """The ensure task must preserve service results and add timing metadata."""

    async def fake_ensure_subscription(self, *, organisation_id, integration_id):
        """Return a successful service result without hitting provider APIs."""
        return {
            "status": "success",
            "organisation_id": str(organisation_id),
            "integration_id": str(integration_id),
            "kind": "gmail",
        }

    monkeypatch.setattr(
        tasks.GoogleSubscriptionTaskService,
        "ensure_subscription",
        fake_ensure_subscription,
    )

    result = tasks.ensure_google_subscription_task.run(
        organisation_id="11111111-1111-1111-1111-111111111111",
        integration_id="22222222-2222-2222-2222-222222222222",
    )

    assert result["status"] == "success"
    assert result["kind"] == "gmail"
    assert result["started_at"]
    assert result["completed_at"]


def test_renew_google_subscriptions_all_dispatches_priority_ensure(monkeypatch) -> None:
    """Scheduled renewal must fan out per-integration ensures to the priority queue."""
    dispatched: list[dict] = []

    async def fake_dispatch_all_subscription_ensures(self, *, dispatch_subscription_ensure):
        """Exercise the task's dispatch callback with one eligible integration."""
        dispatch_subscription_ensure(
            UUID("11111111-1111-1111-1111-111111111111"),
            UUID("22222222-2222-2222-2222-222222222222"),
        )
        return {
            "status": "success",
            "orgs_checked": 1,
            "integrations_dispatched": 1,
        }

    def fake_apply_async(*, kwargs, queue, routing_key):
        """Capture Celery dispatch arguments without requiring a broker."""
        dispatched.append(
            {
                "kwargs": kwargs,
                "queue": queue,
                "routing_key": routing_key,
            }
        )

    monkeypatch.setattr(
        tasks.GoogleSubscriptionTaskService,
        "dispatch_all_subscription_ensures",
        fake_dispatch_all_subscription_ensures,
    )
    monkeypatch.setattr(tasks.ensure_google_subscription_task, "apply_async", fake_apply_async)

    result = tasks.renew_google_subscriptions_all_task.run()

    assert result["status"] == "success"
    assert dispatched == [
        {
            "kwargs": {
                "organisation_id": "11111111-1111-1111-1111-111111111111",
                "integration_id": "22222222-2222-2222-2222-222222222222",
            },
            "queue": "integrations_priority",
            "routing_key": "integrations_priority",
        }
    ]
