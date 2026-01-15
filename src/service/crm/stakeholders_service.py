"""CRM service layer for managing deal stakeholders."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.schemas.crm_models import (
    AddContactsAsStakeholdersResponse,
    AvailableContactItem,
    CreateStakeholderRequest,
    SkippedContact,
    StakeholderDetail,
    StakeholderListResponse,
)
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
    DealStakeholder,
    StakeholderSource,
)
from src.infra.utils.logger import get_logger

logger = get_logger("stakeholders_service")


class StakeholdersServiceError(Exception):
    """Custom exception for stakeholders service errors."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class StakeholdersService:
    """Service for managing CRM deal stakeholders."""

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id

    @property
    def session(self) -> AsyncSession:
        return self.org_session

    async def list_stakeholders_by_deal(
        self,
        deal_id: UUID,
        include_pending: bool = True,
        include_rejected: bool = False,
    ) -> StakeholderListResponse:
        """Get stakeholders associated with a specific deal.

        Args:
            deal_id: The deal to get stakeholders for.
            include_pending: Include PENDING stakeholders (default True).
            include_rejected: Include REJECTED stakeholders (default False).

        Returns:
            StakeholderListResponse with company_domain for frontend validation
            and list of stakeholders filtered by approval_status.
        """
        # Get deal to fetch company_domain for frontend validation
        deal_stmt = select(CRMDeal).where(
            col(CRMDeal.deal_id) == deal_id,
            col(CRMDeal.organisation_id) == self.organisation_id,
        )
        deal_result = await self.session.execute(deal_stmt)
        deal = deal_result.scalar_one_or_none()

        company_domain = deal.company_domain if deal else None
        company_name = deal.company_name if deal else None

        # Build list of approval statuses to include
        statuses = [ApprovalStatus.APPROVED]
        if include_pending:
            statuses.append(ApprovalStatus.PENDING)
        if include_rejected:
            statuses.append(ApprovalStatus.REJECTED)

        stmt = select(DealStakeholder).where(
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.organisation_id) == self.organisation_id,
            col(DealStakeholder.approval_status).in_(statuses),
        )
        result = await self.session.execute(stmt)
        stakeholders = list(result.scalars().all())

        return StakeholderListResponse(
            company_domain=company_domain,
            company_name=company_name,
            stakeholders=[StakeholderDetail.model_validate(s) for s in stakeholders],
        )

    async def get_stakeholder(self, deal_id: UUID, stakeholder_id: UUID) -> DealStakeholder | None:
        """Get a specific stakeholder by ID."""
        stmt = select(DealStakeholder).where(
            col(DealStakeholder.stakeholder_id) == stakeholder_id,
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.organisation_id) == self.organisation_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_stakeholder_detail(self, deal_id: UUID, stakeholder_id: UUID) -> dict | None:
        """Get full stakeholder detail including engagement history.

        Returns the stakeholder with all fields including engagement_history
        which shows how the stakeholder's metrics changed over time.

        Args:
            deal_id: The deal ID.
            stakeholder_id: The stakeholder ID.

        Returns:
            Dict with full stakeholder detail and engagement history, or None if not found.
        """
        stakeholder = await self.get_stakeholder(deal_id, stakeholder_id)
        if not stakeholder:
            return None

        # Format engagement history from JSONB field
        engagement_history = []
        raw_history = stakeholder.stakeholder_engagement_history or []
        for snapshot in raw_history:
            if isinstance(snapshot, dict):
                engagement_history.append(
                    {
                        "recorded_at": snapshot.get("recorded_at"),
                        "stance": snapshot.get("stance"),
                        "temperature": snapshot.get("temperature"),
                        "engagement_score": snapshot.get("engagement_score"),
                        "champion_potential": snapshot.get("champion_potential"),
                        "decision_power": snapshot.get("decision_power"),
                    }
                )

        return {
            "stakeholder_id": stakeholder.stakeholder_id,
            "deal_id": stakeholder.deal_id,
            "contact_id": stakeholder.contact_id,
            "email": stakeholder.email,
            "name": stakeholder.name,
            "job_title": stakeholder.job_title,
            "department": stakeholder.department,
            "external_links": stakeholder.external_links,
            "stance": stakeholder.stance,
            "decision_power": stakeholder.decision_power,
            "champion_potential": stakeholder.champion_potential,
            "motivation": stakeholder.motivation,
            "pain_points": stakeholder.pain_points,
            "keywords": stakeholder.keywords,
            "last_contact_at": stakeholder.last_contact_at,
            "engagement_score": stakeholder.engagement_score,
            "temperature": stakeholder.temperature,
            "source": stakeholder.source,
            "approval_status": stakeholder.approval_status,
            "engagement_history": engagement_history,
            "created_at": stakeholder.created_at,
            "updated_at": stakeholder.updated_at,
        }

    async def approve_stakeholder(self, deal_id: UUID, stakeholder_id: UUID) -> DealStakeholder:
        """Approve a PENDING stakeholder for deal matching.

        Args:
            deal_id: The deal ID.
            stakeholder_id: The stakeholder to approve.

        Returns:
            The updated stakeholder.

        Raises:
            StakeholdersServiceError: If stakeholder not found or already approved.
        """
        stakeholder = await self.get_stakeholder(deal_id, stakeholder_id)
        if not stakeholder:
            raise StakeholdersServiceError("Stakeholder not found", status_code=404)

        if stakeholder.approval_status == ApprovalStatus.APPROVED:
            raise StakeholdersServiceError("Stakeholder is already approved", status_code=400)

        stakeholder.approval_status = ApprovalStatus.APPROVED
        await self.session.commit()
        await self.session.refresh(stakeholder)
        return stakeholder

    async def reject_stakeholder(self, deal_id: UUID, stakeholder_id: UUID) -> DealStakeholder:
        """Reject a stakeholder to exclude from deal matching.

        Args:
            deal_id: The deal ID.
            stakeholder_id: The stakeholder to reject.

        Returns:
            The updated stakeholder.

        Raises:
            StakeholdersServiceError: If stakeholder not found or already rejected.
        """
        stakeholder = await self.get_stakeholder(deal_id, stakeholder_id)
        if not stakeholder:
            raise StakeholdersServiceError("Stakeholder not found", status_code=404)

        if stakeholder.approval_status == ApprovalStatus.REJECTED:
            raise StakeholdersServiceError("Stakeholder is already rejected", status_code=400)

        stakeholder.approval_status = ApprovalStatus.REJECTED
        await self.session.commit()
        await self.session.refresh(stakeholder)
        return stakeholder

    async def remove_stakeholder(self, deal_id: UUID, stakeholder_id: UUID) -> DealStakeholder:
        """Remove an APPROVED stakeholder from the deal.

        This marks the stakeholder as REMOVED, excluding them from deal matching
        and stakeholder lists. Unlike REJECTED, REMOVED indicates the stakeholder
        was previously active but has been removed by the user.

        Args:
            deal_id: The deal ID.
            stakeholder_id: The stakeholder to remove.

        Returns:
            The updated stakeholder with REMOVED status.

        Raises:
            StakeholdersServiceError: If stakeholder not found or not in APPROVED status.
        """
        stakeholder = await self.get_stakeholder(deal_id, stakeholder_id)
        if not stakeholder:
            raise StakeholdersServiceError("Stakeholder not found", status_code=404)

        if stakeholder.approval_status == ApprovalStatus.REMOVED:
            raise StakeholdersServiceError("Stakeholder is already removed", status_code=400)

        if stakeholder.approval_status != ApprovalStatus.APPROVED:
            raise StakeholdersServiceError(
                "Only approved stakeholders can be removed", status_code=400
            )

        stakeholder.approval_status = ApprovalStatus.REMOVED
        await self.session.commit()
        await self.session.refresh(stakeholder)
        return stakeholder

    async def create_stakeholder(
        self, deal_id: UUID, request: CreateStakeholderRequest
    ) -> StakeholderDetail:
        """Manually create a new stakeholder for a deal.

        Args:
            deal_id: The deal to add the stakeholder to.
            request: The stakeholder creation request with user-provided details.

        Returns:
            The created stakeholder as StakeholderDetail DTO.

        Raises:
            StakeholdersServiceError: If email already exists for this deal.
        """
        # Check for duplicate email
        existing_stmt = select(DealStakeholder).where(
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.organisation_id) == self.organisation_id,
            col(DealStakeholder.email) == request.email.lower().strip(),
        )
        existing_result = await self.session.execute(existing_stmt)
        if existing_result.scalar_one_or_none():
            raise StakeholdersServiceError(
                "A stakeholder with this email already exists for this deal",
                status_code=409,
            )

        # Convert external_links from Pydantic models to dicts
        external_links_data = None
        if request.external_links:
            external_links_data = [
                {
                    "platform": link.platform,
                    "url": link.url,
                    "label": link.label,
                    "added_at": link.added_at.isoformat() if link.added_at else None,
                }
                for link in request.external_links
            ]

        # Create the stakeholder with manual source
        # Use neutral defaults for optional numeric fields
        champion_potential = (
            request.champion_potential if request.champion_potential is not None else 50.0
        )

        new_stakeholder = DealStakeholder(
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            contact_id=None,  # Manual adds have no CRM contact
            email=request.email.lower().strip(),
            name=request.name,
            job_title=request.job_title,
            department=request.department,
            external_links=external_links_data,
            stance=request.stance,
            decision_power=request.decision_power,
            champion_potential=champion_potential,
            motivation=request.motivation,
            pain_points=request.pain_points,
            source=StakeholderSource.MANUAL,
            approval_status=ApprovalStatus.APPROVED,  # Manual adds are trusted
        )
        self.session.add(new_stakeholder)
        await self.session.commit()
        await self.session.refresh(new_stakeholder)

        return StakeholderDetail.model_validate(new_stakeholder)

    async def list_available_contacts(
        self, deal_id: UUID, query: str | None = None, limit: int = 50
    ) -> list[AvailableContactItem]:
        """List contacts that are not yet stakeholders for the deal.

        Only returns contacts with valid email addresses, as email is required
        for stakeholder identification.
        """
        # Subquery for existing stakeholders
        subq = select(col(DealStakeholder.contact_id)).where(
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.organisation_id) == self.organisation_id,
        )

        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.contact_id).not_in(subq),
            col(CRMContact.email).isnot(None),  # Only contacts with email
            col(CRMContact.email) != "",  # Exclude empty email strings
        )

        if query:
            search = f"%{query}%"
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

        # Convert to AvailableContactItem with relevant information
        return [
            AvailableContactItem(
                contact_id=c.contact_id,
                external_id=c.external_id,
                email=c.email,  # type: ignore - filtered above
                first_name=c.first_name,
                last_name=c.last_name,
                full_name=c.full_name,
                job_title=c.job_title,
                company_name=c.company_name,
                phone=c.phone,
                lifecycle_stage=c.lifecycle_stage,
                last_activity_at=c.last_activity_at,
            )
            for c in contacts
        ]

    async def add_contacts_as_stakeholders(
        self, deal_id: UUID, contact_ids: list[UUID]
    ) -> AddContactsAsStakeholdersResponse:
        """Add selected contacts as stakeholders to a deal.

        Only contacts with valid email addresses are added. Contacts without
        emails or those that already exist as stakeholders are skipped.
        """
        added: list[StakeholderDetail] = []
        skipped: list[SkippedContact] = []

        # Fetch all requested contacts
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.contact_id).in_(contact_ids),
        )
        result = await self.session.execute(stmt)
        contacts_map = {c.contact_id: c for c in result.scalars().all()}

        # Get existing stakeholder emails for this deal
        existing_stmt = select(DealStakeholder.email, DealStakeholder.contact_id).where(  # type: ignore
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.organisation_id) == self.organisation_id,
        )
        existing_result = await self.session.execute(existing_stmt)
        existing_rows = existing_result.all()
        existing_emails = {row.email.lower() for row in existing_rows if row.email}
        existing_contact_ids = {row.contact_id for row in existing_rows if row.contact_id}

        for contact_id in contact_ids:
            contact = contacts_map.get(contact_id)

            # Contact not found
            if not contact:
                skipped.append(SkippedContact(contact_id=contact_id, reason="Contact not found"))
                continue

            # No email
            if not contact.email or not contact.email.strip():
                skipped.append(
                    SkippedContact(
                        contact_id=contact_id,
                        reason="Contact has no email address",
                    )
                )
                continue

            # Already exists by email
            if contact.email.lower() in existing_emails:
                skipped.append(
                    SkippedContact(
                        contact_id=contact_id,
                        reason="Stakeholder with this email already exists",
                    )
                )
                continue

            # Already exists by contact_id
            if contact_id in existing_contact_ids:
                skipped.append(
                    SkippedContact(
                        contact_id=contact_id,
                        reason="Contact is already a stakeholder",
                    )
                )
                continue

            # Build name from contact
            name = (
                contact.full_name
                or " ".join(filter(None, [contact.first_name, contact.last_name]))
                or None
            )

            # Create the stakeholder
            new_stakeholder = DealStakeholder(
                organisation_id=self.organisation_id,
                deal_id=deal_id,
                contact_id=contact.contact_id,
                email=contact.email,
                name=name,
                job_title=contact.job_title,
                source=StakeholderSource.CRM,
            )
            self.session.add(new_stakeholder)

            # Track for response - add email to existing set
            existing_emails.add(contact.email.lower())
            existing_contact_ids.add(contact_id)

        # Commit all new stakeholders
        await self.session.commit()

        # Refresh added stakeholders for response
        added_stmt = select(DealStakeholder).where(
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.organisation_id) == self.organisation_id,
            col(DealStakeholder.contact_id).in_(
                [cid for cid in contact_ids if cid not in {s.contact_id for s in skipped}]
            ),
        )
        added_result = await self.session.execute(added_stmt)
        added_stakeholders = list(added_result.scalars().all())

        # Convert to StakeholderDetail
        added = [StakeholderDetail.model_validate(s) for s in added_stakeholders]

        return AddContactsAsStakeholdersResponse(
            added=added,
            skipped=skipped,
            total_requested=len(contact_ids),
            total_added=len(added),
            total_skipped=len(skipped),
        )

    async def close(self):
        """Dispose the underlying session when the service is released."""
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("StakeholdersService close exception", exc_info=True)
