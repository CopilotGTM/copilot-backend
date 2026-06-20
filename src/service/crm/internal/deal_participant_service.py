"""Compatibility facade for internal CRM deal participant writes.

Older routers and tests still import `DealParticipantService` and expect tuple
return values. New code should call `ParticipantWriteService`, which returns
typed result objects and keeps transaction ownership with the caller.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealParticipant
from src.service.crm.participants.contracts import (
    ParticipantServiceError,
    ParticipantSkipped,
)
from src.service.crm.participants.participant_write_service import ParticipantWriteService


class DealParticipantServiceError(Exception):
    """HTTP-shaped error kept for legacy participant route callers."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


DealParticipantSkipped = ParticipantSkipped


class DealParticipantService:
    """Delegate legacy participant calls to the canonical writer."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        actor_user_id: UUID,
        master_session: AsyncSession | None = None,
    ):
        self._writer = ParticipantWriteService(
            session=session,
            organisation_id=organisation_id,
            actor_user_id=actor_user_id,
            master_session=master_session,
        )

    @property
    def session(self) -> AsyncSession:
        """Expose the wrapped session for legacy dependency cleanup."""
        return self._writer.session

    @property
    def organisation_id(self) -> UUID:
        """Return the immutable organisation boundary captured by the writer."""
        return self._writer.organisation_id

    @property
    def actor_user_id(self) -> UUID:
        """Return the user whose write permissions are being applied."""
        return self._writer.actor_user_id

    async def close(self) -> None:
        """Close the wrapped writer session when the old dependency owns it."""
        await self._writer.close()

    async def add_participants(
        self, deal_id: UUID, payloads: list[dict[str, Any]]
    ) -> tuple[list[CRMDealParticipant], list[ParticipantSkipped], bool, UUID]:
        """Add participants using the legacy tuple response shape.

        This facade intentionally translates only the return contract; validation
        and DB writes remain owned by `ParticipantWriteService`.
        """
        try:
            result = await self._writer.add_participants(deal_id, payloads)
        except ParticipantServiceError as exc:
            raise DealParticipantServiceError(exc.message, exc.status_code) from exc
        return (
            result.created,
            result.skipped,
            result.contact_participant_changed,
            result.integration_id,
        )

    async def remove_participant(
        self, deal_id: UUID, participant_id: UUID
    ) -> tuple[CRMDealParticipant, bool, UUID]:
        """Remove one participant while preserving the legacy return tuple."""
        try:
            return await self._writer.remove_participant(deal_id, participant_id)
        except ParticipantServiceError as exc:
            raise DealParticipantServiceError(exc.message, exc.status_code) from exc

    async def update_participant_role(
        self,
        deal_id: UUID,
        participant_id: UUID,
        relationship_role: str | None,
    ) -> CRMDealParticipant:
        """Update participant role metadata through the canonical writer."""
        try:
            return await self._writer.update_participant_role(
                deal_id,
                participant_id,
                relationship_role,
            )
        except ParticipantServiceError as exc:
            raise DealParticipantServiceError(exc.message, exc.status_code) from exc
