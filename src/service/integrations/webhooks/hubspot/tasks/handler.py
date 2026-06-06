"""HubSpot task webhook handlers."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivity,
    CRMActivityType,
    CRMSourceObjectType,
)
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.common.source_mapping import CRMSourceMappingHelper
from src.service.integrations.crm.records.hubspot.tasks.task_sync_service import (
    HubSpotTaskRecordSyncService,
)
from src.service.integrations.webhooks.hubspot.portal_resolver import (
    iter_hubspot_integrations_for_portal,
)
from src.service.integrations.webhooks.hubspot.tasks.subscriptions import (
    TASK_ASSOCIATION_CHANGE,
    TASK_CREATION,
    TASK_DELETION,
    TASK_PROPERTY_CHANGE,
)
from src.service.integrations.webhooks.hubspot.types import HubSpotWebhookEvent
from src.workers.integrations_worker.providers.crm.hubspot.single_task_fetcher import (
    HubSpotSingleTaskFetcher,
)

logger = get_logger(__name__)


class HubSpotTaskWebhookHandler:
    """Handle generic HubSpot task webhooks through the task sync pipeline."""

    @classmethod
    async def handle(cls, event: HubSpotWebhookEvent) -> None:
        """Route a normalized task event to upsert or deletion behavior."""
        if event.subscription_type == TASK_DELETION:
            await cls._handle_deletion(event)
            return

        if event.subscription_type in {
            TASK_CREATION,
            TASK_PROPERTY_CHANGE,
            TASK_ASSOCIATION_CHANGE,
        }:
            await cls._handle_upsert(event)
            return

        logger.info(
            "Ignoring unsupported HubSpot task webhook event",
            extra={
                "event": "hubspot_task_webhook_event_unsupported",
                "subscription_type": event.subscription_type,
                "portal_id": event.portal_id,
                "object_id": event.object_id,
                "event_id": event.event_id,
            },
        )

    @classmethod
    async def _handle_upsert(cls, event: HubSpotWebhookEvent) -> None:
        """Fetch the current task snapshot and persist only that task."""
        service = HubSpotTaskRecordSyncService()
        async for target in iter_hubspot_integrations_for_portal(event.portal_id):
            context = await service._load_context(
                target.organisation_id,
                target.integration_id,
            )
            raw_task = await HubSpotSingleTaskFetcher(
                token_info=context.token_info,
                task_id=event.object_id,
            ).fetch()
            if raw_task is None:
                await cls._soft_delete_task(
                    organisation_id=target.organisation_id,
                    integration_id=target.integration_id,
                    external_id=event.object_id,
                )
                continue

            await service.persist_raw_task(
                organisation_id=target.organisation_id,
                integration_id=target.integration_id,
                raw_task=raw_task,
            )

    @classmethod
    async def _handle_deletion(cls, event: HubSpotWebhookEvent) -> None:
        """Soft-delete the matching local task for every portal-matched integration."""
        async for target in iter_hubspot_integrations_for_portal(event.portal_id):
            await cls._soft_delete_task(
                organisation_id=target.organisation_id,
                integration_id=target.integration_id,
                external_id=event.object_id,
            )

    @staticmethod
    async def _soft_delete_task(
        *,
        organisation_id: UUID,
        integration_id: UUID,
        external_id: str,
    ) -> bool:
        """Idempotently mark one org-scoped HubSpot task/todo as deleted."""
        async for session in acquire_org_async_session(organisation_id):
            local_ids = await CRMSourceMappingHelper(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
                provider="hubspot",
            ).mark_external_ids_deleted(
                object_type=CRMSourceObjectType.TASK,
                external_ids=[external_id],
            )
            activity_id = local_ids.get(external_id.strip())
            if activity_id is None:
                return False

            statement = select(CRMActivity).where(
                # Tasks share crm_activities with notes and other activity
                # families, so type filtering prevents cross-family tombstones.
                col(CRMActivity.organisation_id) == organisation_id,
                col(CRMActivity.integration_id) == integration_id,
                col(CRMActivity.activity_id) == activity_id,
                col(CRMActivity.activity_type).in_(
                    [CRMActivityType.TASK.value, CRMActivityType.TODO.value]
                ),
                col(CRMActivity.is_deleted).is_(False),
            )
            task = (await session.execute(statement)).scalars().first()
            if task is None:
                return False

            task.is_deleted = True
            task.synced_at = datetime.now(UTC)
            session.add(task)
            await session.commit()
            return True
        return False
