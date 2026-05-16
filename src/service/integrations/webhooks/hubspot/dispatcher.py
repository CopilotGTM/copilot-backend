"""HubSpot webhook dispatcher facade.

The public route calls this facade to avoid knowing the subscription table
shape. HubSpot-specific subscription wiring stays in `subscriptions.py`, while
the dispatch mechanics stay provider-neutral.
"""

from __future__ import annotations

from src.infra.utils.logger import get_logger
from src.service.integrations.webhooks.common.registry import WebhookDispatcher
from src.service.integrations.webhooks.hubspot.subscriptions import (
    HUBSPOT_WEBHOOK_SUBSCRIPTIONS,
)
from src.service.integrations.webhooks.hubspot.types import HubSpotWebhookEvent

logger = get_logger(__name__)

_DISPATCHER = WebhookDispatcher[HubSpotWebhookEvent](
    provider="hubspot",
    subscriptions=HUBSPOT_WEBHOOK_SUBSCRIPTIONS,
)


class HubSpotWebhookDispatcher:
    """HubSpot adapter over the provider-agnostic webhook dispatcher."""

    @classmethod
    def is_supported(cls, subscription_type: str) -> bool:
        """Expose route-time filtering without leaking registry internals."""
        return _DISPATCHER.is_supported(subscription_type)

    @classmethod
    async def dispatch(cls, event: HubSpotWebhookEvent) -> bool:
        """Dispatch one HubSpot event to the registered subscription handler."""
        handled = await _DISPATCHER.dispatch(
            event=event,
            subscription_type=event.subscription_type,
        )
        if not handled:
            logger.info(
                "Ignoring unsupported HubSpot webhook event",
                extra={
                    "event": "hubspot_webhook_event_unsupported",
                    "subscription_type": event.subscription_type,
                    "portal_id": event.portal_id,
                    "object_id": event.object_id,
                    "event_id": event.event_id,
                },
            )
        return handled
