from __future__ import annotations

from collections.abc import Awaitable, Callable

from src.infra.utils.logger import get_logger
from src.service.integrations.webhooks.hubspot.constants import CONTACT_PRIVACY_DELETION
from src.service.integrations.webhooks.hubspot.handlers.contact_privacy_deletion import (
    HubSpotContactPrivacyDeletionHandler,
)
from src.service.integrations.webhooks.hubspot.types import HubSpotWebhookEvent

logger = get_logger(__name__)

HubSpotWebhookHandler = Callable[[HubSpotWebhookEvent], Awaitable[None]]


class HubSpotWebhookDispatcher:
    """Dispatch normalized HubSpot webhook events to subscription handlers."""

    _handlers: dict[str, HubSpotWebhookHandler] = {
        CONTACT_PRIVACY_DELETION: HubSpotContactPrivacyDeletionHandler.handle,
    }

    @classmethod
    def is_supported(cls, subscription_type: str) -> bool:
        return subscription_type in cls._handlers

    @classmethod
    async def dispatch(cls, event: HubSpotWebhookEvent) -> bool:
        handler = cls._handlers.get(event.subscription_type)
        if handler is None:
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
            return False

        await handler(event)
        return True
