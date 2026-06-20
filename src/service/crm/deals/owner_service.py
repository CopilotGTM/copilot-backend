"""Owner assignment helpers for internal CRM deals."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.service.crm.deals.contracts import DealOwnerChangeResult
from src.service.crm.deals.mutation_event_service import changed_field
from src.service.crm.participants.participant_write_service import ParticipantWriteService


class DealOwnerService:
    """Owns owner snapshot changes once the CRM user has been resolved."""

    def __init__(
        self,
        session: AsyncSession | None = None,
        organisation_id: UUID | None = None,
        actor_user_id: UUID | None = None,
        master_session: AsyncSession | None = None,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.actor_user_id = actor_user_id
        self.master_session = master_session
        self._participant_writer: ParticipantWriteService | None = None
        if session is not None and organisation_id is not None and actor_user_id is not None:
            self._participant_writer = ParticipantWriteService(
                session=session,
                organisation_id=organisation_id,
                actor_user_id=actor_user_id,
                master_session=master_session,
            )

    def apply_owner(self, *, deal: CRMDeal, crm_user: CRMUser | None) -> DealOwnerChangeResult:
        """Set or clear owner fields on a loaded deal."""
        old_values = {
            "owner_id": deal.owner_id,
            "owner_name": deal.owner_name,
            "owner_email": deal.owner_email,
        }
        deal.owner_id = crm_user.user_id if crm_user else None
        deal.owner_name = crm_user.full_name if crm_user else None
        deal.owner_email = crm_user.email if crm_user else None

        changed_fields: dict[str, dict[str, Any]] = {}
        for name, old in old_values.items():
            if change := changed_field(old, getattr(deal, name)):
                changed_fields[name] = change

        return DealOwnerChangeResult(
            deal=deal,
            changed_fields=changed_fields,
            changed=bool(changed_fields),
        )

    async def sync_owner_participant(self, *, deal: CRMDeal, crm_user: CRMUser) -> None:
        """Delegate owner projection writes to the participant boundary."""
        participant_writer = self._require_participant_writer()
        await participant_writer.sync_owner_participant(deal=deal, owner_user_id=crm_user.user_id)

    async def clear_owner_participants(self, *, deal: CRMDeal) -> None:
        """Delegate owner projection cleanup to the participant boundary."""
        participant_writer = self._require_participant_writer()
        await participant_writer.clear_owner_participants(deal=deal)

    def _require_participant_writer(self) -> ParticipantWriteService:
        if self._participant_writer is None:
            raise RuntimeError("DealOwnerService requires participant dependencies")
        return self._participant_writer
