"""Idempotent upsert of CRMDealParticipant rows from HubSpot deal associations.

One participant row per mapped provider association, with local related IDs when resolved.
HubSpot doesn't ship a participant identifier, so we synthesise one:

    {deal_external_id}:{participant_type}:{related_external_id}:{role|''}

The role remains in the synthetic id for backwards compatibility, but writes
also match by the related entity identity. That lets a later sync merge older
`*_unlabeled` rows into the preferred labeled row instead of creating a second
participant for the same contact/company.

Resolution at write time:

  * `deal_id`         ← `{deal_external_id → deal_id}` (deals upserted earlier
                         in the same session).
  * `related_local_id`← per-type map: contacts → contact_map, companies →
                         company_map, users → user_map.

Misses leave `related_local_id` NULL; provider IDs remain in source mappings.
We do not deactivate participants that disappear from a HubSpot snapshot —
historical deal context is intentional. A future reaper can use
`last_seen_at` if a deactivation policy is needed.
"""

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
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.common.source_mapping import CRMSourceMappingHelper
from src.service.integrations.crm.records.hubspot.deals.deal_types import (
    DimensionHit,
    NormalizedDealParticipant,
    ParticipantWriteResult,
)

logger = get_logger(__name__)


class HubSpotDealParticipantWriter:
    """Writer for `crm_deal_participants` rows."""

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
            provider="hubspot",
        )

    async def apply(
        self,
        *,
        participants: list[NormalizedDealParticipant],
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
            deal_ids=set(deal_pk_map.values())
        )

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
                related_email = _normalize_email(related_hit.email)

            existing = existing_by_external.get(external_id)
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
                await self._upsert_source_mapping(
                    participant, external_id, row.participant_id, now=now
                )
                _remember_identity(existing_by_identity, row)
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
                await self._upsert_source_mapping(
                    participant, external_id, existing.participant_id, now=now
                )
                existing_by_external[external_id] = existing
                _remember_identity(existing_by_identity, existing)
                result.participants_updated += 1

        await self.session.flush()
        logger.info(
            "Participant writer apply complete",
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
        participant: NormalizedDealParticipant,
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
        *,
        deal_ids: set[UUID],
    ) -> tuple[
        dict[str, CRMDealParticipant],
        dict[tuple[UUID, str, str], CRMDealParticipant],
    ]:
        external_to_local = await self.source_mappings.load_local_id_map(
            object_type=CRMSourceObjectType.DEAL_PARTICIPANT,
            include_deleted=True,
        )
        rows_by_id: dict[UUID, CRMDealParticipant] = {}
        if external_to_local:
            stmt = select(CRMDealParticipant).where(
                col(CRMDealParticipant.organisation_id) == self.organisation_id,
                col(CRMDealParticipant.participant_id).in_(set(external_to_local.values())),
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

        local_to_external = {
            local_id: external_id for external_id, local_id in external_to_local.items()
        }
        by_external = {
            local_to_external[row.participant_id]: row
            for row in rows_by_id.values()
            if row.participant_id in local_to_external
        }
        by_identity: dict[tuple[UUID, str, str], CRMDealParticipant] = {}
        for row in rows_by_id.values():
            _remember_identity(by_identity, row)
        return by_external, by_identity

    async def _upsert_source_mapping(
        self,
        participant: NormalizedDealParticipant,
        external_id: str,
        local_participant_id: UUID,
        *,
        now: datetime,
    ) -> None:
        await self.source_mappings.upsert_integration_mapping(
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
        participant: NormalizedDealParticipant,
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
            is_primary_contact=_is_primary_contact(participant),
            related_local_id=related_local_id,
            related_name=related_name,
            related_email=related_email,
            participant_source=_participant_source(participant),
            synced_at=now,
            last_seen_at=now,
        )

    def _apply_updates(
        self,
        row: CRMDealParticipant,
        *,
        participant: NormalizedDealParticipant,
        related_local_id: UUID | None,
        related_name: str | None,
        related_email: str | None,
        now: datetime,
    ) -> None:
        row.relationship_role = participant.relationship_role
        if participant.participant_type == CRMParticipantType.CONTACT.value:
            row.is_primary_contact = _is_primary_contact(participant)
            row.participant_source = "CRM"
        if related_local_id is not None:
            row.related_local_id = related_local_id
        if related_name is not None:
            row.related_name = related_name
        if related_email is not None:
            row.related_email = related_email
        row.synced_at = now
        row.last_seen_at = now


def _synthetic_external_id(participant: NormalizedDealParticipant) -> str:
    """Stable id across re-syncs for the same (deal, type, target, role) tuple."""
    role = participant.relationship_role or ""
    return (
        f"{participant.deal_external_id}:{participant.participant_type}:"
        f"{participant.related_external_id}:{role}"
    )


def _is_primary_contact(participant: NormalizedDealParticipant) -> bool:
    if participant.participant_type != CRMParticipantType.CONTACT.value:
        return False
    return participant.is_primary


def _participant_source(participant: NormalizedDealParticipant) -> str | None:
    if participant.participant_type == CRMParticipantType.CONTACT.value:
        return "CRM"
    return None


def _role_rank(role: str | None) -> int:
    role_value = (role or "").lower()
    if role_value.endswith("_primary"):
        return 30
    if "unlabeled" in role_value:
        return 10
    if role_value:
        return 20
    return 0


def _participant_type_value(participant_type: CRMParticipantType | str) -> str:
    return (
        participant_type.value
        if isinstance(participant_type, CRMParticipantType)
        else str(participant_type)
    )


def _normalize_email(email: str | None) -> str | None:
    if not email:
        return None
    normalized = email.strip().lower()
    return normalized or None


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
