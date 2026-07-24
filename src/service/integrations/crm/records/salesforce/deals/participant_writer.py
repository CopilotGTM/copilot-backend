"""Upsert Salesforce deal participants into crm_deal_participants."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.source_mappings import (
    CRMSourceObjectType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    DecisionPower,
    StakeholderStance,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.common.query_chunks import (
    LOCAL_ID_QUERY_CHUNK_SIZE,
    iter_chunks,
)
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

_DECISION_POWER_BY_CONTACT_ROLE = {
    "business user": DecisionPower.END_USER.value,
    "decision maker": DecisionPower.DECISION_MAKER.value,
    "economic decision maker": DecisionPower.DECISION_MAKER.value,
    "end user": DecisionPower.END_USER.value,
    "evaluator": DecisionPower.EVALUATOR.value,
    "executive sponsor": DecisionPower.DECISION_MAKER.value,
    "gatekeeper": DecisionPower.GATEKEEPER.value,
    "influencer": DecisionPower.INFLUENCER.value,
    "other": DecisionPower.UNKNOWN.value,
    "technical buyer": DecisionPower.EVALUATOR.value,
}
_STANCE_BY_CONTACT_ROLE = {
    "blocker": StakeholderStance.BLOCKER.value,
    "champion": StakeholderStance.CHAMPION.value,
    "detractor": StakeholderStance.DETRACTOR.value,
    "neutral": StakeholderStance.NEUTRAL.value,
    "supporter": StakeholderStance.SUPPORTER.value,
}


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
        existing_by_external, existing_by_identity = await self._load_existing_rows(
            external_ids={
                external_id
                for participant in participants
                for external_id in _source_external_ids(participant)
            },
            deal_ids=set(deal_pk_map.values()),
        )
        mapping_rows: list[IntegrationMappingUpsert] = []

        for participant in participants:
            deal_id = deal_pk_map.get(participant.deal_external_id)
            if deal_id is None:
                continue

            external_id = _synthetic_external_id(participant)
            legacy_external_id = _legacy_synthetic_external_id(participant)
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
                related_email = _normalize_email(related_hit.email)

            existing = existing_by_external.get(external_id) or existing_by_external.get(
                legacy_external_id
            )
            if existing is None:
                existing = _find_by_identity(
                    existing_by_identity=existing_by_identity,
                    deal_id=deal_id,
                    participant_type=participant.participant_type,
                    related_local_id=related_local_id,
                    related_email=related_email,
                )
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
                existing_by_external[legacy_external_id] = row
                _remember_identity(existing_by_identity, row)
                mapping_rows.extend(
                    self._source_mapping_rows(
                        participant=participant,
                        local_participant_id=row.participant_id,
                        now=now,
                    )
                )
                result.participants_created += 1
            else:
                self._apply_updates(
                    existing,
                    participant=participant,
                    related_local_id=related_local_id,
                    related_name=related_name,
                    related_email=related_email,
                    now=now,
                )
                existing.is_deleted = False
                existing_by_external[external_id] = existing
                existing_by_external[legacy_external_id] = existing
                _remember_identity(existing_by_identity, existing)
                mapping_rows.extend(
                    self._source_mapping_rows(
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
        deal_ids: set[UUID] | None = None,
    ) -> tuple[
        dict[str, CRMDealParticipant],
        dict[tuple[UUID, str, str], CRMDealParticipant],
    ]:
        external_to_local = await self.source_mappings.load_local_id_map(
            object_type=CRMSourceObjectType.DEAL_PARTICIPANT,
            external_ids=external_ids,
            include_deleted=True,
        )
        rows_by_id: dict[UUID, CRMDealParticipant] = {}
        if external_to_local:
            for local_ids in iter_chunks(
                set(external_to_local.values()), LOCAL_ID_QUERY_CHUNK_SIZE
            ):
                stmt = select(CRMDealParticipant).where(
                    col(CRMDealParticipant.organisation_id) == self.organisation_id,
                    col(CRMDealParticipant.participant_id).in_(local_ids),
                )
                rows = (await self.session.execute(stmt)).scalars().all()
                rows_by_id.update({row.participant_id: row for row in rows})

        if deal_ids:
            stmt = select(CRMDealParticipant).where(
                col(CRMDealParticipant.organisation_id) == self.organisation_id,
                col(CRMDealParticipant.deal_id).in_(deal_ids),
                col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
                col(CRMDealParticipant.is_deleted).is_(False),
                or_(
                    col(CRMDealParticipant.related_local_id).is_not(None),
                    col(CRMDealParticipant.related_email).is_not(None),
                ),
            )
            rows = (await self.session.execute(stmt)).scalars().all()
            rows_by_id.update({row.participant_id: row for row in rows})

        local_to_external = _local_to_external(external_to_local)
        by_external = {
            local_to_external[row.participant_id]: row
            for row in rows_by_id.values()
            if row.participant_id in local_to_external
        }
        by_identity: dict[tuple[UUID, str, str], CRMDealParticipant] = {}
        for row in rows_by_id.values():
            _remember_identity(by_identity, row)
        return by_external, by_identity

    def _source_mapping_rows(
        self,
        *,
        participant: NormalizedSalesforceDealParticipant,
        local_participant_id: UUID,
        now: datetime,
    ) -> list[IntegrationMappingUpsert]:
        rows: list[IntegrationMappingUpsert] = []
        for external_id in _source_external_ids(participant):
            rows.append(
                IntegrationMappingUpsert(
                    external_object_type=CRMSourceObjectType.DEAL_PARTICIPANT,
                    external_id=external_id,
                    local_object_type=CRMSourceObjectType.DEAL_PARTICIPANT,
                    local_object_id=local_participant_id,
                    data_payload=participant.data_payload,
                    source_created_at=None,
                    source_modified_at=None,
                    synced_at=now,
                    mapping_metadata={"is_primary_contact": _is_primary_contact(participant)},
                )
            )
        return rows

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
            relationship_role=_business_relationship_role(participant.relationship_role),
            is_primary_contact=_is_primary_contact(participant),
            related_local_id=related_local_id,
            related_name=related_name,
            related_email=related_email,
            participant_source=_participant_source(participant),
            decision_power=(
                participant.decision_power
                or _decision_power_for_role(participant.relationship_role)
                or DecisionPower.UNKNOWN.value
            ),
            stance=(
                participant.stance
                or _stance_for_role(participant.relationship_role)
                or StakeholderStance.UNKNOWN.value
            ),
            synced_at=now,
            last_seen_at=now,
        )

    def _apply_updates(
        self,
        row: CRMDealParticipant,
        *,
        participant: NormalizedSalesforceDealParticipant,
        related_local_id: UUID | None,
        related_name: str | None,
        related_email: str | None,
        now: datetime,
    ) -> None:
        provider_owned_role = row.participant_source == "CRM"
        if _participant_type_value(row.participant_type) == CRMParticipantType.CONTACT.value:
            row.is_primary_contact = _is_primary_contact(participant)
            incoming_role = _business_relationship_role(participant.relationship_role)
            if incoming_role is not None and (
                provider_owned_role
                or not row.relationship_role
                or _normalized_role(row.relationship_role) == "primary contact"
            ):
                row.relationship_role = incoming_role
            elif (
                incoming_role is None
                and row.relationship_role
                and ("primary contact" in _normalized_role(row.relationship_role))
            ):
                row.relationship_role = None
            mapped_decision_power = participant.decision_power or _decision_power_for_role(
                participant.relationship_role
            )
            if (
                mapped_decision_power is not None
                and row.decision_power == DecisionPower.UNKNOWN.value
            ):
                row.decision_power = mapped_decision_power
            if participant.stance is not None and row.stance == StakeholderStance.UNKNOWN.value:
                row.stance = participant.stance
            mapped_stance = _stance_for_role(participant.relationship_role)
            if mapped_stance is not None and row.stance == StakeholderStance.UNKNOWN.value:
                row.stance = mapped_stance
        if related_local_id is not None:
            row.related_local_id = related_local_id
        if related_name is not None:
            row.related_name = related_name
        if related_email is not None:
            row.related_email = related_email
        row.synced_at = now
        row.last_seen_at = now


def _synthetic_external_id(participant: NormalizedSalesforceDealParticipant) -> str:
    return f"{participant.deal_external_id}:{participant.participant_type}:{participant.related_external_id}"


def _legacy_synthetic_external_id(participant: NormalizedSalesforceDealParticipant) -> str:
    role = participant.relationship_role or ""
    return (
        f"{participant.deal_external_id}:{participant.participant_type}:"
        f"{participant.related_external_id}:{role}"
    )


def _source_external_ids(participant: NormalizedSalesforceDealParticipant) -> set[str]:
    return {_synthetic_external_id(participant), _legacy_synthetic_external_id(participant)}


def _is_primary_contact(participant: NormalizedSalesforceDealParticipant) -> bool:
    if participant.participant_type != CRMParticipantType.CONTACT.value:
        return False
    role = (participant.relationship_role or "").strip().lower().replace(" ", "_")
    return participant.is_primary or role == "primary_contact"


def _participant_source(participant: NormalizedSalesforceDealParticipant) -> str | None:
    if participant.participant_type in {
        CRMParticipantType.CONTACT.value,
        CRMParticipantType.COMPANY.value,
        CRMParticipantType.USER.value,
    }:
        return "CRM"
    return None


def _decision_power_for_role(role: str | None) -> str | None:
    if not role:
        return None
    normalized = " ".join(role.strip().lower().replace("-", " ").replace("_", " ").split())
    return _DECISION_POWER_BY_CONTACT_ROLE.get(normalized)


def _stance_for_role(role: str | None) -> str | None:
    if not role:
        return None
    return _STANCE_BY_CONTACT_ROLE.get(_normalized_role(role))


def _business_relationship_role(role: str | None) -> str | None:
    if not role or "primary contact" in _normalized_role(role):
        return None
    return role.strip() or None


def _normalized_role(role: str) -> str:
    return " ".join(role.strip().lower().replace("-", " ").replace("_", " ").split())


def _local_to_external(external_to_local: dict[str, UUID]) -> dict[UUID, str]:
    return {local_id: external_id for external_id, local_id in external_to_local.items()}


def _normalize_email(email: str | None) -> str | None:
    if not email:
        return None
    normalized = email.strip().lower()
    return normalized or None


def _participant_type_value(participant_type: CRMParticipantType | str) -> str:
    return (
        participant_type.value
        if isinstance(participant_type, CRMParticipantType)
        else str(participant_type)
    )


def _role_rank(role: str | None) -> int:
    role_value = (role or "").lower()
    if "primary" in role_value:
        return 30
    if role_value:
        return 20
    return 0


def _identity_keys(row: CRMDealParticipant) -> list[tuple[UUID, str, str]]:
    participant_type = _participant_type_value(row.participant_type)
    keys: list[tuple[UUID, str, str]] = []
    if row.related_local_id is not None:
        keys.append((row.deal_id, participant_type, f"local:{row.related_local_id}"))
    normalized_email = _normalize_email(row.related_email)
    if normalized_email:
        keys.append((row.deal_id, participant_type, f"email:{normalized_email}"))
    return keys


def _remember_identity(
    existing_by_identity: dict[tuple[UUID, str, str], CRMDealParticipant],
    row: CRMDealParticipant,
) -> None:
    if row.is_deleted:
        return
    for key in _identity_keys(row):
        existing = existing_by_identity.get(key)
        if existing is None or _role_rank(row.relationship_role) > _role_rank(
            existing.relationship_role
        ):
            existing_by_identity[key] = row


def _find_by_identity(
    *,
    existing_by_identity: dict[tuple[UUID, str, str], CRMDealParticipant],
    deal_id: UUID,
    participant_type: str,
    related_local_id: UUID | None,
    related_email: str | None,
) -> CRMDealParticipant | None:
    if related_local_id is not None:
        existing = existing_by_identity.get(
            (deal_id, participant_type, f"local:{related_local_id}")
        )
        if existing is not None:
            return existing
    normalized_email = _normalize_email(related_email)
    if normalized_email:
        return existing_by_identity.get((deal_id, participant_type, f"email:{normalized_email}"))
    return None
