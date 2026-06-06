"""HubSpot notes record sync orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.crm.source_mappings import (
    CRMSourceObjectType,
)
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.common.source_mapping import CRMSourceMappingHelper
from src.service.integrations.crm.records.hubspot.notes.note_normalizer import normalize_notes
from src.service.integrations.crm.records.hubspot.notes.note_types import (
    ActivityDimensionHit,
    NoteSyncResult,
    NoteWriteResult,
)
from src.service.integrations.crm.records.hubspot.notes.note_writer import HubSpotNoteWriter
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.hubspot.notes_fetcher import HubSpotNotesFetcher

logger = get_logger(__name__)


@dataclass(slots=True)
class HubSpotNoteSyncContext:
    """Provider token context loaded from an org-scoped HubSpot integration row."""

    token_info: IntegrationTokenInfo


class NoteSyncConfigurationError(ValueError):
    """Integration not usable for HubSpot notes record sync."""


class HubSpotNoteRecordSyncService:
    """Coordinate full and single-record HubSpot note writes.

    Full sync callers fetch every note and may deactivate missing rows. The
    incremental repair path calls `persist_raw_note()` with one full snapshot,
    which deliberately avoids deactivating unrelated activities.
    """

    PROVIDER = "hubspot"
    OBJECT_TYPE = "note"

    async def run(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> NoteSyncResult:
        """Fetch all HubSpot notes for an integration and persist an active snapshot."""
        started_at = datetime.now(UTC)

        # Load credentials in a short DB session, then release it before the
        # provider scan so a slow HubSpot page cannot hold a tenant connection.
        context = await self._load_context(organisation_id, integration_id)
        raw_notes = await HubSpotNotesFetcher(token_info=context.token_info).fetch_all()
        normalized, skipped_malformed = normalize_notes(raw_notes)
        all_external_ids = [note.external_id for note in normalized]

        write_result = await self._persist(
            organisation_id=organisation_id,
            integration_id=integration_id,
            normalized=normalized,
            all_external_ids=all_external_ids,
        )

        completed_at = datetime.now(UTC)
        result = NoteSyncResult(
            provider=self.PROVIDER,
            object_type=self.OBJECT_TYPE,
            organisation_id=organisation_id,
            integration_id=integration_id,
            started_at=started_at,
            completed_at=completed_at,
            fetched_notes=len(raw_notes),
            notes_created=write_result.notes_created,
            notes_updated=write_result.notes_updated,
            notes_reactivated=write_result.notes_reactivated,
            notes_deactivated=write_result.notes_deactivated,
            unresolved_owner_link=write_result.unresolved_owner_link,
            unresolved_deal_link=write_result.unresolved_deal_link,
            unresolved_contact_link=write_result.unresolved_contact_link,
            unresolved_company_link=write_result.unresolved_company_link,
            skipped_malformed=skipped_malformed,
        )
        logger.info(
            "HubSpot notes sync summary",
            extra={
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
                "fetched_notes": result.fetched_notes,
                "notes_created": result.notes_created,
                "notes_updated": result.notes_updated,
                "notes_reactivated": result.notes_reactivated,
                "notes_deactivated": result.notes_deactivated,
                "unresolved_owner_link": result.unresolved_owner_link,
                "unresolved_deal_link": result.unresolved_deal_link,
                "unresolved_contact_link": result.unresolved_contact_link,
                "unresolved_company_link": result.unresolved_company_link,
                "skipped_malformed": result.skipped_malformed,
            },
        )
        return result

    async def persist_raw_note(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        raw_note: dict,
    ):
        """Normalize and persist one HubSpot note snapshot from incremental repair."""
        normalized, skipped_malformed = normalize_notes([raw_note])
        if not normalized:
            logger.warning(
                "HubSpot note incremental payload was malformed",
                extra={
                    "organisation_id": str(organisation_id),
                    "integration_id": str(integration_id),
                    "skipped_malformed": skipped_malformed,
                },
            )
            return NoteWriteResult()

        return await self._persist_single(
            organisation_id=organisation_id,
            integration_id=integration_id,
            note=normalized[0],
        )

    async def _load_context(
        self,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> HubSpotNoteSyncContext:
        async for session in acquire_org_async_session(organisation_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.organisation_id) == organisation_id,
            )
            # The organisation predicate is the tenant trust boundary; queued
            # worker payloads must not resolve integrations by external id alone.
            integration = (await session.execute(stmt)).scalar_one_or_none()
            if integration is None:
                raise NoteSyncConfigurationError(
                    f"Integration {integration_id} not found for organisation {organisation_id}"
                )
            int_type = integration.integration_type
            if isinstance(int_type, str):
                int_type = IntegrationType(int_type)
            if int_type != IntegrationType.HUBSPOT:
                raise NoteSyncConfigurationError(
                    f"Integration {integration_id} is {int_type.value}, expected hubspot"
                )
            if not integration.is_active:
                raise NoteSyncConfigurationError(f"Integration {integration_id} is not active")
            return HubSpotNoteSyncContext(
                token_info=IntegrationTokenInfo.from_integration(integration, organisation_id)
            )
        raise NoteSyncConfigurationError("Could not acquire org session for HubSpot note sync")

    async def _persist(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        normalized,
        all_external_ids,
    ):
        async for session in acquire_org_async_session(organisation_id):
            writer = HubSpotNoteWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )
            # Dimension maps are loaded inside the same org session as the
            # writer so note associations cannot link across tenant schemas.
            write_result = await writer.apply(
                normalized=normalized,
                all_external_ids=all_external_ids,
                user_map=await self._load_user_map(session, organisation_id, integration_id),
                deal_map=await self._load_deal_map(session, organisation_id, integration_id),
                contact_map=await self._load_contact_map(session, organisation_id, integration_id),
                company_map=await self._load_company_map(session, organisation_id, integration_id),
            )
            await session.commit()
            return write_result

        raise NoteSyncConfigurationError(
            "Could not acquire org session for HubSpot note sync write"
        )

    async def _persist_single(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        note,
    ):
        async for session in acquire_org_async_session(organisation_id):
            writer = HubSpotNoteWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )
            # Single-record writes come from webhooks/incremental polling. They
            # reuse the same association maps as full sync but do not mark other
            # notes deleted just because they were absent from this one payload.
            write_result = await writer.apply_single(
                note=note,
                user_map=await self._load_user_map(session, organisation_id, integration_id),
                deal_map=await self._load_deal_map(session, organisation_id, integration_id),
                contact_map=await self._load_contact_map(session, organisation_id, integration_id),
                company_map=await self._load_company_map(session, organisation_id, integration_id),
            )
            await session.commit()
            return write_result

        raise NoteSyncConfigurationError(
            "Could not acquire org session for HubSpot note sync write"
        )

    async def _load_user_map(
        self,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, ActivityDimensionHit]:
        external_to_local = await self._load_external_to_local(
            session, organisation_id, integration_id, CRMSourceObjectType.USER
        )
        if not external_to_local:
            return {}
        stmt = select(CRMUser).where(
            col(CRMUser.organisation_id) == organisation_id,
            col(CRMUser.integration_id) == integration_id,
            col(CRMUser.is_deleted) == False,  # noqa: E712
            col(CRMUser.user_id).in_(set(external_to_local.values())),
        )
        rows = (await session.execute(stmt)).scalars().all()
        local_to_external = _local_to_external(external_to_local)
        return {
            local_to_external[row.user_id]: ActivityDimensionHit(
                internal_id=row.user_id,
                name=row.full_name,
                email=row.email,
            )
            for row in rows
            if row.user_id in local_to_external
        }

    async def _load_deal_map(
        self,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, ActivityDimensionHit]:
        external_to_local = await self._load_external_to_local(
            session, organisation_id, integration_id, CRMSourceObjectType.DEAL
        )
        if not external_to_local:
            return {}
        stmt = select(CRMDeal).where(
            col(CRMDeal.organisation_id) == organisation_id,
            col(CRMDeal.integration_id) == integration_id,
            col(CRMDeal.is_deleted) == False,  # noqa: E712
            col(CRMDeal.deal_id).in_(set(external_to_local.values())),
        )
        rows = (await session.execute(stmt)).scalars().all()
        local_to_external = _local_to_external(external_to_local)
        return {
            local_to_external[row.deal_id]: ActivityDimensionHit(
                internal_id=row.deal_id, name=row.name
            )
            for row in rows
            if row.deal_id in local_to_external
        }

    async def _load_contact_map(
        self,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, ActivityDimensionHit]:
        external_to_local = await self._load_external_to_local(
            session, organisation_id, integration_id, CRMSourceObjectType.CONTACT
        )
        if not external_to_local:
            return {}
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == organisation_id,
            col(CRMContact.integration_id) == integration_id,
            col(CRMContact.is_deleted) == False,  # noqa: E712
            col(CRMContact.contact_id).in_(set(external_to_local.values())),
        )
        rows = (await session.execute(stmt)).scalars().all()
        local_to_external = _local_to_external(external_to_local)
        return {
            local_to_external[row.contact_id]: ActivityDimensionHit(
                internal_id=row.contact_id,
                name=row.full_name,
                email=row.email,
            )
            for row in rows
            if row.contact_id in local_to_external
        }

    async def _load_company_map(
        self,
        session,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, ActivityDimensionHit]:
        external_to_local = await self._load_external_to_local(
            session, organisation_id, integration_id, CRMSourceObjectType.COMPANY
        )
        if not external_to_local:
            return {}
        stmt = select(CRMCompany).where(
            col(CRMCompany.organisation_id) == organisation_id,
            col(CRMCompany.integration_id) == integration_id,
            col(CRMCompany.is_deleted) == False,  # noqa: E712
            col(CRMCompany.company_id).in_(set(external_to_local.values())),
        )
        rows = (await session.execute(stmt)).scalars().all()
        local_to_external = _local_to_external(external_to_local)
        return {
            local_to_external[row.company_id]: ActivityDimensionHit(
                internal_id=row.company_id, name=row.name
            )
            for row in rows
            if row.company_id in local_to_external
        }

    async def _load_external_to_local(
        self,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        object_type: CRMSourceObjectType,
    ) -> dict[str, UUID]:
        helper = CRMSourceMappingHelper(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            provider=self.PROVIDER,
        )
        return await helper.load_local_id_map(object_type=object_type)


def _local_to_external(external_to_local: dict[str, UUID]) -> dict[UUID, str]:
    return {local_id: external_id for external_id, local_id in external_to_local.items()}
