"""Celery entry point for HubSpot webhook events.

The public FastAPI route keeps HTTP-specific validation and dedupe concerns;
this task rebuilds the typed event and hands it to the provider dispatcher in
the integrations worker process.
"""

from __future__ import annotations

import random
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
_MAX_RETRY_COUNTDOWN_SECONDS = 15 * 60


class HubSpotWebhookTaskPayloadError(ValueError):
    """Raised when a webhook task message is malformed before dispatch."""


def _event_property_name(event: HubSpotWebhookEvent) -> str | None:
    property_name = event.raw.get("propertyName")
    return str(property_name) if property_name is not None else None


def _validate_task_payload(
    *,
    subscription_type: str,
    portal_id: str,
    object_id: str,
    event_payload: dict[str, Any] | None,
) -> None:
    missing = [
        name
        for name, value in {
            "subscription_type": subscription_type,
            "portal_id": portal_id,
            "object_id": object_id,
        }.items()
        if not value
    ]
    if missing:
        raise HubSpotWebhookTaskPayloadError(
            "Missing HubSpot webhook task fields: " + ", ".join(sorted(missing))
        )
    if event_payload is not None and not isinstance(event_payload, dict):
        raise HubSpotWebhookTaskPayloadError("HubSpot webhook event_payload must be a dict")


def _retry_countdown(retries: int) -> int:
    base_delay = int(_L.get("retry_delay", 60))
    exponential_delay = min(base_delay * (2**retries), _MAX_RETRY_COUNTDOWN_SECONDS)
    jitter = random.uniform(0, max(1, base_delay * 0.25))  # noqa: S311  # nosec B311
    return int(exponential_delay + jitter)


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
    """Dispatch one already-verified HubSpot webhook event.

    The task is safe to retry because handlers are expected to be idempotent:
    upserts reuse provider external IDs, and deletion webhooks soft-delete by
    org/integration/object identity.
    """
    try:
        _validate_task_payload(
            subscription_type=subscription_type,
            portal_id=portal_id,
            object_id=object_id,
            event_payload=event_payload,
        )
        event = HubSpotWebhookEvent.from_task_kwargs(
            subscription_type=subscription_type,
            portal_id=portal_id,
            object_id=object_id,
            event_id=event_id,
            occurred_at=occurred_at,
            event_payload=event_payload,
        )
    except HubSpotWebhookTaskPayloadError as exc:
        logger.warning(
            "Rejected malformed HubSpot webhook task payload",
            extra={
                "event": "hubspot_webhook_event_task_payload_rejected",
                "subscription_type": subscription_type,
                "portal_id": portal_id,
                "object_id": object_id,
                "event_id": event_id,
                "reason": str(exc),
            },
        )
        return {
            "subscription_type": subscription_type,
            "portal_id": portal_id,
            "object_id": object_id,
            "event_id": event_id,
            "handled": False,
            "status": "rejected",
            "reason": str(exc),
        }

    logger.info(
        "HubSpot webhook event task starting",
        extra={
            "event": "hubspot_webhook_event_task_started",
            "subscription_type": event.subscription_type,
            "portal_id": event.portal_id,
            "object_id": event.object_id,
            "event_id": event.event_id,
            "property_name": _event_property_name(event),
        },
    )

    try:
        handled = run_async(HubSpotWebhookDispatcher.dispatch(event))
    except Exception as exc:
        retry_count = int(getattr(self.request, "retries", 0) or 0)
        max_retries = int(self.max_retries or 0)
        log_extra = {
            "event": "hubspot_webhook_event_task_dispatch_failed",
            "subscription_type": event.subscription_type,
            "portal_id": event.portal_id,
            "object_id": event.object_id,
            "event_id": event.event_id,
            "property_name": _event_property_name(event),
            "retry_count": retry_count,
            "max_retries": max_retries,
            "error_class": type(exc).__name__,
        }
        if retry_count >= max_retries:
            logger.exception(
                "HubSpot webhook event task failed permanently",
                extra={**log_extra, "event": "hubspot_webhook_event_task_failed_permanently"},
            )
            raise

        countdown = _retry_countdown(retry_count)
        logger.warning(
            "Retrying HubSpot webhook event task after dispatch failure",
            extra={**log_extra, "retry_countdown_seconds": countdown},
            exc_info=True,
        )
        raise self.retry(exc=exc, countdown=countdown) from exc

    return {
        "subscription_type": event.subscription_type,
        "portal_id": event.portal_id,
        "object_id": event.object_id,
        "event_id": event.event_id,
        "handled": bool(handled),
    }
