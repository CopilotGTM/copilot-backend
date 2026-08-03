"""CRM lead participant service."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.leads import (
    CRMLeadParticipant,
    CRMLeadParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.service.crm.leads.contracts import LeadServiceError
from src.service.crm.leads.read_service import LeadReadService


class LeadParticipantService(LeadReadService):
    """Manage lead participants and participant copying."""

    async def add_participant(
        self,
        lead_id: UUID,
        payload: dict[str, Any],
    ) -> CRMLeadParticipant:
        """Add a contact, company, user, or custom participant to a lead."""
        await self._get_lead_or_404(lead_id, "control")
        if not (payload.get("related_local_id") or payload.get("email")):
            raise LeadServiceError(
                "Participant requires related_local_id or email",
                status_code=400,
            )

        related_local_id = payload.get("related_local_id")
        if related_local_id:
            try:
                participant_type = CRMLeadParticipantType(payload.get("participant_type"))
            except ValueError as exc:
                raise LeadServiceError("Invalid participant type", status_code=400) from exc

            related_model = {
                CRMLeadParticipantType.CONTACT: (CRMContact, CRMContact.contact_id),
                CRMLeadParticipantType.COMPANY: (CRMCompany, CRMCompany.company_id),
                CRMLeadParticipantType.USER: (CRMUser, CRMUser.user_id),
            }.get(participant_type)
            if related_model is None:
                raise LeadServiceError(
                    "Custom participants cannot reference a local CRM record",
                    status_code=400,
                )
            model, id_column = related_model
            stmt = select(id_column).where(
                col(model.organisation_id) == self.organisation_id,
                col(id_column) == related_local_id,
                col(model.is_deleted).is_(False),
            )
            if (await self.org_session.execute(stmt)).scalars().first() is None:
                raise LeadServiceError("Related participant not found", status_code=404)

        participant = CRMLeadParticipant(
            **payload,
            lead_id=lead_id,
            organisation_id=self.organisation_id,
        )
        self.org_session.add(participant)
        await self.org_session.commit()
        await self.org_session.refresh(participant)
        return participant

    async def copy_participants_to_deal(
        self,
        *,
        lead_participants: list[CRMLeadParticipant],
        deal_id: UUID,
        deal_integration_id: UUID,
    ) -> int:
        copied_count = 0
        now = datetime.now(UTC)
        seen_identities: set[tuple[str, str]] = set()
        for participant in lead_participants:
            participant_type = CRMParticipantType(self._enum_value(participant.participant_type))
            related_email = self._normalize_email(participant.email)
            if participant.related_local_id is None and not (
                participant_type == CRMParticipantType.CONTACT and related_email
            ):
                continue
            identity_key = self._deal_participant_identity_key(
                participant_type=participant_type,
                related_local_id=participant.related_local_id,
                email=related_email,
            )
            if identity_key in seen_identities:
                continue
            seen_identities.add(identity_key)
            self.org_session.add(
                CRMDealParticipant(
                    organisation_id=self.organisation_id,
                    integration_id=deal_integration_id,
                    deal_id=deal_id,
                    participant_type=participant_type,
                    relationship_role=participant.relationship_role,
                    related_local_id=participant.related_local_id,
                    related_name=participant.name,
                    related_email=related_email,
                    synced_at=now,
                    source_modified_at=now,
                    last_seen_at=now,
                    participant_source="MANUAL",
                    source_reference_id=participant.participant_id,
                )
            )
            copied_count += 1
        await self.org_session.flush()
        return copied_count

    def _deal_participant_identity_key(
        self,
        *,
        participant_type: CRMParticipantType,
        related_local_id: UUID | None,
        email: str | None,
    ) -> tuple[str, str]:
        if related_local_id is not None:
            return participant_type.value, f"local:{related_local_id}"
        normalized_email = self._normalize_email(email)
        if participant_type == CRMParticipantType.CONTACT and normalized_email:
            return participant_type.value, f"email:{normalized_email}"
        return participant_type.value, ""

    @staticmethod
    def _normalize_email(value: str | None) -> str | None:
        if not value:
            return None
        normalized = value.strip().lower()
        return normalized or None
