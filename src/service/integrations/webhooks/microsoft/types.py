"""Typed webhook notification parsing for Microsoft Graph payloads.

This module converts untrusted webhook JSON into validated in-process objects.
It also provides a deterministic dedupe token so we can collapse provider
retries without dropping unique notifications.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MicrosoftGraphWebhookNotification:
    """Validated webhook notification payload used by ingestion service."""

    subscription_id: str
    client_state: str
    change_type: str
    resource: str
    notification_id: str | None
    tenant_id: str | None
    resource_data: dict[str, Any] | None
    raw: dict[str, Any]

    @classmethod
    def from_payload(
        cls,
        payload: Any,
    ) -> MicrosoftGraphWebhookNotification | None:
        """Return a typed notification when required Graph fields are present."""
        if not isinstance(payload, dict):
            return None

        subscription_id = payload.get("subscriptionId")
        client_state = payload.get("clientState")
        change_type = payload.get("changeType") or payload.get("lifecycleEvent")
        resource = payload.get("resource")

        if (
            not isinstance(subscription_id, str)
            or not isinstance(client_state, str)
            or not isinstance(change_type, str)
            or not isinstance(resource, str)
        ):
            return None

        notification_id = payload.get("id")
        tenant_id = payload.get("tenantId")
        resource_data = payload.get("resourceData")

        return cls(
            subscription_id=subscription_id,
            client_state=client_state,
            change_type=change_type,
            resource=resource,
            notification_id=notification_id if isinstance(notification_id, str) else None,
            tenant_id=tenant_id if isinstance(tenant_id, str) else None,
            resource_data=resource_data if isinstance(resource_data, dict) else None,
            raw=payload,
        )

    def dedupe_token(self) -> str:
        """Return a stable dedupe token for idempotent webhook processing."""
        if self.notification_id:
            return self.notification_id

        raw_payload = json.dumps(self.raw, sort_keys=True, default=str)

        fingerprint = "|".join(
            [
                self.subscription_id,
                self.change_type,
                self.resource,
                raw_payload,
            ]
        )
        return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
