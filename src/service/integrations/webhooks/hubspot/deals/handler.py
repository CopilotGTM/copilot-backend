"""HubSpot deal webhook handlers.

HubSpot deal webhooks carry only the portal/object identity, not the full deal
record. This handler resolves the portal to org-scoped integrations and then
reuses the existing deal sync pipeline so webhook sync and full sync write the
same normalized rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.hubspot.deals.deal_sync_service import (
    HubSpotDealRecordSyncService,
)
from src.service.integrations.webhooks.hubspot.deals.subscriptions import (
    DEAL_CREATION,
    DEAL_DELETION,
    DEAL_PROPERTY_CHANGE,
)
from src.service.integrations.webhooks.hubspot.portal_resolver import (
    iter_hubspot_integrations_for_portal,
)
from src.service.integrations.webhooks.hubspot.types import HubSpotWebhookEvent
from src.workers.integrations_worker.providers.crm.hubspot.single_deal_fetcher import (
    HubSpotSingleDealFetcher,
)

logger = get_logger(__name__)


class HubSpotDealWebhookHandler:
    """Handle HubSpot deal webhook events by reusing the record-sync pipeline."""

    @classmethod
    async def handle(cls, event: HubSpotWebhookEvent) -> None:
        """Route a supported HubSpot deal event to the correct write path."""
        if event.subscription_type == DEAL_DELETION:
            await cls._handle_deletion(event)
            return

        if event.subscription_type in {DEAL_CREATION, DEAL_PROPERTY_CHANGE}:
            await cls._handle_upsert(event)
            return

        logger.info(
            "Ignoring unsupported HubSpot deal webhook event",
            extra={
                "event": "hubspot_deal_webhook_event_unsupported",
                "subscription_type": event.subscription_type,
                "portal_id": event.portal_id,
                "object_id": event.object_id,
                "event_id": event.event_id,
            },
        )

    @classmethod
    async def _handle_upsert(cls, event: HubSpotWebhookEvent) -> None:
        """Fetch the current deal snapshot and persist it through full-sync code."""
        service = HubSpotDealRecordSyncService()
        async for target in iter_hubspot_integrations_for_portal(event.portal_id):
            # The portal resolver returns org/integration ids only after matching
            # active HubSpot integrations, so every provider read and local write
            # stays inside that organisation's tenant schema.
            token_info = await service._load_token_info(
                target.organisation_id,
                target.integration_id,
            )
            property_list = await service._build_property_list(token_info=token_info)
            raw_deal = await HubSpotSingleDealFetcher(
                token_info=token_info,
                deal_id=event.object_id,
                properties=property_list,
            ).fetch()
            if raw_deal is None:
                # HubSpot can deliver an upsert event after the object was
                # archived/deleted. Treat a 404 as a local tombstone rather than
                # failing the webhook and forcing repeated provider retries.
                await cls._soft_delete_deal(
                    organisation_id=target.organisation_id,
                    integration_id=target.integration_id,
                    external_id=event.object_id,
                )
                continue

            await service._persist_raw_deals(
                organisation_id=target.organisation_id,
                integration_id=target.integration_id,
                raw_deals=[raw_deal],
                property_list_size=len(property_list),
            )

    @classmethod
    async def _handle_deletion(cls, event: HubSpotWebhookEvent) -> None:
        """Soft-delete the matching local deal for every portal-matched integration."""
        async for target in iter_hubspot_integrations_for_portal(event.portal_id):
            await cls._soft_delete_deal(
                organisation_id=target.organisation_id,
                integration_id=target.integration_id,
                external_id=event.object_id,
            )

    @staticmethod
    async def _soft_delete_deal(
        *,
        organisation_id: UUID,
        integration_id: UUID,
        external_id: str,
    ) -> bool:
        """Mark one org-scoped HubSpot deal as deleted without cascading children."""
        async for session in acquire_org_async_session(organisation_id):
            statement = select(CRMDeal).where(
                # The external object id is not trusted by itself; the org and
                # integration filters are the tenant boundary for this write.
                col(CRMDeal.organisation_id) == organisation_id,
                col(CRMDeal.integration_id) == integration_id,
                col(CRMDeal.external_id) == external_id,
                col(CRMDeal.is_deleted).is_(False),
            )
            deal = (await session.execute(statement)).scalars().first()
            if deal is None:
                return False

            # HubSpot deletion webhooks carry only the object id, so the local
            # mirror intentionally keeps historical payload fields for audit.
            deal.is_deleted = True
            deal.synced_at = datetime.now(UTC)
            session.add(deal)
            await session.commit()
            return True
        return False
