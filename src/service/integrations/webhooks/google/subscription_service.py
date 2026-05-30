"""Google webhook subscription lifecycle management.

Called by the integrations worker after post-connect sync and by scheduled
renewal. This module performs Google API calls only after the task service has
loaded a detached integration snapshot, so slow provider requests do not hold
tenant DB sessions open.
"""

from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
from sqlalchemy.orm import attributes
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    get_access_token,
)
from src.service.integrations.webhooks.google.metadata import (
    GoogleSubscriptionKind,
    get_subscription_metadata,
    set_calendar_subscription_metadata,
    set_gmail_subscription_metadata,
)
from src.workers.integrations_worker.providers._google_api import (
    calendar_api_post_json,
    gmail_api_post_json,
)

# Google watches/channels expire. Renew a day early so clock skew, delayed beat
# runs, or transient provider failures do not leave the integration without a
# webhook trigger.
GOOGLE_GMAIL_RENEWAL_BUFFER = timedelta(days=1)
GOOGLE_CALENDAR_RENEWAL_BUFFER = timedelta(days=1)
GOOGLE_CALENDAR_REQUESTED_CHANNEL_LIFETIME = timedelta(days=6)

_INTEGRATION_TYPE_BY_KIND = {
    GoogleSubscriptionKind.GMAIL: IntegrationType.GMAIL,
    GoogleSubscriptionKind.CALENDAR: IntegrationType.GOOGLE_CALENDAR,
}


class GoogleSubscriptionService:
    """Create and refresh Google webhook metadata for one integration."""

    async def ensure_subscription_metadata(
        self,
        *,
        organisation_id: UUID,
        integration: KnowledgeIntegration,
        kind: GoogleSubscriptionKind,
    ) -> dict:
        """Return metadata with a valid Gmail watch or Calendar channel.

        The caller persists the returned metadata in a fresh DB session. Keeping
        this method side-effect-free for the database lets worker orchestration
        release pooled connections before Google API calls.
        """
        self._validate_kind(integration, kind)
        existing = get_subscription_metadata(integration.connection_metadata, kind)
        renewal_buffer = (
            GOOGLE_GMAIL_RENEWAL_BUFFER
            if kind == GoogleSubscriptionKind.GMAIL
            else GOOGLE_CALENDAR_RENEWAL_BUFFER
        )
        if existing and existing.expires_at > datetime.now(UTC) + renewal_buffer:
            # Existing metadata is still outside the renewal window, so avoid a
            # provider call and preserve the current channel/watch identifiers.
            return dict(integration.connection_metadata or {})
        if kind == GoogleSubscriptionKind.GMAIL:
            return await self._create_gmail_watch_metadata(
                organisation_id=organisation_id,
                integration=integration,
            )
        return await self._create_calendar_channel_metadata(
            organisation_id=organisation_id,
            integration=integration,
        )

    async def _create_gmail_watch_metadata(
        self,
        *,
        organisation_id: UUID,
        integration: KnowledgeIntegration,
    ) -> dict:
        """Create Gmail `users.watch` metadata for one connected mailbox."""
        topic_name = os.environ.get("GOOGLE_GMAIL_PUBSUB_TOPIC")
        if not topic_name:
            raise RuntimeError("GOOGLE_GMAIL_PUBSUB_TOPIC is required")

        # Gmail sends notifications only for labels included in the watch. Keep
        # v1 aligned with the monitored INBOX sync path rather than mirroring
        # every mailbox label.
        payload = {
            "topicName": topic_name,
            "labelIds": ["INBOX"],
            "labelFilterBehavior": "INCLUDE",
        }
        token_info = IntegrationTokenInfo.from_integration(integration, organisation_id)
        access_token = await get_access_token(token_info)
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        ) as client:
            # This network call can be slow or rate-limited; the task service
            # has already detached the integration so no org DB session is held.
            response = await gmail_api_post_json(
                client,
                "/gmail/v1/users/me/watch",
                json_body=payload,
            )

        history_id = response.get("historyId")
        expiration = response.get("expiration")
        if not isinstance(history_id, str) or not isinstance(expiration, str):
            raise RuntimeError("Gmail watch response missing historyId or expiration")

        return set_gmail_subscription_metadata(
            integration.connection_metadata,
            watch_history_id=history_id,
            topic_name=topic_name,
            label_ids=["INBOX"],
            label_filter_behavior="INCLUDE",
            expires_at=_parse_epoch_millis(expiration),
        )

    async def _create_calendar_channel_metadata(
        self,
        *,
        organisation_id: UUID,
        integration: KnowledgeIntegration,
    ) -> dict:
        """Create Google Calendar `events.watch` metadata for one primary calendar."""
        channel_id = str(uuid4())
        # Calendar echoes this token back on every notification. It is a routing
        # secret only, never an OAuth credential.
        channel_token = secrets.token_urlsafe(32)
        address = self._calendar_callback_url(
            organisation_id=organisation_id,
            integration_id=integration.integration_id,
        )
        requested_expiration = datetime.now(UTC) + GOOGLE_CALENDAR_REQUESTED_CHANNEL_LIFETIME
        payload = {
            "id": channel_id,
            "type": "web_hook",
            "address": address,
            "token": channel_token,
            "expiration": str(int(requested_expiration.timestamp() * 1000)),
        }

        token_info = IntegrationTokenInfo.from_integration(integration, organisation_id)
        access_token = await get_access_token(token_info)
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        ) as client:
            # Calendar may send a "sync" notification before this response is
            # observed, so ingestion treats that state as an acknowledgement.
            response = await calendar_api_post_json(
                client,
                "/calendar/v3/calendars/primary/events/watch",
                json_body=payload,
            )

        resource_id = response.get("resourceId")
        resource_uri = response.get("resourceUri")
        response_expiration = response.get("expiration") or payload["expiration"]
        if not isinstance(resource_id, str) or not isinstance(resource_uri, str):
            raise RuntimeError("Calendar watch response missing resourceId or resourceUri")

        return set_calendar_subscription_metadata(
            integration.connection_metadata,
            channel_id=channel_id,
            resource_id=resource_id,
            resource_uri=resource_uri,
            channel_token=channel_token,
            address=address,
            expires_at=_parse_epoch_millis(str(response_expiration)),
        )

    def _calendar_callback_url(self, *, organisation_id: UUID, integration_id: UUID) -> str:
        """Build the per-channel callback URL that binds Calendar traffic to one integration."""
        raw_base_url = os.environ.get("GOOGLE_WEBHOOK_BASE_URL")
        if not raw_base_url:
            raise RuntimeError("GOOGLE_WEBHOOK_BASE_URL is required")
        query = urlencode(
            {
                "kind": GoogleSubscriptionKind.CALENDAR.value,
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
            }
        )
        return f"{raw_base_url.rstrip('/')}/users/integrations/google/webhook?{query}"

    def _validate_kind(
        self,
        integration: KnowledgeIntegration,
        kind: GoogleSubscriptionKind,
    ) -> None:
        """Reject subscription creation for the wrong Google integration type."""
        integration_type = (
            IntegrationType(integration.integration_type)
            if isinstance(integration.integration_type, str)
            else integration.integration_type
        )
        if integration_type != _INTEGRATION_TYPE_BY_KIND[kind]:
            raise ValueError(
                f"Cannot create {kind.value} subscription for {integration_type.value}"
            )


def persist_subscription_metadata(integration: KnowledgeIntegration, metadata: dict) -> None:
    """Assign metadata and mark the JSONB field dirty for SQLAlchemy.

    SQLAlchemy does not always detect JSONB replacement/mutation reliably in
    this model, so renewal tasks explicitly flag the field before commit.
    """
    integration.connection_metadata = metadata
    attributes.flag_modified(integration, "connection_metadata")


def _parse_epoch_millis(value: str) -> datetime:
    """Convert Google epoch-millisecond expiration values to UTC datetimes."""
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
