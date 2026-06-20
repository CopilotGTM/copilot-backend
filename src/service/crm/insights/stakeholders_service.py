"""Compatibility facade for participant-backed stakeholder services.

The old insights import path remains during rollout, but the SQL and mutation
rules now live in `src.service.crm.participants`. This facade preserves legacy
commit behavior for callers that have not yet moved to caller-owned commits.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.contracts.crm.stakeholders import (
    AddContactsAsStakeholdersResponse,
    AvailableContactItem,
    CreateStakeholderRequest,
    StakeholderDetail,
    StakeholderListResponse,
    UpdateStakeholderRequest,
)
from src.infra.utils.logger import get_logger
from src.service.crm.participants.errors import StakeholdersServiceError
from src.service.crm.participants.stakeholder_read_service import StakeholderReadService
from src.service.crm.participants.stakeholder_write_service import StakeholderWriteService

logger = get_logger("stakeholders_service")

__all__ = ["StakeholdersService", "StakeholdersServiceError"]


class StakeholdersService:
    """Delegate old stakeholder service calls to participant read/write services."""

    def __init__(
        self,
        org_session: AsyncSession,
        organisation_id: UUID,
        user_id: UUID | None = None,
        *,
        commit_writes: bool = True,
    ):
        self.org_session = org_session
        self.organisation_id = organisation_id
        self.user_id = user_id
        self._commit_writes = commit_writes
        self._reader = StakeholderReadService(org_session, organisation_id, user_id)
        self._writer = StakeholderWriteService(org_session, organisation_id, user_id)

    @property
    def session(self) -> AsyncSession:
        """Expose the original org session for old dependency cleanup and tests."""
        return self.org_session

    async def _finish_compat_write(self) -> None:
        """Preserve old commit semantics until every caller uses new services.

        New participant services flush only. This method exists so remaining
        `StakeholdersService` callers do not silently lose their historical
        service-owned commit behavior during the rollout.
        """
        if self._commit_writes:
            await self.session.commit()
        else:
            await self.session.flush()

    async def list_stakeholders_by_deal(
        self,
        deal_id: UUID,
        include_pending: bool = True,
        include_rejected: bool = False,
    ) -> StakeholderListResponse:
        """Read stakeholder list data through the participant-backed reader."""
        return await self._reader.list_stakeholders_by_deal(
            deal_id=deal_id,
            include_pending=include_pending,
            include_rejected=include_rejected,
        )

    async def get_stakeholder_detail(
        self, deal_id: UUID, participant_id: UUID
    ) -> dict[str, Any] | None:
        """Read one stakeholder detail payload by canonical participant ID."""
        return await self._reader.get_stakeholder_detail(deal_id, participant_id)

    async def list_available_contacts(
        self, deal_id: UUID, query: str | None = None, limit: int = 50
    ) -> list[AvailableContactItem]:
        """Read contacts that can still become stakeholder participants."""
        return await self._reader.list_available_contacts(deal_id, query, limit)

    async def create_stakeholder(
        self, deal_id: UUID, request: CreateStakeholderRequest
    ) -> StakeholderDetail:
        """Create a stakeholder and keep legacy service-owned commit behavior."""
        stakeholder = await self._writer.create_stakeholder(deal_id, request)
        await self._finish_compat_write()
        return stakeholder

    async def add_contacts_as_stakeholders(
        self, deal_id: UUID, contact_ids: list[UUID]
    ) -> AddContactsAsStakeholdersResponse:
        """Add CRM contacts as stakeholders using the old service import path."""
        response = await self._writer.add_contacts_as_stakeholders(deal_id, contact_ids)
        await self._finish_compat_write()
        return response

    async def update_stakeholder(
        self,
        deal_id: UUID,
        participant_id: UUID,
        request: UpdateStakeholderRequest,
    ) -> StakeholderDetail:
        """Update stakeholder profile/behavior fields through the new writer."""
        stakeholder = await self._writer.update_stakeholder(deal_id, participant_id, request)
        await self._finish_compat_write()
        return stakeholder

    async def approve_stakeholder(self, deal_id: UUID, participant_id: UUID) -> StakeholderDetail:
        """Approve a pending participant-backed stakeholder."""
        stakeholder = await self._writer.approve_stakeholder(deal_id, participant_id)
        await self._finish_compat_write()
        return stakeholder

    async def reject_stakeholder(self, deal_id: UUID, participant_id: UUID) -> StakeholderDetail:
        """Reject a participant-backed stakeholder from matching workflows."""
        stakeholder = await self._writer.reject_stakeholder(deal_id, participant_id)
        await self._finish_compat_write()
        return stakeholder

    async def remove_stakeholder(self, deal_id: UUID, participant_id: UUID) -> StakeholderDetail:
        """Remove a stakeholder while keeping the old import path working."""
        stakeholder = await self._writer.remove_stakeholder(deal_id, participant_id)
        await self._finish_compat_write()
        return stakeholder

    async def close(self) -> None:
        """Close the org session for legacy FastAPI dependency cleanup."""
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("StakeholdersService close exception", exc_info=True)
