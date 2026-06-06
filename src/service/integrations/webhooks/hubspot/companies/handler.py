"""HubSpot company webhook handlers.

HubSpot calls this object family "company"; the product-facing CRM concept is
`CRMCompany` / account. Webhooks carry only object identity, so upserts fetch a
fresh company snapshot and reuse the company sync writer without invoking full
snapshot deactivation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm import CRMSourceObjectType
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.common.source_mapping import CRMSourceMappingHelper
from src.service.integrations.crm.records.hubspot.companies.company_sync_service import (
    HubSpotCompanyRecordSyncService,
)
from src.service.integrations.webhooks.hubspot.companies.subscriptions import (
    COMPANY_CREATION,
    COMPANY_DELETION,
    COMPANY_PROPERTY_CHANGE,
)
from src.service.integrations.webhooks.hubspot.portal_resolver import (
    iter_hubspot_integrations_for_portal,
)
from src.service.integrations.webhooks.hubspot.types import HubSpotWebhookEvent
from src.workers.integrations_worker.providers.crm.hubspot.single_company_fetcher import (
    HubSpotSingleCompanyFetcher,
)

logger = get_logger(__name__)


class HubSpotCompanyWebhookHandler:
    """Handle HubSpot company webhooks by updating one org-scoped company row."""

    @classmethod
    async def handle(cls, event: HubSpotWebhookEvent) -> None:
        """Route a supported HubSpot company event to the correct write path."""
        if event.subscription_type == COMPANY_DELETION:
            await cls._handle_deletion(event)
            return

        if event.subscription_type in {COMPANY_CREATION, COMPANY_PROPERTY_CHANGE}:
            await cls._handle_upsert(event)
            return

        logger.info(
            "Ignoring unsupported HubSpot company webhook event",
            extra={
                "event": "hubspot_company_webhook_event_unsupported",
                "subscription_type": event.subscription_type,
                "portal_id": event.portal_id,
                "object_id": event.object_id,
                "event_id": event.event_id,
            },
        )

    @classmethod
    async def _handle_upsert(cls, event: HubSpotWebhookEvent) -> None:
        """Fetch the current company snapshot and persist only that company."""
        service = HubSpotCompanyRecordSyncService()
        async for target in iter_hubspot_integrations_for_portal(event.portal_id):
            # The portal resolver has already mapped HubSpot's portal id to an
            # active org integration; every provider read and DB write stays
            # inside that tenant boundary.
            token_info = await service._load_token_info(
                target.organisation_id,
                target.integration_id,
            )
            raw_company = await HubSpotSingleCompanyFetcher(
                token_info=token_info,
                company_id=event.object_id,
            ).fetch()
            if raw_company is None:
                await cls._soft_delete_company(
                    organisation_id=target.organisation_id,
                    integration_id=target.integration_id,
                    external_id=event.object_id,
                )
                continue

            await service.persist_raw_company(
                organisation_id=target.organisation_id,
                integration_id=target.integration_id,
                raw_company=raw_company,
            )

    @classmethod
    async def _handle_deletion(cls, event: HubSpotWebhookEvent) -> None:
        """Soft-delete the matching local company for every portal-matched integration."""
        async for target in iter_hubspot_integrations_for_portal(event.portal_id):
            await cls._soft_delete_company(
                organisation_id=target.organisation_id,
                integration_id=target.integration_id,
                external_id=event.object_id,
            )

    @staticmethod
    async def _soft_delete_company(
        *,
        organisation_id: UUID,
        integration_id: UUID,
        external_id: str,
    ) -> bool:
        """Mark one org-scoped HubSpot company deleted without cascading deals."""
        async for session in acquire_org_async_session(organisation_id):
            local_ids = await CRMSourceMappingHelper(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
                provider="hubspot",
            ).mark_external_ids_deleted(
                object_type=CRMSourceObjectType.COMPANY,
                external_ids=[external_id],
            )
            company_id = local_ids.get(external_id.strip())
            if company_id is None:
                return False

            statement = select(CRMCompany).where(
                col(CRMCompany.organisation_id) == organisation_id,
                col(CRMCompany.integration_id) == integration_id,
                col(CRMCompany.company_id) == company_id,
                col(CRMCompany.is_deleted).is_(False),
            )
            company = (await session.execute(statement)).scalars().first()
            if company is None:
                return False

            # Keep historical display fields on linked deals/contacts. The
            # webhook only proves the company was deleted in HubSpot; it does
            # not imply local child records should be rewritten or cascaded.
            company.is_deleted = True
            company.is_active = False
            company.synced_at = datetime.now(UTC)
            session.add(company)
            await session.commit()
            return True
        return False
