"""Upsert Salesforce deal participants into crm_deal_participants."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.source_mappings import (
    CRMSourceObjectType,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.common.source_mapping import (
    CRMSourceMappingHelper,
    IntegrationMappingUpsert,
)
from src.service.integrations.crm.records.salesforce.deals.deal_types import (
    DimensionHit,
    NormalizedSalesforceDealParticipant,
    ParticipantWriteResult,
)

logger = get_logger(__name__)


class SalesforceDealParticipantWriter:
    def __init__(
        self,
        *,
        session: AsyncSession,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.integration_id = integration_id
        self.source_mappings = CRMSourceMappingHelper(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            provider="salesforce",
        )

    async def apply(
        self,
        *,
        participants: list[NormalizedSalesforceDealParticipant],
        deal_pk_map: dict[str, UUID],
        contact_map: dict[str, DimensionHit],
        company_map: dict[str, DimensionHit],
        user_map: dict[str, DimensionHit],
    ) -> ParticipantWriteResult:
        result = ParticipantWriteResult()
        if not participants:
            await self.session.flush()
            return result

        now = datetime.now(UTC)
        existing_by_external = await self._load_existing_rows(
            external_ids={_synthetic_external_id(participant) for participant in participants}
        )
        mapping_rows: list[IntegrationMappingUpsert] = []

        for participant in participants:
            deal_id = deal_pk_map.get(participant.deal_external_id)
            if deal_id is None:
                continue

            external_id = _synthetic_external_id(participant)
            related_hit = self._related_hit(
                participant=participant,
                contact_map=contact_map,
                company_map=company_map,
                user_map=user_map,
            )
            if related_hit is None:
                result.unresolved_related += 1
                related_local_id: UUID | None = None
                related_name: str | None = None
                related_email: str | None = None
            else:
                related_local_id = related_hit.internal_id
                related_name = related_hit.name
                related_email = related_hit.email

            existing = existing_by_external.get(external_id)
            if existing is None:
                row = self._new_row(
                    participant=participant,
                    deal_id=deal_id,
                    related_local_id=related_local_id,
                    related_name=related_name,
                    related_email=related_email,
                    now=now,
                )
                self.session.add(row)
                existing_by_external[external_id] = row
                mapping_rows.append(
                    self._source_mapping_row(
                        external_id=external_id,
                        participant=participant,
                        local_participant_id=row.participant_id,
                        now=now,
                    )
                )
                result.participants_created += 1
            else:
                self._apply_updates(
                    existing,
                    related_local_id=related_local_id,
                    related_name=related_name,
                    related_email=related_email,
                    now=now,
                )
                existing.is_deleted = False
                mapping_rows.append(
                    self._source_mapping_row(
                        external_id=external_id,
                        participant=participant,
                        local_participant_id=existing.participant_id,
                        now=now,
                    )
                )
                result.participants_updated += 1

        if mapping_rows:
            await self.source_mappings.bulk_upsert_integration_mappings(mapping_rows)

        await self.session.flush()
        logger.info(
            "Salesforce participant writer apply complete",
            extra={
                "organisation_id": str(self.organisation_id),
                "integration_id": str(self.integration_id),
                "participants_created": result.participants_created,
                "participants_updated": result.participants_updated,
                "unresolved_related": result.unresolved_related,
            },
        )
        return result

    def _related_hit(
        self,
        *,
        participant: NormalizedSalesforceDealParticipant,
        contact_map: dict[str, DimensionHit],
        company_map: dict[str, DimensionHit],
        user_map: dict[str, DimensionHit],
    ) -> DimensionHit | None:
        if participant.participant_type == CRMParticipantType.CONTACT.value:
            return contact_map.get(participant.related_external_id)
        if participant.participant_type == CRMParticipantType.COMPANY.value:
            return company_map.get(participant.related_external_id)
        if participant.participant_type == CRMParticipantType.USER.value:
            return user_map.get(participant.related_external_id)
        return None

    async def _load_existing_rows(
        self,
        external_ids: set[str] | None = None,
    ) -> dict[str, CRMDealParticipant]:
        external_to_local = await self.source_mappings.load_local_id_map(
            object_type=CRMSourceObjectType.DEAL_PARTICIPANT,
            external_ids=external_ids,
            include_deleted=True,
        )
        if not external_to_local:
            return {}
        stmt = select(CRMDealParticipant).where(
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.integration_id) == self.integration_id,
            col(CRMDealParticipant.participant_id).in_(set(external_to_local.values())),
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        local_to_external = _local_to_external(external_to_local)
        return {
            local_to_external[row.participant_id]: row
            for row in rows
            if row.participant_id in local_to_external
        }

    def _source_mapping_row(
        self,
        *,
        external_id: str,
        participant: NormalizedSalesforceDealParticipant,
        local_participant_id: UUID,
        now: datetime,
    ) -> IntegrationMappingUpsert:
        return IntegrationMappingUpsert(
            external_object_type=CRMSourceObjectType.DEAL_PARTICIPANT,
            external_id=external_id,
            local_object_type=CRMSourceObjectType.DEAL_PARTICIPANT,
            local_object_id=local_participant_id,
            data_payload=participant.data_payload,
            source_created_at=None,
            source_modified_at=None,
            synced_at=now,
        )

    def _new_row(
        self,
        *,
        participant: NormalizedSalesforceDealParticipant,
        deal_id: UUID,
        related_local_id: UUID | None,
        related_name: str | None,
        related_email: str | None,
        now: datetime,
    ) -> CRMDealParticipant:
        return CRMDealParticipant(
            organisation_id=self.organisation_id,
            integration_id=self.integration_id,
            deal_id=deal_id,
            participant_type=CRMParticipantType(participant.participant_type),
            relationship_role=participant.relationship_role,
            related_local_id=related_local_id,
            related_name=related_name,
            related_email=related_email,
            synced_at=now,
            last_seen_at=now,
        )

    def _apply_updates(
        self,
        row: CRMDealParticipant,
        *,
        related_local_id: UUID | None,
        related_name: str | None,
        related_email: str | None,
        now: datetime,
    ) -> None:
        row.related_local_id = related_local_id
        row.related_name = related_name
        row.related_email = related_email
        row.synced_at = now
        row.last_seen_at = now


def _synthetic_external_id(participant: NormalizedSalesforceDealParticipant) -> str:
    role = participant.relationship_role or ""
    return (
        f"{participant.deal_external_id}:{participant.participant_type}:"
        f"{participant.related_external_id}:{role}"
    )


def _local_to_external(external_to_local: dict[str, UUID]) -> dict[UUID, str]:
    return {local_id: external_id for external_id, local_id in external_to_local.items()}
