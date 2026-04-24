"""Idempotent upsert of CRMDealParticipant rows from HubSpot deal associations.

One participant row per (deal, participant_type, related_external_id, role).
HubSpot doesn't ship a participant identifier, so we synthesise one:

    {deal_external_id}:{participant_type}:{related_external_id}:{role|''}

Resolution at write time:

  * `deal_id`         ← `{deal_external_id → deal_id}` (deals upserted earlier
                         in the same session).
  * `related_local_id`← per-type map: contacts → contact_map, companies →
                         company_map, users → user_map.

Misses leave `related_local_id` NULL with the related_external_id intact.
We do not deactivate participants that disappear from a HubSpot snapshot —
historical deal context is intentional. A future reaper can use
`last_seen_at` if a deactivation policy is needed.
"""

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
from src.infra.utils.logger import get_logger
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
        existing_by_external = await self._load_existing_rows()

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
                self.session.add(
                    self._new_row(
                        participant=participant,
                        external_id=external_id,
                        deal_id=deal_id,
                        related_local_id=related_local_id,
                        related_name=related_name,
                        related_email=related_email,
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

    async def _load_existing_rows(self) -> dict[str, CRMDealParticipant]:
        stmt = select(CRMDealParticipant).where(
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.integration_id) == self.integration_id,
            col(CRMDealParticipant.is_deleted) == False,  # noqa: E712
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return {r.external_id: r for r in rows if r.external_id is not None}

    def _new_row(
        self,
        *,
        participant: NormalizedDealParticipant,
        external_id: str,
        deal_id: UUID,
        related_local_id: UUID | None,
        related_name: str | None,
        related_email: str | None,
        now: datetime,
    ) -> CRMDealParticipant:
        return CRMDealParticipant(
            organisation_id=self.organisation_id,
            integration_id=self.integration_id,
            external_id=external_id,
            deal_id=deal_id,
            participant_type=CRMParticipantType(participant.participant_type),
            relationship_role=participant.relationship_role,
            related_external_id=participant.related_external_id,
            related_local_id=related_local_id,
            related_name=related_name,
            related_email=related_email,
            synced_at=now,
            last_seen_at=now,
            data_payload=participant.data_payload,
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
        row.related_local_id = related_local_id
        row.related_name = related_name
        row.related_email = related_email
        row.synced_at = now
        row.last_seen_at = now
        row.data_payload = participant.data_payload


def _synthetic_external_id(participant: NormalizedDealParticipant) -> str:
    """Stable id across re-syncs for the same (deal, type, target, role) tuple."""
    role = participant.relationship_role or ""
    return (
        f"{participant.deal_external_id}:{participant.participant_type}:"
        f"{participant.related_external_id}:{role}"
    )
