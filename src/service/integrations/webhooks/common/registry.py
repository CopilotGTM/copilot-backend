"""Provider-neutral webhook subscription dispatch primitives.

Provider modules register their own subscription types and handlers against
this small registry. Keeping the registry generic lets Salesforce or other CRM
providers reuse the same dispatch shape without importing HubSpot code.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Generic, TypeVar

from src.infra.utils.logger import get_logger

logger = get_logger(__name__)

EventT = TypeVar("EventT")
WebhookHandler = Callable[[EventT], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class WebhookSubscription(Generic[EventT]):
    """A single provider subscription type and its async handler."""

    subscription_type: str
    handler: WebhookHandler[EventT]


class WebhookDispatcher(Generic[EventT]):
    """Provider-agnostic subscription registry and dispatcher."""

    def __init__(
        self,
        *,
        provider: str,
        subscriptions: Iterable[WebhookSubscription[EventT]],
    ) -> None:
        self.provider = provider
        self._handlers = {
            subscription.subscription_type: subscription.handler for subscription in subscriptions
        }

    def is_supported(self, subscription_type: str) -> bool:
        """Return whether this provider has an explicit handler for the event."""
        return subscription_type in self._handlers

    async def dispatch(self, *, event: EventT, subscription_type: str) -> bool:
        """Run the matching handler and report whether the event was handled."""
        handler = self._handlers.get(subscription_type)
        if handler is None:
            logger.info(
                "Ignoring unsupported webhook event",
                extra={
                    "event": "webhook_event_unsupported",
                    "provider": self.provider,
                    "subscription_type": subscription_type,
                },
            )
            return False

        await handler(event)
        return True
