"""Authenticated CRM lead write service."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import update
from src.domain.models.organisation_tables.sync.crm.leads import (
    CRMLead,
    CRMLeadStatus,
    CRMLeadStatusHistory,
)
from src.service.crm.leads.contracts import LeadServiceError
from src.service.crm.leads.read_service import LeadReadService


class LeadWriteService(LeadReadService):
    """Create and update first-party CRM leads."""

    async def create_lead(self, payload: dict[str, Any]) -> CRMLead:
        """Create a manual/API lead in the current organisation."""
        self._reject_direct_converted_status(payload)
        actor_crm_user_id = await self._resolve_actor_crm_user_id()
        now = datetime.now(UTC)
        owner_fields: dict[str, Any] = {}
        if "owner_id" not in payload and actor_crm_user_id is not None:
            owner_fields = {
                "owner_id": actor_crm_user_id,
                "owner_email": self.access_actor.email if self.access_actor else None,
                "assigned_at": now,
            }
        lead = CRMLead(
            **payload,
            **owner_fields,
            organisation_id=self.organisation_id,
            created_by_user_id=actor_crm_user_id,
            updated_by_user_id=actor_crm_user_id,
        )
        self.org_session.add(lead)
        await self.org_session.commit()
        await self.org_session.refresh(lead)
        return lead

    async def update_lead(self, lead_id: UUID, payload: dict[str, Any]) -> CRMLead:
        """Update mutable lead fields and audit status transitions."""
        self._reject_direct_converted_status(payload)
        lead = await self._get_lead_or_404(lead_id, "control")
        old_status = lead.status
        actor_crm_user_id = await self._resolve_actor_crm_user_id()

        for key, value in payload.items():
            setattr(lead, key, value)
        lead.updated_by_user_id = actor_crm_user_id

        if "status" in payload and payload["status"] != old_status:
            self._apply_status_timestamp(lead, payload["status"])
            self.org_session.add(
                CRMLeadStatusHistory(
                    organisation_id=self.organisation_id,
                    lead_id=lead_id,
                    from_status=old_status,
                    to_status=payload["status"],
                    actor_user_id=actor_crm_user_id,
                    source="manual",
                )
            )

        self.org_session.add(lead)
        await self.org_session.commit()
        await self.org_session.refresh(lead)
        return lead

    async def claim_lead(self, lead_id: UUID) -> CRMLead:
        """Atomically assign one unassigned lead to the authenticated member."""
        if self.actor_user_id is None:
            raise LeadServiceError("Lead claim requires an authenticated user", 401)
        await self._get_lead_or_404(lead_id, "view")
        crm_user_id = await self._resolve_actor_crm_user_id()
        if crm_user_id is None:
            raise LeadServiceError("Lead claim requires an authenticated user", 401)

        now = datetime.now(UTC)
        stmt = (
            update(CRMLead)
            .where(
                CRMLead.organisation_id == self.organisation_id,
                CRMLead.lead_id == lead_id,
                CRMLead.is_deleted.is_(False),
                CRMLead.owner_id.is_(None),
            )
            .values(
                owner_id=crm_user_id,
                owner_email=self.access_actor.email if self.access_actor else None,
                assigned_at=now,
                updated_by_user_id=crm_user_id,
            )
            .returning(CRMLead)
        )
        lead = (await self.org_session.execute(stmt)).scalar_one_or_none()
        if lead is None:
            raise LeadServiceError("Lead has already been claimed", status_code=409)
        await self.org_session.commit()
        return lead

    async def archive_lead(self, lead_id: UUID, reason: str | None = None) -> CRMLead:
        """Soft-archive a lead; the access policy limits this action to admins."""
        lead = await self._get_lead_or_404(lead_id, "admin")
        lead.is_deleted = True
        lead.archive_reason = reason.strip() if reason and reason.strip() else None
        lead.source_modified_at = datetime.now(UTC)
        lead.updated_by_user_id = await self._resolve_actor_crm_user_id()
        self.org_session.add(lead)
        await self.org_session.commit()
        await self.org_session.refresh(lead)
        return lead

    @staticmethod
    def _apply_status_timestamp(lead: CRMLead, status: CRMLeadStatus | str) -> None:
        now = datetime.now(UTC)
        status_value = status.value if isinstance(status, CRMLeadStatus) else status
        if status_value == CRMLeadStatus.QUALIFIED.value:
            lead.qualified_at = now
        elif status_value == CRMLeadStatus.UNQUALIFIED.value:
            lead.disqualified_at = now

    @staticmethod
    def _reject_direct_converted_status(payload: dict[str, Any]) -> None:
        status = payload.get("status")
        status_value = LeadReadService._enum_value(status)
        if status_value == CRMLeadStatus.CONVERTED.value:
            raise LeadServiceError(
                "Use the lead conversion endpoint to set converted status",
                status_code=422,
                data={"code": "converted_status_not_allowed", "field": "status"},
            )
