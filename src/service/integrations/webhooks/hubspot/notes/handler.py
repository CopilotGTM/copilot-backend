"""HubSpot note webhook handlers."""

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
from src.service.integrations.crm.records.hubspot.notes.note_sync_service import (
    HubSpotNoteRecordSyncService,
)
from src.service.integrations.webhooks.hubspot.notes.subscriptions import (
    NOTE_ASSOCIATION_CHANGE,
    NOTE_CREATION,
    NOTE_DELETION,
    NOTE_PROPERTY_CHANGE,
)
from src.service.integrations.webhooks.hubspot.portal_resolver import (
    iter_hubspot_integrations_for_portal,
)
from src.service.integrations.webhooks.hubspot.types import HubSpotWebhookEvent
from src.workers.integrations_worker.providers.crm.hubspot.single_note_fetcher import (
    HubSpotSingleNoteFetcher,
)

logger = get_logger(__name__)


class HubSpotNoteWebhookHandler:
    """Handle generic HubSpot note webhooks through the note sync pipeline."""

    @classmethod
    async def handle(cls, event: HubSpotWebhookEvent) -> None:
        """Route a normalized note event to upsert or deletion behavior."""
        if event.subscription_type == NOTE_DELETION:
            await cls._handle_deletion(event)
            return

        if event.subscription_type in {
            NOTE_CREATION,
            NOTE_PROPERTY_CHANGE,
            NOTE_ASSOCIATION_CHANGE,
        }:
            await cls._handle_upsert(event)
            return

        logger.info(
            "Ignoring unsupported HubSpot note webhook event",
            extra={
                "event": "hubspot_note_webhook_event_unsupported",
                "subscription_type": event.subscription_type,
                "portal_id": event.portal_id,
                "object_id": event.object_id,
                "event_id": event.event_id,
            },
        )

    @classmethod
    async def _handle_upsert(cls, event: HubSpotWebhookEvent) -> None:
        """Fetch the current note snapshot and persist only that note."""
        service = HubSpotNoteRecordSyncService()
        async for target in iter_hubspot_integrations_for_portal(event.portal_id):
            context = await service._load_context(
                target.organisation_id,
                target.integration_id,
            )
            raw_note = await HubSpotSingleNoteFetcher(
                token_info=context.token_info,
                note_id=event.object_id,
            ).fetch()
            if raw_note is None:
                await cls._soft_delete_note(
                    organisation_id=target.organisation_id,
                    integration_id=target.integration_id,
                    external_id=event.object_id,
                )
                continue

            await service.persist_raw_note(
                organisation_id=target.organisation_id,
                integration_id=target.integration_id,
                raw_note=raw_note,
            )

    @classmethod
    async def _handle_deletion(cls, event: HubSpotWebhookEvent) -> None:
        """Soft-delete the matching local note for every portal-matched integration."""
        async for target in iter_hubspot_integrations_for_portal(event.portal_id):
            await cls._soft_delete_note(
                organisation_id=target.organisation_id,
                integration_id=target.integration_id,
                external_id=event.object_id,
            )

    @staticmethod
    async def _soft_delete_note(
        *,
        organisation_id: UUID,
        integration_id: UUID,
        external_id: str,
    ) -> bool:
        """Idempotently mark one org-scoped HubSpot note as deleted."""
        async for session in acquire_org_async_session(organisation_id):
            local_ids = await CRMSourceMappingHelper(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
                provider="hubspot",
            ).mark_external_ids_deleted(
                object_type=CRMSourceObjectType.NOTE,
                external_ids=[external_id],
            )
            activity_id = local_ids.get(external_id.strip())
            if activity_id is None:
                return False

            statement = select(CRMActivity).where(
                # Notes share external ids with other activity families, so the
                # activity type is part of the tenant-safe tombstone boundary.
                col(CRMActivity.organisation_id) == organisation_id,
                col(CRMActivity.integration_id) == integration_id,
                col(CRMActivity.activity_id) == activity_id,
                col(CRMActivity.activity_type) == CRMActivityType.NOTE.value,
                col(CRMActivity.is_deleted).is_(False),
            )
            note = (await session.execute(statement)).scalars().first()
            if note is None:
                return False

            note.is_deleted = True
            note.synced_at = datetime.now(UTC)
            session.add(note)
            await session.commit()
            return True
        return False
