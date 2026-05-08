from __future__ import annotations

from typing import Any

from celery import Task

from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations.webhooks.hubspot.constants import HUBSPOT_WEBHOOK_EVENT_TASK
from src.service.integrations.webhooks.hubspot.dispatcher import HubSpotWebhookDispatcher
from src.service.integrations.webhooks.hubspot.types import HubSpotWebhookEvent
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.config import TASK_LIMITS

logger = get_logger(__name__)

_L = TASK_LIMITS[HUBSPOT_WEBHOOK_EVENT_TASK]


@app.task(
    name=HUBSPOT_WEBHOOK_EVENT_TASK,
    bind=True,
    max_retries=_L["retries"],
    default_retry_delay=_L.get("retry_delay", 60),
    acks_late=True,
    soft_time_limit=_L["soft"],
    time_limit=_L["hard"],
)
def process_hubspot_webhook_event_task(
    self: Task,
    *,
    subscription_type: str,
    portal_id: str,
    object_id: str,
    event_id: str | None = None,
    occurred_at: str | None = None,
    event_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = HubSpotWebhookEvent.from_task_kwargs(
        subscription_type=subscription_type,
        portal_id=portal_id,
        object_id=object_id,
        event_id=event_id,
        occurred_at=occurred_at,
        event_payload=event_payload,
    )
    logger.info(
        "HubSpot webhook event task starting",
        extra={
            "event": "hubspot_webhook_event_task_started",
            "subscription_type": event.subscription_type,
            "portal_id": event.portal_id,
            "object_id": event.object_id,
            "event_id": event.event_id,
        },
    )

    handled = run_async(HubSpotWebhookDispatcher.dispatch(event))

    return {
        "subscription_type": event.subscription_type,
        "portal_id": event.portal_id,
        "object_id": event.object_id,
        "event_id": event.event_id,
        "handled": bool(handled),
    }
