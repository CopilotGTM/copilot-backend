"""Microsoft Graph subscription lifecycle management for Outlook integrations.

Called by post-connect setup and periodic renewal tasks. This module creates or
renews Graph subscriptions and returns updated `connection_metadata`; database
writes stay in callers so session ownership remains explicit.
"""

from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID

import httpx
from sqlalchemy.orm import attributes
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    get_access_token,
)
from src.service.integrations.webhooks.microsoft.metadata import (
    MicrosoftSubscriptionKind,
    get_subscription_metadata,
    set_subscription_metadata,
)
from src.workers.integrations_worker.providers._microsoft_graph import (
    graph_api_patch_json,
    graph_api_post_json,
)

# Subscribe to every mutation type because the downstream delta sync owns the
# exact created/updated/deleted interpretation.
MICROSOFT_GRAPH_SUBSCRIPTION_CHANGE_TYPE = "created,updated,deleted"

# Renew a full day before expiry so worker delays or Graph throttling still
# leave enough overlap for the old subscription to keep delivering callbacks.
MICROSOFT_GRAPH_RENEWAL_BUFFER = timedelta(hours=24)

MICROSOFT_GRAPH_OUTLOOK_MAX_SUBSCRIPTION_LIFETIME = timedelta(minutes=4230)

_RESOURCE_BY_KIND = {
    MicrosoftSubscriptionKind.CALENDAR: "/me/events",
    MicrosoftSubscriptionKind.EMAIL: "/me/messages",
}

_INTEGRATION_TYPE_BY_KIND = {
    MicrosoftSubscriptionKind.CALENDAR: IntegrationType.OUTLOOK_CALENDAR,
    MicrosoftSubscriptionKind.EMAIL: IntegrationType.OUTLOOK_EMAIL,
}


class MicrosoftGraphSubscriptionService:
    """Create and renew Graph webhook subscriptions for one integration."""

    async def ensure_subscription_metadata(
        self,
        *,
        organisation_id: UUID,
        integration: KnowledgeIntegration,
        kind: MicrosoftSubscriptionKind,
    ) -> dict:
        """Return metadata with a valid subscription, creating/renewing as needed."""
        self._validate_kind(integration, kind)
        existing = get_subscription_metadata(integration.connection_metadata, kind)
        if existing and existing.expires_at > datetime.now(UTC) + MICROSOFT_GRAPH_RENEWAL_BUFFER:
            return dict(integration.connection_metadata or {})
        if existing:
            return await self.renew_subscription_metadata(
                organisation_id=organisation_id,
                integration=integration,
                kind=kind,
            )

        return await self._create_subscription_metadata(
            organisation_id=organisation_id,
            integration=integration,
            kind=kind,
        )

    async def _create_subscription_metadata(
        self,
        *,
        organisation_id: UUID,
        integration: KnowledgeIntegration,
        kind: MicrosoftSubscriptionKind,
    ) -> dict:
        """Create a brand-new Graph subscription and return updated metadata.

        The caller must persist the returned metadata atomically with the
        integration row so webhook callbacks do not arrive before subscription
        state is stored.
        """
        payload = self._build_subscription_payload(
            organisation_id=organisation_id,
            integration_id=integration.integration_id,
            kind=kind,
            client_state=secrets.token_urlsafe(32),
        )
        token_info = IntegrationTokenInfo.from_integration(integration, organisation_id)
        # The worker service passes a detached integration snapshot here; no org
        # DB session is held while Graph performs the network call.
        access_token = await get_access_token(token_info)
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        ) as client:
            response = await graph_api_post_json(
                client,
                "/v1.0/subscriptions",
                json_body=payload,
            )

        subscription_id = response.get("id")
        expires_at_raw = response.get("expirationDateTime")
        if not isinstance(subscription_id, str) or not isinstance(expires_at_raw, str):
            raise RuntimeError("Microsoft Graph subscription response missing id or expiration")

        return set_subscription_metadata(
            integration.connection_metadata,
            kind=kind,
            subscription_id=subscription_id,
            client_state=payload["clientState"],
            resource=payload["resource"],
            change_type=payload["changeType"],
            expires_at=_parse_graph_datetime(expires_at_raw),
            notification_url=payload["notificationUrl"],
            lifecycle_notification_url=payload["lifecycleNotificationUrl"],
        )

    async def renew_subscription_metadata(
        self,
        *,
        organisation_id: UUID,
        integration: KnowledgeIntegration,
        kind: MicrosoftSubscriptionKind,
    ) -> dict:
        """Renew an existing subscription or recreate it when Graph has purged it."""
        self._validate_kind(integration, kind)
        existing = get_subscription_metadata(integration.connection_metadata, kind)
        if existing is None:
            return await self._create_subscription_metadata(
                organisation_id=organisation_id,
                integration=integration,
                kind=kind,
            )
        if existing.expires_at <= datetime.now(UTC):
            return await self._create_subscription_metadata(
                organisation_id=organisation_id,
                integration=integration,
                kind=kind,
            )
        if existing.expires_at > datetime.now(UTC) + MICROSOFT_GRAPH_RENEWAL_BUFFER:
            return dict(integration.connection_metadata or {})

        token_info = IntegrationTokenInfo.from_integration(integration, organisation_id)
        # Renewal uses the same detached snapshot pattern as creation so a slow
        # Graph PATCH cannot pin a tenant connection.
        access_token = await get_access_token(token_info)
        new_expiration = _requested_expiration()
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        ) as client:
            try:
                response = await graph_api_patch_json(
                    client,
                    f"/v1.0/subscriptions/{existing.subscription_id}",
                    json_body={"expirationDateTime": _format_graph_datetime(new_expiration)},
                )
            except httpx.HTTPStatusError as exc:
                # Graph can return 404/410 for expired or already-removed IDs.
                # Recreate immediately so next webhook cycle can resume.
                if exc.response.status_code in {404, 410}:
                    return await self._create_subscription_metadata(
                        organisation_id=organisation_id,
                        integration=integration,
                        kind=kind,
                    )
                raise

        expires_at = _parse_graph_datetime(
            response.get("expirationDateTime") or _format_graph_datetime(new_expiration)
        )
        return set_subscription_metadata(
            integration.connection_metadata,
            kind=kind,
            subscription_id=existing.subscription_id,
            client_state=existing.client_state,
            resource=existing.resource,
            change_type=existing.change_type,
            expires_at=expires_at,
            notification_url=existing.notification_url,
            lifecycle_notification_url=existing.lifecycle_notification_url,
        )

    def _build_subscription_payload(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        kind: MicrosoftSubscriptionKind,
        client_state: str,
    ) -> dict[str, str]:
        """Build Graph subscription payload with org-scoped callback URLs."""
        raw_base_url = os.environ.get("MICROSOFT_GRAPH_WEBHOOK_BASE_URL")
        if not raw_base_url:
            raise RuntimeError("MICROSOFT_GRAPH_WEBHOOK_BASE_URL is required")
        base_url = raw_base_url.rstrip("/")
        # These query params are the routing contract for future callbacks; the
        # route refuses notifications that cannot be mapped back to one org row.
        query = urlencode(
            {
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
                "kind": kind.value,
            }
        )
        return {
            "changeType": MICROSOFT_GRAPH_SUBSCRIPTION_CHANGE_TYPE,
            "notificationUrl": f"{base_url}/users/integrations/microsoft/webhook?{query}",
            "lifecycleNotificationUrl": (
                f"{base_url}/users/integrations/microsoft/webhook/lifecycle?{query}"
            ),
            "resource": _RESOURCE_BY_KIND[kind],
            "expirationDateTime": _format_graph_datetime(_requested_expiration()),
            "clientState": client_state,
        }

    def _validate_kind(
        self,
        integration: KnowledgeIntegration,
        kind: MicrosoftSubscriptionKind,
    ) -> None:
        """Enforce integration-type and subscription-kind compatibility."""
        integration_type = (
            IntegrationType(integration.integration_type)
            if isinstance(integration.integration_type, str)
            else integration.integration_type
        )
        if integration_type != _INTEGRATION_TYPE_BY_KIND[kind]:
            raise ValueError(
                f"Cannot create {kind.value} subscription for {integration_type.value}"
            )


def persist_subscription_metadata(
    integration: KnowledgeIntegration,
    metadata: dict,
) -> None:
    """Persist connection metadata and mark JSON field as mutated for SQLAlchemy."""
    integration.connection_metadata = metadata
    attributes.flag_modified(integration, "connection_metadata")


def _requested_expiration() -> datetime:
    """Return requested expiration within Graph's max subscription window."""
    return datetime.now(UTC) + MICROSOFT_GRAPH_OUTLOOK_MAX_SUBSCRIPTION_LIFETIME


def _format_graph_datetime(value: datetime) -> str:
    """Format datetime in the `Z`-suffixed shape expected by Graph."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_graph_datetime(value: str) -> datetime:
    """Parse Graph datetime values into UTC-aware datetime objects."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
