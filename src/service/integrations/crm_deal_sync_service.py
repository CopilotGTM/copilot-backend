"""
CRM Deal sync service.

This service handles the complete deal sync flow:
1. Fetching deals from CRM API
2. Syncing deals to database
3. Running enrichment
4. Triggering on-demand syncs for missing reference data
5. Dispatching stakeholder sync tasks for all synced deals

Usage:
    service = CRMDealSyncService(session, organisation_id)
    result = await service.sync_integration(
        integration=integration,
        nango=nango_client,
        skip_enrichment=False,
        force_full_sync=False,
    )
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import attributes
from sqlmodel import col
from src.domain.models.organisation_tables.sync import IntegrationType
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.integration_models import SyncStatus
from src.infra.config.nango_provider_mapping import to_nango_provider_key
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.enrichment import CRMEnrichmentOrchestrator
from src.service.integrations.crm.entities import DealSyncService
from src.workers.deal_worker.services.deal_change_detector import StakeholderChangeType
from src.workers.integrations_worker.providers.registry import get_fetcher
from src.workers.integrations_worker.tasks.crm.utils import (
    get_last_sync_timestamp,
    get_provider_from_integration_type,
    get_sync_data_type,
    update_last_sync_timestamp,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from src.domain.models.organisation_tables.sync import KnowledgeIntegration
    from src.service.integrations import NangoClient

logger = get_logger(__name__)


class CRMDealSyncService:
    """
    Service for syncing CRM deals from external providers.

    Main entry point: sync_integration()
    """

    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None:
        self.session = session
        self.organisation_id = organisation_id

    async def sync_integration(
        self,
        *,
        integration: KnowledgeIntegration,
        nango: NangoClient,
        skip_enrichment: bool = False,
        force_full_sync: bool = False,
    ) -> dict[str, Any]:
        """
        Sync deals for a CRM integration.

        Args:
            integration: KnowledgeIntegration instance
            nango: NangoClient for API calls
            skip_enrichment: Skip enrichment (used during initial sync chord)
            force_full_sync: Ignore last_sync_at and do full sync

        Returns:
            Dictionary with sync results
        """
        integration_id = integration.integration_id
        integration_type = integration.integration_type

        # Normalize integration_type to enum
        if isinstance(integration_type, str):
            integration_type = IntegrationType(integration_type)

        # Determine sync parameters
        provider = get_provider_from_integration_type(integration_type)
        provider_config_key = to_nango_provider_key(integration_type)
        data_type = get_sync_data_type("deals", provider)
        last_sync_at = None if force_full_sync else get_last_sync_timestamp(integration, "deals")
        is_initial = last_sync_at is None

        logger.info(
            "CRM deal sync: org=%s integration=%s provider=%s is_initial=%s",
            self.organisation_id,
            integration_id,
            provider,
            is_initial,
        )

        # 1. Fetch deals from CRM API
        fetcher = get_fetcher("crm", provider, data_type)
        if not fetcher:
            raise ValueError(f"No fetcher found for crm/{provider}/{data_type}")

        sync_start = datetime.now(UTC)
        records = await fetcher(
            nango=nango,
            connection_id=integration.connection_id,
            is_initial=is_initial,
            last_synced_at=last_sync_at,
            data_type=data_type,
            provider_config_key=provider_config_key,
        )

        logger.info("Fetched %d deals for integration %s", len(records), integration_id)

        # 2. Sync deals to database
        deal_service = DealSyncService(db=self.session, organisation_id=self.organisation_id)
        if is_initial:
            metrics = await deal_service.sync(
                integration=integration,
                records=records,
                provider=provider,
            )
        else:
            metrics = await deal_service.sync_incremental(
                integration=integration,
                records=records,
                provider=provider,
            )

        # 3. Update integration status
        integration.last_synced_at = sync_start
        integration.last_sync_status = SyncStatus.SUCCESS.value
        integration.last_sync_error = None
        update_last_sync_timestamp(integration, "deals", sync_start)
        attributes.flag_modified(integration, "connection_metadata")
        self.session.add(integration)
        await self.session.commit()

        # 4. Run enrichment (unless skipped)
        enrichment_metrics: dict[str, Any] = {}
        triggered_syncs: list[str] = []
        stakeholder_count = 0

        if not skip_enrichment:
            enrichment_metrics = await self._run_enrichment(integration)
            triggered_syncs = self._trigger_missing_data_syncs(integration, enrichment_metrics)

            # 5. Dispatch stakeholder sync for all synced deals
            # The stakeholder task will internally decide what changes occurred
            stakeholder_count = await self._dispatch_stakeholder_sync(
                integration_id=integration_id,
                integration_type=integration_type,
                sync_start=sync_start,
            )

            if stakeholder_count > 0:
                triggered_syncs.append("stakeholders")

            logger.info(
                "CRM deal sync complete: org=%s integration=%s synced=%d stakeholders=%d",
                self.organisation_id,
                integration_id,
                metrics.get("deals", 0),
                stakeholder_count,
            )

        return {
            "integration_id": str(integration_id),
            "integration_type": integration_type.value,
            "status": "success",
            "records_fetched": len(records),
            "records_synced": metrics.get("deals", len(records)),
            "is_initial": is_initial,
            "metrics": metrics,
            "enrichment_metrics": enrichment_metrics,
            "triggered_syncs": triggered_syncs,
            "stakeholder_syncs_dispatched": stakeholder_count,
        }

    # =========================================================================
    # PRIVATE METHODS
    # =========================================================================

    async def _run_enrichment(
        self,
        integration: KnowledgeIntegration,
    ) -> dict[str, Any]:
        """Run enrichment after deals sync."""
        orchestrator = CRMEnrichmentOrchestrator(
            db=self.session,
            organisation_id=self.organisation_id,
            integration=integration,
        )
        metrics = await orchestrator.enrich_after_deals_sync(detect_missing=True)
        await self.session.commit()

        logger.info(
            "Deal enrichment complete: org=%s integration=%s metrics=%s",
            self.organisation_id,
            integration.integration_id,
            metrics,
        )
        return metrics

    def _trigger_missing_data_syncs(
        self,
        integration: KnowledgeIntegration,
        enrichment_metrics: dict[str, Any],
    ) -> list[str]:
        """Trigger on-demand syncs for missing pipelines and users."""
        triggered: list[str] = []

        missing_pipelines = enrichment_metrics.get("missing_pipelines", [])
        missing_users = enrichment_metrics.get("missing_users", [])

        if missing_pipelines:
            logger.info(
                "Triggering pipeline sync: org=%s integration=%s missing=%d",
                self.organisation_id,
                integration.integration_id,
                len(missing_pipelines),
            )
            from src.workers.integrations_worker.tasks.crm.pipelines.integration_task import (
                sync_crm_pipelines_integration_task,
            )

            sync_crm_pipelines_integration_task.delay(  # type: ignore
                organisation_id=str(self.organisation_id),
                integration_id=str(integration.integration_id),
            )
            triggered.append("pipelines")

        if missing_users:
            logger.info(
                "Triggering users sync: org=%s integration=%s missing=%d",
                self.organisation_id,
                integration.integration_id,
                len(missing_users),
            )
            from src.workers.integrations_worker.tasks.crm.users.integration_task import (
                sync_crm_users_integration_task,
            )

            sync_crm_users_integration_task.delay(  # type: ignore
                organisation_id=str(self.organisation_id),
                integration_id=str(integration.integration_id),
            )
            triggered.append("users")

        return triggered

    async def _dispatch_stakeholder_sync(
        self,
        *,
        integration_id: UUID,
        integration_type: IntegrationType,
        sync_start: datetime,
    ) -> int:
        """
        Dispatch stakeholder sync for all deals synced in this run.

        The stakeholder task will internally determine what changes occurred
        (new deal, participant added/updated/removed) and act accordingly.
        """
        # Get all deals synced in this run
        stmt = select(CRMDeal.deal_id).where(  # type: ignore
            col(CRMDeal.organisation_id) == self.organisation_id,
            col(CRMDeal.integration_id) == integration_id,
            col(CRMDeal.synced_at) >= sync_start,
            not col(CRMDeal.is_deleted),
        )
        result = await self.session.execute(stmt)
        deal_ids = [row[0] for row in result.fetchall()]

        if not deal_ids:
            return 0

        logger.info(
            "Dispatching stakeholder sync for %d deals",
            len(deal_ids),
        )

        from src.workers.deal_worker.tasks.deal_mappers.stakeholder_tasks import (
            sync_deal_stakeholders_task,
        )

        for deal_id in deal_ids:
            sync_deal_stakeholders_task.delay(  # type: ignore
                organisation_id=str(self.organisation_id),
                deal_id=str(deal_id),
                change_type=StakeholderChangeType.NEW_DEAL.value,  # Task will determine actual type
                integration_id=str(integration_id),
                integration_type=integration_type.value,
                trigger_rematching=True,
            )

        return len(deal_ids)
