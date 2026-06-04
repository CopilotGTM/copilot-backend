"""Salesforce notes record sync orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.salesforce.notes.note_normalizer import normalize_notes
from src.service.integrations.crm.records.salesforce.notes.note_types import (
    ActivityDimensionHit,
    NoteSyncResult,
    NoteWriteResult,
)
from src.service.integrations.crm.records.salesforce.notes.note_writer import SalesforceNoteWriter
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.workers.integrations_worker.providers.crm.salesforce.notes_fetcher import (
    SalesforceNotesFetcher,
)

logger = get_logger(__name__)


@dataclass(slots=True)
class SalesforceNoteSyncContext:
    token_info: IntegrationTokenInfo
    instance_url: str


class SalesforceNoteSyncConfigurationError(ValueError):
    """Integration not usable for Salesforce notes record sync."""


class SalesforceNoteRecordSyncService:
    PROVIDER = "salesforce"
    OBJECT_TYPE = "note"

    async def run(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> NoteSyncResult:
        started_at = datetime.now(UTC)

        context = await self._load_context(organisation_id, integration_id)
        linked_entity_ids = await self._load_linked_entity_ids(organisation_id, integration_id)
        fetcher = SalesforceNotesFetcher(
            token_info=context.token_info,
            instance_url=context.instance_url,
        )
        fetched_notes = 0
        skipped_malformed = 0
        all_external_ids: set[str] = set()
        write_result = NoteWriteResult()

        async for raw_batch in fetcher.iter_batches(linked_entity_ids=linked_entity_ids):
            fetched_notes += len(raw_batch)
            normalized, batch_skipped = normalize_notes(raw_batch)
            skipped_malformed += batch_skipped
            scoped = [note for note in normalized if self._is_in_scope(note)]
            all_external_ids.update(note.external_id for note in scoped)
            if not scoped:
                continue
            batch_result = await self._persist(
                organisation_id=organisation_id,
                integration_id=integration_id,
                normalized=scoped,
                all_external_ids=[note.external_id for note in scoped],
                deactivate_missing=False,
            )
            _add_write_result(write_result, batch_result)

        final_result = await self._persist(
            organisation_id=organisation_id,
            integration_id=integration_id,
            normalized=[],
            all_external_ids=all_external_ids,
            deactivate_missing=True,
        )
        _add_write_result(write_result, final_result)

        completed_at = datetime.now(UTC)
        result = NoteSyncResult(
            provider=self.PROVIDER,
            object_type=self.OBJECT_TYPE,
            organisation_id=organisation_id,
            integration_id=integration_id,
            started_at=started_at,
            completed_at=completed_at,
            fetched_notes=fetched_notes,
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
            "Salesforce notes sync summary",
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

    async def persist_single_note(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        raw_notes: list[dict[str, Any]],
    ):
        normalized, _skipped_malformed = normalize_notes(raw_notes)
        scoped = [note for note in normalized if self._is_in_scope(note)]
        return await self._persist(
            organisation_id=organisation_id,
            integration_id=integration_id,
            normalized=scoped,
            all_external_ids=[note.external_id for note in scoped],
            deactivate_missing=False,
        )

    def _is_in_scope(self, note) -> bool:
        return (
            note.deal_external_id is not None
            or note.contact_external_id is not None
            or note.company_external_id is not None
        )

    async def _load_context(
        self,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> SalesforceNoteSyncContext:
        async for session in acquire_org_async_session(organisation_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.organisation_id) == organisation_id,
            )
            integration = (await session.execute(stmt)).scalar_one_or_none()
            if integration is None:
                raise SalesforceNoteSyncConfigurationError(
                    f"Integration {integration_id} not found for organisation {organisation_id}"
                )
            int_type = integration.integration_type
            if isinstance(int_type, str):
                int_type = IntegrationType(int_type)
            if int_type != IntegrationType.SALESFORCE:
                raise SalesforceNoteSyncConfigurationError(
                    f"Integration {integration_id} is {int_type.value}, expected salesforce"
                )
            if not integration.is_active:
                raise SalesforceNoteSyncConfigurationError(
                    f"Integration {integration_id} is not active"
                )
            credentials = dict(integration.credentials or {})
            instance_url = str(credentials.get("instance_url") or "").rstrip("/")
            if not instance_url:
                raise SalesforceNoteSyncConfigurationError(
                    f"Integration {integration_id} is missing Salesforce instance_url"
                )
            return SalesforceNoteSyncContext(
                token_info=IntegrationTokenInfo.from_integration(integration, organisation_id),
                instance_url=instance_url,
            )
        raise SalesforceNoteSyncConfigurationError(
            "Could not acquire org session for Salesforce note sync"
        )

    async def _load_linked_entity_ids(
        self,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> list[str]:
        async for session in acquire_org_async_session(organisation_id):
            deal_stmt = select(CRMDeal.external_id).where(
                col(CRMDeal.organisation_id) == organisation_id,
                col(CRMDeal.integration_id) == integration_id,
                col(CRMDeal.is_deleted) == False,  # noqa: E712
            )
            contact_stmt = select(CRMContact.external_id).where(
                col(CRMContact.organisation_id) == organisation_id,
                col(CRMContact.integration_id) == integration_id,
                col(CRMContact.is_deleted) == False,  # noqa: E712
            )
            company_stmt = select(CRMCompany.external_id).where(
                col(CRMCompany.organisation_id) == organisation_id,
                col(CRMCompany.integration_id) == integration_id,
                col(CRMCompany.is_deleted) == False,  # noqa: E712
            )
            deal_rows = (await session.execute(deal_stmt)).all()
            contact_rows = (await session.execute(contact_stmt)).all()
            company_rows = (await session.execute(company_stmt)).all()
            linked_entity_ids: list[str] = []
            linked_entity_ids.extend(external_id for (external_id,) in deal_rows if external_id)
            linked_entity_ids.extend(external_id for (external_id,) in contact_rows if external_id)
            linked_entity_ids.extend(external_id for (external_id,) in company_rows if external_id)
            return linked_entity_ids
        raise SalesforceNoteSyncConfigurationError(
            "Could not acquire org session for Salesforce note sync scope"
        )

    async def _persist(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        normalized,
        all_external_ids,
        deactivate_missing: bool = True,
    ):
        async for session in acquire_org_async_session(organisation_id):
            user_external_ids = _compact_str_set(note.owner_external_id for note in normalized)
            deal_external_ids = _compact_str_set(note.deal_external_id for note in normalized)
            contact_external_ids = _compact_str_set(note.contact_external_id for note in normalized)
            company_external_ids = _compact_str_set(note.company_external_id for note in normalized)
            writer = SalesforceNoteWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )
            write_result = await writer.apply(
                normalized=normalized,
                all_external_ids=all_external_ids,
                user_map=await self._load_user_map(
                    session, organisation_id, integration_id, user_external_ids
                ),
                deal_map=await self._load_deal_map(
                    session, organisation_id, integration_id, deal_external_ids
                ),
                contact_map=await self._load_contact_map(
                    session, organisation_id, integration_id, contact_external_ids
                ),
                company_map=await self._load_company_map(
                    session, organisation_id, integration_id, company_external_ids
                ),
                deactivate_missing=deactivate_missing,
            )
            await session.commit()
            return write_result

        raise SalesforceNoteSyncConfigurationError(
            "Could not acquire org session for Salesforce note sync write"
        )

    async def _load_user_map(
        self,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        external_ids: set[str],
    ) -> dict[str, ActivityDimensionHit]:
        if not external_ids:
            return {}
        stmt = select(CRMUser.external_id, CRMUser.user_id, CRMUser.full_name, CRMUser.email).where(
            col(CRMUser.organisation_id) == organisation_id,
            col(CRMUser.integration_id) == integration_id,
            col(CRMUser.is_deleted) == False,  # noqa: E712
            col(CRMUser.external_id).in_(external_ids),
        )
        rows = (await session.execute(stmt)).all()
        return {
            external_id: ActivityDimensionHit(
                internal_id=user_id,
                name=full_name,
                email=email,
            )
            for external_id, user_id, full_name, email in rows
            if external_id is not None
        }

    async def _load_deal_map(
        self,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        external_ids: set[str],
    ) -> dict[str, ActivityDimensionHit]:
        if not external_ids:
            return {}
        stmt = select(CRMDeal.external_id, CRMDeal.deal_id, CRMDeal.name).where(
            col(CRMDeal.organisation_id) == organisation_id,
            col(CRMDeal.integration_id) == integration_id,
            col(CRMDeal.is_deleted) == False,  # noqa: E712
            col(CRMDeal.external_id).in_(external_ids),
        )
        rows = (await session.execute(stmt)).all()
        return {
            external_id: ActivityDimensionHit(internal_id=deal_id, name=name)
            for external_id, deal_id, name in rows
            if external_id is not None
        }

    async def _load_contact_map(
        self,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        external_ids: set[str],
    ) -> dict[str, ActivityDimensionHit]:
        if not external_ids:
            return {}
        stmt = select(
            CRMContact.external_id,
            CRMContact.contact_id,
            CRMContact.full_name,
            CRMContact.email,
        ).where(
            col(CRMContact.organisation_id) == organisation_id,
            col(CRMContact.integration_id) == integration_id,
            col(CRMContact.is_deleted) == False,  # noqa: E712
            col(CRMContact.external_id).in_(external_ids),
        )
        rows = (await session.execute(stmt)).all()
        return {
            external_id: ActivityDimensionHit(
                internal_id=contact_id,
                name=full_name,
                email=email,
            )
            for external_id, contact_id, full_name, email in rows
            if external_id is not None
        }

    async def _load_company_map(
        self,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        external_ids: set[str],
    ) -> dict[str, ActivityDimensionHit]:
        if not external_ids:
            return {}
        stmt = select(CRMCompany.external_id, CRMCompany.company_id, CRMCompany.name).where(
            col(CRMCompany.organisation_id) == organisation_id,
            col(CRMCompany.integration_id) == integration_id,
            col(CRMCompany.is_deleted) == False,  # noqa: E712
            col(CRMCompany.external_id).in_(external_ids),
        )
        rows = (await session.execute(stmt)).all()
        return {
            external_id: ActivityDimensionHit(internal_id=company_id, name=name)
            for external_id, company_id, name in rows
            if external_id is not None
        }


def _compact_str_set(values) -> set[str]:
    return {value for value in values if isinstance(value, str) and value}


def _add_write_result(target: NoteWriteResult, source: NoteWriteResult) -> None:
    target.notes_created += source.notes_created
    target.notes_updated += source.notes_updated
    target.notes_reactivated += source.notes_reactivated
    target.notes_deactivated += source.notes_deactivated
    target.unresolved_owner_link += source.unresolved_owner_link
    target.unresolved_deal_link += source.unresolved_deal_link
    target.unresolved_contact_link += source.unresolved_contact_link
    target.unresolved_company_link += source.unresolved_company_link
