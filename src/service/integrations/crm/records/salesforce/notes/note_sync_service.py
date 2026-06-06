"""Salesforce notes record sync orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.domain.models.organisation_tables.sync.crm import CRMSourceObjectType
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.common.source_mapping import CRMSourceMappingHelper
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
        snapshot_started_at = started_at
        run_started = perf_counter()

        context = await self._load_context(organisation_id, integration_id)
        linked_entity_ids = await self._load_linked_entity_ids(organisation_id, integration_id)
        fetcher = SalesforceNotesFetcher(
            token_info=context.token_info,
            instance_url=context.instance_url,
        )
        fetched_notes = 0
        skipped_malformed = 0
        write_result = NoteWriteResult()
        buffer: list = []
        batch_count = 0
        persist_batch_size = 1000

        async for raw_batch in fetcher.iter_batches(linked_entity_ids=linked_entity_ids):
            batch_count += 1
            batch_started = perf_counter()
            fetched_notes += len(raw_batch)
            normalize_started = perf_counter()
            normalized, batch_skipped = normalize_notes(raw_batch)
            normalize_elapsed = perf_counter() - normalize_started
            skipped_malformed += batch_skipped
            scoped = [note for note in normalized if self._is_in_scope(note)]
            if not scoped:
                logger.info(
                    "Salesforce notes sync batch skipped",
                    extra={
                        "organisation_id": str(organisation_id),
                        "integration_id": str(integration_id),
                        "batch_number": batch_count,
                        "raw_batch_size": len(raw_batch),
                        "scoped_batch_size": 0,
                        "normalize_elapsed_seconds": round(normalize_elapsed, 3),
                        "elapsed_seconds": round(perf_counter() - run_started, 3),
                    },
                )
                continue
            buffer.extend(scoped)
            logger.info(
                "Salesforce notes sync batch buffered",
                extra={
                    "organisation_id": str(organisation_id),
                    "integration_id": str(integration_id),
                    "batch_number": batch_count,
                    "raw_batch_size": len(raw_batch),
                    "scoped_batch_size": len(scoped),
                    "buffer_size": len(buffer),
                    "normalize_elapsed_seconds": round(normalize_elapsed, 3),
                    "batch_elapsed_seconds": round(perf_counter() - batch_started, 3),
                    "fetched_notes": fetched_notes,
                    "elapsed_seconds": round(perf_counter() - run_started, 3),
                },
            )
            if len(buffer) < persist_batch_size:
                continue
            persist_started = perf_counter()
            batch_result = await self._persist(
                organisation_id=organisation_id,
                integration_id=integration_id,
                normalized=buffer,
                snapshot_started_at=snapshot_started_at,
                deactivate_missing=False,
            )
            logger.info(
                "Salesforce notes buffered persist complete",
                extra={
                    "organisation_id": str(organisation_id),
                    "integration_id": str(integration_id),
                    "batch_size": len(buffer),
                    "writer_elapsed_seconds": round(perf_counter() - persist_started, 3),
                    "elapsed_seconds": round(perf_counter() - run_started, 3),
                },
            )
            _add_write_result(write_result, batch_result)
            buffer = []

        if buffer:
            persist_started = perf_counter()
            batch_result = await self._persist(
                organisation_id=organisation_id,
                integration_id=integration_id,
                normalized=buffer,
                snapshot_started_at=snapshot_started_at,
                deactivate_missing=False,
            )
            logger.info(
                "Salesforce notes final buffered persist complete",
                extra={
                    "organisation_id": str(organisation_id),
                    "integration_id": str(integration_id),
                    "batch_size": len(buffer),
                    "writer_elapsed_seconds": round(perf_counter() - persist_started, 3),
                    "elapsed_seconds": round(perf_counter() - run_started, 3),
                },
            )
            _add_write_result(write_result, batch_result)

        final_result = await self._persist(
            organisation_id=organisation_id,
            integration_id=integration_id,
            normalized=[],
            snapshot_started_at=snapshot_started_at,
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
            snapshot_started_at=datetime.now(UTC),
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
            deal_ids = await _load_live_external_ids(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
                provider=self.PROVIDER,
                object_type=CRMSourceObjectType.DEAL,
                model=CRMDeal,
                id_column=CRMDeal.deal_id,
            )
            contact_ids = await _load_live_external_ids(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
                provider=self.PROVIDER,
                object_type=CRMSourceObjectType.CONTACT,
                model=CRMContact,
                id_column=CRMContact.contact_id,
            )
            company_ids = await _load_live_external_ids(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
                provider=self.PROVIDER,
                object_type=CRMSourceObjectType.COMPANY,
                model=CRMCompany,
                id_column=CRMCompany.company_id,
            )
            return deal_ids + contact_ids + company_ids
        raise SalesforceNoteSyncConfigurationError(
            "Could not acquire org session for Salesforce note sync scope"
        )

    async def _persist(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        normalized,
        snapshot_started_at: datetime | None,
        deactivate_missing: bool = True,
    ):
        persist_started = perf_counter()
        async for session in acquire_org_async_session(organisation_id):
            user_external_ids = _compact_str_set(note.owner_external_id for note in normalized)
            deal_external_ids = _compact_str_set(note.deal_external_id for note in normalized)
            contact_external_ids = _compact_str_set(note.contact_external_id for note in normalized)
            company_external_ids = _compact_str_set(note.company_external_id for note in normalized)
            map_started = perf_counter()
            user_map = await self._load_user_map(
                session, organisation_id, integration_id, user_external_ids
            )
            deal_map = await self._load_deal_map(
                session, organisation_id, integration_id, deal_external_ids
            )
            contact_map = await self._load_contact_map(
                session, organisation_id, integration_id, contact_external_ids
            )
            company_map = await self._load_company_map(
                session, organisation_id, integration_id, company_external_ids
            )
            map_elapsed = perf_counter() - map_started
            writer = SalesforceNoteWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )
            write_result = await writer.apply(
                normalized=normalized,
                snapshot_started_at=snapshot_started_at,
                user_map=user_map,
                deal_map=deal_map,
                contact_map=contact_map,
                company_map=company_map,
                deactivate_missing=deactivate_missing,
            )
            await session.commit()
            logger.info(
                "Salesforce notes persist complete",
                extra={
                    "organisation_id": str(organisation_id),
                    "integration_id": str(integration_id),
                    "batch_size": len(normalized),
                    "deactivate_missing": deactivate_missing,
                    "dimension_map_elapsed_seconds": round(map_elapsed, 3),
                    "persist_elapsed_seconds": round(perf_counter() - persist_started, 3),
                },
            )
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
        external_to_local = await _load_mapping_ids(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            provider=self.PROVIDER,
            object_type=CRMSourceObjectType.USER,
            external_ids=external_ids,
        )
        if not external_to_local:
            return {}
        stmt = select(CRMUser.user_id, CRMUser.full_name, CRMUser.email).where(
            col(CRMUser.organisation_id) == organisation_id,
            col(CRMUser.integration_id) == integration_id,
            col(CRMUser.is_deleted) == False,  # noqa: E712
            col(CRMUser.user_id).in_(set(external_to_local.values())),
        )
        rows = (await session.execute(stmt)).all()
        local_to_external = _local_to_external(external_to_local)
        return {
            local_to_external[user_id]: ActivityDimensionHit(
                internal_id=user_id,
                name=full_name,
                email=email,
            )
            for user_id, full_name, email in rows
            if user_id in local_to_external
        }

    async def _load_deal_map(
        self,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        external_ids: set[str],
    ) -> dict[str, ActivityDimensionHit]:
        external_to_local = await _load_mapping_ids(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            provider=self.PROVIDER,
            object_type=CRMSourceObjectType.DEAL,
            external_ids=external_ids,
        )
        if not external_to_local:
            return {}
        stmt = select(CRMDeal.deal_id, CRMDeal.name).where(
            col(CRMDeal.organisation_id) == organisation_id,
            col(CRMDeal.integration_id) == integration_id,
            col(CRMDeal.is_deleted) == False,  # noqa: E712
            col(CRMDeal.deal_id).in_(set(external_to_local.values())),
        )
        rows = (await session.execute(stmt)).all()
        local_to_external = _local_to_external(external_to_local)
        return {
            local_to_external[deal_id]: ActivityDimensionHit(internal_id=deal_id, name=name)
            for deal_id, name in rows
            if deal_id in local_to_external
        }

    async def _load_contact_map(
        self,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        external_ids: set[str],
    ) -> dict[str, ActivityDimensionHit]:
        external_to_local = await _load_mapping_ids(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            provider=self.PROVIDER,
            object_type=CRMSourceObjectType.CONTACT,
            external_ids=external_ids,
        )
        if not external_to_local:
            return {}
        stmt = select(
            CRMContact.contact_id,
            CRMContact.full_name,
            CRMContact.email,
        ).where(
            col(CRMContact.organisation_id) == organisation_id,
            col(CRMContact.integration_id) == integration_id,
            col(CRMContact.is_deleted) == False,  # noqa: E712
            col(CRMContact.contact_id).in_(set(external_to_local.values())),
        )
        rows = (await session.execute(stmt)).all()
        local_to_external = _local_to_external(external_to_local)
        return {
            local_to_external[contact_id]: ActivityDimensionHit(
                internal_id=contact_id,
                name=full_name,
                email=email,
            )
            for contact_id, full_name, email in rows
            if contact_id in local_to_external
        }

    async def _load_company_map(
        self,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        external_ids: set[str],
    ) -> dict[str, ActivityDimensionHit]:
        external_to_local = await _load_mapping_ids(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            provider=self.PROVIDER,
            object_type=CRMSourceObjectType.COMPANY,
            external_ids=external_ids,
        )
        if not external_to_local:
            return {}
        stmt = select(CRMCompany.company_id, CRMCompany.name).where(
            col(CRMCompany.organisation_id) == organisation_id,
            col(CRMCompany.integration_id) == integration_id,
            col(CRMCompany.is_deleted) == False,  # noqa: E712
            col(CRMCompany.company_id).in_(set(external_to_local.values())),
        )
        rows = (await session.execute(stmt)).all()
        local_to_external = _local_to_external(external_to_local)
        return {
            local_to_external[company_id]: ActivityDimensionHit(internal_id=company_id, name=name)
            for company_id, name in rows
            if company_id in local_to_external
        }


async def _load_live_external_ids(
    *,
    session,
    organisation_id: UUID,
    integration_id: UUID,
    provider: str,
    object_type: CRMSourceObjectType,
    model,
    id_column,
) -> list[str]:
    external_to_local = await _load_mapping_ids(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
        provider=provider,
        object_type=object_type,
        external_ids=None,
    )
    if not external_to_local:
        return []
    stmt = select(id_column).where(
        col(model.organisation_id) == organisation_id,
        col(model.integration_id) == integration_id,
        col(model.is_deleted) == False,  # noqa: E712
        col(id_column).in_(set(external_to_local.values())),
    )
    rows = (await session.execute(stmt)).all()
    live_ids = {local_id for (local_id,) in rows}
    return [
        external_id for external_id, local_id in external_to_local.items() if local_id in live_ids
    ]


async def _load_mapping_ids(
    *,
    session,
    organisation_id: UUID,
    integration_id: UUID,
    provider: str,
    object_type: CRMSourceObjectType,
    external_ids: set[str] | None,
) -> dict[str, UUID]:
    helper = CRMSourceMappingHelper(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
        provider=provider,
    )
    return await helper.load_local_id_map(object_type=object_type, external_ids=external_ids)


def _local_to_external(external_to_local: dict[str, UUID]) -> dict[UUID, str]:
    return {local_id: external_id for external_id, local_id in external_to_local.items()}


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
