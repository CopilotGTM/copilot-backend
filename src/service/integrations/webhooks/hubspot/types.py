from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HubSpotWebhookEvent:
    subscription_type: str
    portal_id: str
    object_id: str
    event_id: str | None
    occurred_at: str | None
    raw: dict[str, Any]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> HubSpotWebhookEvent | None:
        subscription_type = payload.get("subscriptionType")
        portal_id = payload.get("portalId")
        object_id = payload.get("objectId")
        if subscription_type is None or portal_id is None or object_id is None:
            return None

        event_id = payload.get("eventId")
        occurred_at = payload.get("occurredAt")
        return cls(
            subscription_type=str(subscription_type),
            portal_id=str(portal_id),
            object_id=str(object_id),
            event_id=str(event_id) if event_id is not None else None,
            occurred_at=str(occurred_at) if occurred_at is not None else None,
            raw=payload,
        )

    def to_task_kwargs(self) -> dict[str, Any]:
        return {
            "subscription_type": self.subscription_type,
            "portal_id": self.portal_id,
            "object_id": self.object_id,
            "event_id": self.event_id,
            "occurred_at": self.occurred_at,
            "event_payload": self.raw,
        }

    @classmethod
    def from_task_kwargs(
        cls,
        *,
        subscription_type: str,
        portal_id: str,
        object_id: str,
        event_id: str | None = None,
        occurred_at: str | None = None,
        event_payload: dict[str, Any] | None = None,
    ) -> HubSpotWebhookEvent:
        return cls(
            subscription_type=subscription_type,
            portal_id=portal_id,
            object_id=object_id,
            event_id=event_id,
            occurred_at=occurred_at,
            raw=event_payload or {},
        )
