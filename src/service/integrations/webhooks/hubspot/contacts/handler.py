"""HubSpot contact webhook handlers."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.hubspot.contacts.contact_sync_service import (
    HubSpotContactRecordSyncService,
)
from src.service.integrations.webhooks.hubspot.contacts.subscriptions import (
    CONTACT_CREATION,
    CONTACT_DELETION,
    CONTACT_PROPERTY_CHANGE,
)
from src.service.integrations.webhooks.hubspot.portal_resolver import (
    iter_hubspot_integrations_for_portal,
)
from src.service.integrations.webhooks.hubspot.types import HubSpotWebhookEvent
from src.workers.integrations_worker.providers.crm.hubspot.single_contact_fetcher import (
    HubSpotSingleContactFetcher,
)

logger = get_logger(__name__)


class HubSpotContactWebhookHandler:
    """Handle HubSpot contact webhooks by updating one org-scoped contact row."""

    @classmethod
    async def handle(cls, event: HubSpotWebhookEvent) -> None:
        """Route a supported HubSpot contact event to the correct write path."""
        if event.subscription_type == CONTACT_DELETION:
            await cls._handle_deletion(event)
            return

        if event.subscription_type in {CONTACT_CREATION, CONTACT_PROPERTY_CHANGE}:
            await cls._handle_upsert(event)
            return

        logger.info(
            "Ignoring unsupported HubSpot contact webhook event",
            extra={
                "event": "hubspot_contact_webhook_event_unsupported",
                "subscription_type": event.subscription_type,
                "portal_id": event.portal_id,
                "object_id": event.object_id,
                "event_id": event.event_id,
            },
        )

    @classmethod
    async def _handle_upsert(cls, event: HubSpotWebhookEvent) -> None:
        """Fetch the current contact snapshot and persist only that contact."""
        service = HubSpotContactRecordSyncService()
        async for target in iter_hubspot_integrations_for_portal(event.portal_id):
            token_info = await service._load_token_info(
                target.organisation_id,
                target.integration_id,
            )
            raw_contact = await HubSpotSingleContactFetcher(
                token_info=token_info,
                contact_id=event.object_id,
            ).fetch()
            if raw_contact is None:
                await cls._soft_delete_contact(
                    organisation_id=target.organisation_id,
                    integration_id=target.integration_id,
                    external_id=event.object_id,
                )
                continue

            await service.persist_raw_contact(
                organisation_id=target.organisation_id,
                integration_id=target.integration_id,
                raw_contact=raw_contact,
            )

    @classmethod
    async def _handle_deletion(cls, event: HubSpotWebhookEvent) -> None:
        """Soft-delete the matching local contact for every portal-matched integration."""
        async for target in iter_hubspot_integrations_for_portal(event.portal_id):
            await cls._soft_delete_contact(
                organisation_id=target.organisation_id,
                integration_id=target.integration_id,
                external_id=event.object_id,
            )

    @staticmethod
    async def _soft_delete_contact(
        *,
        organisation_id: UUID,
        integration_id: UUID,
        external_id: str,
    ) -> bool:
        """Mark one org-scoped HubSpot contact deleted without PII scrubbing."""
        async for session in acquire_org_async_session(organisation_id):
            statement = select(CRMContact).where(
                col(CRMContact.organisation_id) == organisation_id,
                col(CRMContact.integration_id) == integration_id,
                col(CRMContact.external_id) == external_id,
                col(CRMContact.is_deleted).is_(False),
            )
            contact = (await session.execute(statement)).scalars().first()
            if contact is None:
                return False

            # Normal deletion is a tombstone only; privacy deletion remains the
            # dedicated path that scrubs PII and dependent references.
            contact.is_deleted = True
            contact.synced_at = datetime.now(UTC)
            session.add(contact)
            await session.commit()
            return True
        return False
