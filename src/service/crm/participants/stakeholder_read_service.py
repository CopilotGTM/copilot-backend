"""Participant-backed stakeholder read service.

Called by stakeholder API routes and agent tools that need product stakeholder
views. The storage row remains `CRMDealParticipant`; this service joins contact
profile data only where detail views need durable person fields.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.contracts.crm.stakeholders import (
    AvailableContactItem,
    StakeholderDetail,
    StakeholderListResponse,
)
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
        contact_stmt = select(CRMContact).where(
            col(CRMContact.contact_id) == participant.related_local_id,
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
        )
        contact_result = await self.session.execute(contact_stmt)
        return contact_result.scalar_one_or_none()

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

        return StakeholderListResponse(
            company_domain=company_domain,
            company_name=company_name,
            stakeholders=[self._participant_detail(participant) for participant in participants],
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

    async def list_available_contacts(
        self, deal_id: UUID, query: str | None = None, limit: int = 50
    ) -> list[AvailableContactItem]:
        """List CRM contacts that can still be added as deal stakeholders.

        Exclusion is by contact ID and normalized email because historic data can
        have one without the other; using both avoids duplicate stakeholder rows.
        """
        contact_id_subq = select(col(CRMDealParticipant.related_local_id)).where(
            col(CRMDealParticipant.deal_id) == deal_id,
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
            col(CRMDealParticipant.is_deleted).is_(False),
            col(CRMDealParticipant.related_local_id).isnot(None),
        )
        email_subq = select(func.lower(func.trim(col(CRMDealParticipant.related_email)))).where(
            col(CRMDealParticipant.deal_id) == deal_id,
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
            col(CRMDealParticipant.is_deleted).is_(False),
            col(CRMDealParticipant.related_email).isnot(None),
            col(CRMDealParticipant.related_email) != "",
        )

        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.contact_id).not_in(contact_id_subq),
            func.lower(func.trim(col(CRMContact.email))).not_in(email_subq),
            col(CRMContact.email).isnot(None),
            col(CRMContact.email) != "",
        )

        if query:
            search = f"%{query}%"
            # The UI uses this endpoint as typeahead, so the search spans identity
            # and company fields without exposing contacts outside the org filter.
            stmt = stmt.where(
                (col(CRMContact.first_name).ilike(search))
                | (col(CRMContact.last_name).ilike(search))
                | (col(CRMContact.full_name).ilike(search))
                | (col(CRMContact.email).ilike(search))
                | (col(CRMContact.company_name).ilike(search))
            )

        stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        contacts = list(result.scalars().all())

        available_contacts: list[AvailableContactItem] = []
        for contact in contacts:
            if not contact.email:
                continue
            available_contacts.append(
                AvailableContactItem(
                    contact_id=contact.contact_id,
                    external_id=None,
                    email=contact.email,
                    first_name=contact.first_name,
                    last_name=contact.last_name,
                    full_name=contact.full_name,
                    job_title=contact.job_title,
                    company_name=contact.company_name,
                    phone=contact.phone,
                    lifecycle_stage=contact.lifecycle_stage,
                    last_activity_at=contact.last_activity_at,
                )
            )
        return available_contacts

    async def close(self) -> None:
        """Close the caller-provided org session when dependency cleanup owns it."""
        await self.org_session.close()
