"""CRM service layer for managing deal stakeholders."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.schemas.crm_models import (
    CreateStakeholderRequest,
    UpdateStakeholderRequest,
)
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
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

    async def list_stakeholders_by_deal(self, deal_id: UUID) -> list[DealStakeholder]:
        """Get all stakeholders associated with a specific deal."""
        stmt = select(DealStakeholder).where(
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.organisation_id) == self.organisation_id,
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_available_contacts(
        self, deal_id: UUID, query: str | None = None, limit: int = 50
    ) -> list[CRMContact]:
        """List contacts that are not yet stakeholders for the deal."""
        # Subquery for existing stakeholders
        subq = select(col(DealStakeholder.contact_id)).where(
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.organisation_id) == self.organisation_id,
        )

        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.contact_id).not_in(subq),
        )

        if query:
            search = f"%{query}%"
            stmt = stmt.where(
                (col(CRMContact.first_name).ilike(search))
                | (col(CRMContact.last_name).ilike(search))
                | (col(CRMContact.email).ilike(search))
            )

        stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def create_stakeholder(
        self, deal_id: UUID, request: CreateStakeholderRequest
    ) -> DealStakeholder:
        """Manually add a stakeholder to a deal."""
        # Check if already exists
        stmt = select(DealStakeholder).where(
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.email) == request.email,
            col(DealStakeholder.organisation_id) == self.organisation_id,
        )
        result = await self.session.execute(stmt)
        existing = result.scalars().first()

        if existing:
            raise StakeholdersServiceError(
                "Stakeholder already exists for this deal.", status_code=409
            )

        new_stakeholder = DealStakeholder(
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            email=request.email,
            name=request.name,
            job_title=request.job_title,
            external_links=request.external_links,
            stance=request.stance,
            decision_power=request.decision_power,
            source=StakeholderSource.MANUAL,
        )
        self.session.add(new_stakeholder)
        await self.session.commit()
        await self.session.refresh(new_stakeholder)
        return new_stakeholder

    async def update_stakeholder(
        self, deal_id: UUID, stakeholder_id: UUID, request: UpdateStakeholderRequest
    ) -> DealStakeholder:
        """Update manual fields for a stakeholder."""
        stmt = select(DealStakeholder).where(
            col(DealStakeholder.stakeholder_id) == stakeholder_id,
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.organisation_id) == self.organisation_id,
        )
        result = await self.session.execute(stmt)
        stakeholder = result.scalars().first()

        if not stakeholder:
            raise StakeholdersServiceError("Stakeholder not found.", status_code=404)

        if request.stance is not None:
            stakeholder.stance = request.stance
        if request.decision_power is not None:
            stakeholder.decision_power = request.decision_power
        if request.motivation is not None:
            stakeholder.motivation = request.motivation
        if request.pain_points is not None:
            stakeholder.pain_points = request.pain_points
        if request.external_links is not None:
            stakeholder.external_links = [link.model_dump() for link in request.external_links]

        await self.session.commit()
        await self.session.refresh(stakeholder)
        return stakeholder

    async def close(self):
        """Dispose the underlying session when the service is released."""
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("StakeholdersService close exception", exc_info=True)
