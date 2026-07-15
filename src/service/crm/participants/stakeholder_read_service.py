"""Participant-backed stakeholder read service.

Called by stakeholder API routes and agent tools that need product stakeholder
views. The storage row remains `CRMDealParticipant`; this service joins contact
profile data so list and detail reads both prefer durable person fields from
`CRMContact`.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer
from sqlmodel import col
from src.contracts.crm.stakeholders import StakeholderDetail, StakeholderListResponse
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import ApprovalStatus
from src.service.crm.participants.stakeholder_adapter import (
    participant_to_stakeholder_detail_dict,
)


class StakeholderReadService:
    """Read stakeholder DTOs from the canonical participant table."""

    def __init__(
        self,
        org_session: AsyncSession,
        organisation_id: UUID,
        user_id: UUID | None = None,
    ):
        self.org_session = org_session
        self.organisation_id = organisation_id
        self.user_id = user_id

    @property
    def session(self) -> AsyncSession:
        """Expose the org-scoped session for route/tool cleanup symmetry."""
        return self.org_session

    async def _get_participant_contact(self, participant: CRMDealParticipant) -> CRMContact | None:
        if not participant.related_local_id:
            return None

        # The participant ID is not enough as a trust boundary; the contact join
        # must stay inside the current organisation's schema data.
        contact_stmt = (
            select(CRMContact)
            .options(defer(cast(Any, CRMContact).profile_enrichment))
            .where(
                col(CRMContact.contact_id) == participant.related_local_id,
                col(CRMContact.organisation_id) == self.organisation_id,
                col(CRMContact.is_deleted).is_(False),
            )
        )
        contact_result = await self.session.execute(contact_stmt)
        return contact_result.scalar_one_or_none()

    async def _get_participant_contacts(
        self,
        participants: list[CRMDealParticipant],
    ) -> dict[UUID, CRMContact]:
        """Load linked contacts for a stakeholder page in one org-scoped query."""
        contact_ids = {
            participant.related_local_id
            for participant in participants
            if participant.related_local_id is not None
        }
        if not contact_ids:
            return {}

        # List responses can contain caller-supplied deal IDs, so the contact
        # join repeats the organisation predicate instead of trusting local IDs.
        contact_stmt = (
            select(CRMContact)
            .options(defer(cast(Any, CRMContact).profile_enrichment))
            .where(
                col(CRMContact.contact_id).in_(contact_ids),
                col(CRMContact.organisation_id) == self.organisation_id,
                col(CRMContact.is_deleted).is_(False),
            )
        )
        contact_result = await self.session.execute(contact_stmt)
        return {contact.contact_id: contact for contact in contact_result.scalars().all()}

    def _participant_detail(
        self,
        participant: CRMDealParticipant,
        *,
        contact: CRMContact | None = None,
    ) -> StakeholderDetail:
        """Return the API DTO for one contact participant."""
        return StakeholderDetail.model_validate(
            participant_to_stakeholder_detail_dict(participant, contact=contact)
        )

    async def list_stakeholders_by_deal(
        self,
        deal_id: UUID,
        include_pending: bool = True,
        include_rejected: bool = False,
        limit: int | None = None,
    ) -> StakeholderListResponse:
        """List visible stakeholder participants for one deal.

        The deal lookup is org-scoped before reading participants so a caller
        cannot infer company metadata from a deal ID outside its organisation.
        Agent tools pass a limit to preserve their compact response contract.
        """
        deal_stmt = select(CRMDeal).where(
            col(CRMDeal.deal_id) == deal_id,
            col(CRMDeal.organisation_id) == self.organisation_id,
        )
        deal_result = await self.session.execute(deal_stmt)
        deal = deal_result.scalar_one_or_none()

        company_domain = deal.company_domain if deal else None
        company_name = deal.company_name if deal else None

        statuses = [ApprovalStatus.APPROVED.value]
        if include_pending:
            statuses.append(ApprovalStatus.PENDING.value)
        if include_rejected:
            statuses.append(ApprovalStatus.REJECTED.value)

        # Stakeholder is product language for active contact participants only;
        # company/user participants stay out of buying-committee views.
        stmt = (
            select(CRMDealParticipant)
            .where(
                col(CRMDealParticipant.deal_id) == deal_id,
                col(CRMDealParticipant.organisation_id) == self.organisation_id,
                col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
                col(CRMDealParticipant.is_deleted).is_(False),
                col(CRMDealParticipant.approval_status).in_(statuses),
            )
            .order_by(
                col(CRMDealParticipant.decision_power).asc(),
                col(CRMDealParticipant.related_name).asc().nullslast(),
            )
        )
        if limit is not None:
            stmt = stmt.limit(max(1, min(limit, 50)))
        result = await self.session.execute(stmt)
        participants = list(result.scalars().all())
        contacts_by_id = await self._get_participant_contacts(participants)

        return StakeholderListResponse(
            company_domain=company_domain,
            company_name=company_name,
            stakeholders=[
                self._participant_detail(
                    participant,
                    contact=(
                        contacts_by_id.get(participant.related_local_id)
                        if participant.related_local_id
                        else None
                    ),
                )
                for participant in participants
            ],
        )

    async def get_stakeholder_detail(
        self, deal_id: UUID, participant_id: UUID
    ) -> dict[str, Any] | None:
        """Load one stakeholder detail payload by participant ID.

        Both `deal_id` and `organisation_id` are checked because participant IDs
        can arrive from agents or clients and must not cross tenant boundaries.
        """
        stmt = select(CRMDealParticipant).where(
            col(CRMDealParticipant.participant_id) == participant_id,
            col(CRMDealParticipant.deal_id) == deal_id,
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
            col(CRMDealParticipant.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        participant = result.scalar_one_or_none()
        if not participant:
            return None

        contact = await self._get_participant_contact(participant)
        payload = participant_to_stakeholder_detail_dict(participant, contact=contact)
        # These fields are route-level enrichments today. Returning stable empty
        # defaults keeps tools and API callers independent of the old insights service.
        payload["engagement_history"] = []
        payload["active_research_job"] = None
        return payload

    async def close(self) -> None:
        """Close the caller-provided org session when dependency cleanup owns it."""
        await self.org_session.close()
