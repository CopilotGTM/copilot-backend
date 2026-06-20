"""CRM service layer for accessing and managing lead data."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.leads import (
    CRMLead,
    CRMLeadParticipant,
    CRMLeadSourceType,
    CRMLeadStatus,
    CRMLeadStatusHistory,
)
from src.infra.utils.logger import get_logger
from src.service.crm.deals.contracts import DealWriteServiceError
from src.service.crm.deals.write_service import DealWriteService
from src.service.crm.internal.internal_crm_user_service import (
    InternalCRMUserService,
    InternalCRMUserServiceError,
)

logger = get_logger("leads_access_service")


class LeadsAccessServiceError(Exception):
    """Custom exception for leads access service errors."""

    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)


class LeadsAccessService:
    """Service for CRM lead CRUD, filtering, and participant management."""

    def __init__(
        self,
        org_session: AsyncSession,
        organisation_id: UUID,
        actor_user_id: UUID | None = None,
        master_session: AsyncSession | None = None,
    ):
        self.org_session = org_session
        self.organisation_id = organisation_id
        self.actor_user_id = actor_user_id
        self.master_session = master_session

    async def close(self) -> None:
        """Clean up any resources."""
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("LeadsAccessService close exception", exc_info=True)

    async def list_leads(
        self,
        *,
        page: int = 1,
        limit: int = 25,
        q: str | None = None,
        status: CRMLeadStatus | None = None,
        owner_id: UUID | None = None,
        source_type: CRMLeadSourceType | None = None,
    ) -> tuple[list[CRMLead], int]:
        """List leads with org-scoped filters and pagination."""
        if page < 1:
            raise LeadsAccessServiceError("Page must be >= 1", status_code=400)

        base = select(CRMLead).where(
            col(CRMLead.organisation_id) == self.organisation_id,
            col(CRMLead.is_deleted).is_(False),
        )

        if q:
            like_expr = f"%{q.lower()}%"
            base = base.where(
                col(CRMLead.full_name).ilike(like_expr)
                | col(CRMLead.email).ilike(like_expr)
                | col(CRMLead.company_name).ilike(like_expr)
                | col(CRMLead.company_domain).ilike(like_expr)
            )
        if status is not None:
            base = base.where(col(CRMLead.status) == status.value)
        if owner_id is not None:
            base = base.where(col(CRMLead.owner_id) == owner_id)
        if source_type is not None:
            base = base.where(col(CRMLead.source_type) == source_type.value)

        count_stmt = select(func.count()).select_from(base.subquery())
        count_res = await self.org_session.execute(count_stmt)
        total_count = count_res.scalar() or 0

        offset = (page - 1) * limit
        stmt = (
            base.order_by(col(CRMLead.captured_at).desc(), col(CRMLead.lead_id).desc())
            .offset(offset)
            .limit(limit)
        )
        rows = (await self.org_session.execute(stmt)).scalars().all()
        return list(rows), total_count

    async def create_lead(self, payload: dict[str, Any]) -> CRMLead:
        """Create a manual/API lead in the current organisation."""
        self._reject_direct_converted_status(payload)
        actor_crm_user_id = await self._resolve_actor_crm_user_id()
        lead = CRMLead(
            **payload,
            organisation_id=self.organisation_id,
            created_by_user_id=actor_crm_user_id,
            updated_by_user_id=actor_crm_user_id,
        )
        self.org_session.add(lead)
        await self.org_session.commit()
        await self.org_session.refresh(lead)
        return lead

    async def get_lead(self, lead_id: UUID) -> tuple[CRMLead, list[CRMLeadParticipant]]:
        """Return a lead and its active participants."""
        lead = await self._get_lead_or_404(lead_id)
        participants_stmt = (
            select(CRMLeadParticipant)
            .where(
                col(CRMLeadParticipant.organisation_id) == self.organisation_id,
                col(CRMLeadParticipant.lead_id) == lead_id,
                col(CRMLeadParticipant.is_deleted).is_(False),
            )
            .order_by(
                col(CRMLeadParticipant.is_primary).desc(),
                col(CRMLeadParticipant.created_at).asc(),
            )
        )
        participants = (await self.org_session.execute(participants_stmt)).scalars().all()
        return lead, list(participants)

    async def update_lead(self, lead_id: UUID, payload: dict[str, Any]) -> CRMLead:
        """Update mutable lead fields and audit status transitions."""
        self._reject_direct_converted_status(payload)
        lead = await self._get_lead_or_404(lead_id)
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

    async def add_participant(
        self,
        lead_id: UUID,
        payload: dict[str, Any],
    ) -> CRMLeadParticipant:
        """Add a contact, company, user, or custom participant to a lead."""
        await self._get_lead_or_404(lead_id)
        if not (payload.get("related_local_id") or payload.get("email")):
            raise LeadsAccessServiceError(
                "Participant requires related_local_id or email",
                status_code=400,
            )

        participant = CRMLeadParticipant(
            **payload,
            lead_id=lead_id,
            organisation_id=self.organisation_id,
        )
        self.org_session.add(participant)
        await self.org_session.commit()
        await self.org_session.refresh(participant)
        return participant

    async def convert_lead(self, lead_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        """Convert a lead into an internal CRM deal and stamp conversion metadata."""
        if self.actor_user_id is None or self.master_session is None:
            raise LeadsAccessServiceError("Lead conversion requires an authenticated user", 401)

        lead, participants = await self.get_lead(lead_id)
        if self._enum_value(lead.status) == CRMLeadStatus.CONVERTED.value or (
            lead.converted_deal_id is not None
        ):
            raise LeadsAccessServiceError("Lead is already converted", status_code=409)

        deal_service = DealWriteService(
            org_session=self.org_session,
            master_session=self.master_session,
            organisation_id=self.organisation_id,
            actor_user_id=self.actor_user_id,
        )
        try:
            deal = await deal_service.create_deal(self._build_deal_payload_from_lead(lead, payload))
        except DealWriteServiceError as exc:
            raise LeadsAccessServiceError(exc.message, exc.status_code) from exc

        copied_count = await self._copy_participants_to_deal(
            lead_participants=participants,
            deal_id=deal.deal_id,
            deal_integration_id=deal.integration_id,
        )

        now = datetime.now(UTC)
        old_status = lead.status
        actor_crm_user_id = await self._resolve_actor_crm_user_id()
        lead.status = CRMLeadStatus.CONVERTED
        lead.converted_deal_id = deal.deal_id
        lead.converted_at = now
        lead.updated_by_user_id = actor_crm_user_id
        lead.properties = {
            **(lead.properties or {}),
            "converted_via": "api",
            "converted_deal_name": deal.name,
        }

        self.org_session.add(
            CRMLeadStatusHistory(
                organisation_id=self.organisation_id,
                lead_id=lead.lead_id,
                from_status=old_status,
                to_status=CRMLeadStatus.CONVERTED,
                actor_user_id=actor_crm_user_id,
                source="api",
                changed_at=now,
                transition_metadata={"converted_deal_id": str(deal.deal_id)},
            )
        )
        self.org_session.add(lead)
        await self.org_session.commit()
        await self.org_session.refresh(lead)

        return {
            "lead": lead,
            "deal_id": deal.deal_id,
            "copied_participant_count": copied_count,
        }

    async def _resolve_actor_crm_user_id(self) -> UUID | None:
        """Return a valid crm_users.user_id for audit FKs, creating it on demand."""
        if self.actor_user_id is None:
            return None
        if self.master_session is None:
            return None

        user_service = InternalCRMUserService(
            org_session=self.org_session,
            master_session=self.master_session,
            organisation_id=self.organisation_id,
            actor_user_id=self.actor_user_id,
        )
        try:
            crm_user = await user_service.ensure_crm_user(self.actor_user_id)
        except InternalCRMUserServiceError as exc:
            raise LeadsAccessServiceError(exc.message, exc.status_code) from exc
        return crm_user.user_id

    async def _get_lead_or_404(self, lead_id: UUID) -> CRMLead:
        stmt = select(CRMLead).where(
            col(CRMLead.lead_id) == lead_id,
            col(CRMLead.organisation_id) == self.organisation_id,
            col(CRMLead.is_deleted).is_(False),
        )
        res = await self.org_session.execute(stmt)
        lead = res.scalars().first()
        if not lead:
            raise LeadsAccessServiceError("Lead not found", status_code=404)
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
        status_value = LeadsAccessService._enum_value(status)
        if status_value == CRMLeadStatus.CONVERTED.value:
            raise LeadsAccessServiceError(
                "Use the lead conversion endpoint to set converted status",
                status_code=422,
                data={"code": "converted_status_not_allowed", "field": "status"},
            )

    @staticmethod
    def _build_deal_payload_from_lead(lead: CRMLead, payload: dict[str, Any]) -> dict[str, Any]:
        deal_payload: dict[str, Any] = {
            "name": payload.get("deal_name") or LeadsAccessService._default_deal_name(lead),
            "amount": payload.get("amount", lead.budget_amount),
            "currency": payload.get("currency", lead.budget_currency),
            "pipeline_id": payload.get("pipeline_id"),
            "stage_id": payload.get("stage_id"),
            # Internal CRM user IDs intentionally match app user IDs, so a lead owner can
            # become the converted deal owner unless the request explicitly overrides it.
            "owner_user_id": payload.get("owner_user_id") or lead.owner_id,
            "expected_close_date": payload.get("expected_close_date"),
            "description": lead.message,
            "deal_type": payload.get("deal_type"),
            "priority": payload.get("priority") or LeadsAccessService._enum_value(lead.priority),
            "lead_source": lead.source_channel
            or lead.source_name
            or LeadsAccessService._enum_value(lead.source_type),
            "primary_contact_name": lead.full_name,
            "primary_contact_email": lead.email,
            "created_via": "lead_conversion",
            "custom_properties": {
                **(payload.get("custom_properties") or {}),
                "converted_from_lead_id": str(lead.lead_id),
            },
        }
        if lead.company_name or lead.company_domain or lead.company_website or lead.industry:
            deal_payload["account"] = {
                "name": lead.company_name,
                "domain": lead.company_domain,
                "website": lead.company_website,
                "industry": lead.industry,
                "country": lead.country,
            }
        return {key: value for key, value in deal_payload.items() if value is not None}

    @staticmethod
    def _default_deal_name(lead: CRMLead) -> str:
        if lead.company_name and lead.full_name:
            return f"{lead.company_name} - {lead.full_name}"
        if lead.company_name:
            return lead.company_name
        if lead.full_name:
            return lead.full_name
        if lead.email:
            return lead.email
        return f"Converted lead {lead.lead_id}"

    async def _copy_participants_to_deal(
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
            if participant.related_local_id is None:
                continue
            participant_type = CRMParticipantType(self._enum_value(participant.participant_type))
            identity_key = self._deal_participant_identity_key(
                participant_type=participant_type,
                related_local_id=participant.related_local_id,
                email=participant.email,
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
                    related_email=self._normalize_email(participant.email),
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
    def _enum_value(value: Any) -> Any:
        return value.value if hasattr(value, "value") else value

    @staticmethod
    def _normalize_email(value: str | None) -> str | None:
        if not value:
            return None
        normalized = value.strip().lower()
        return normalized or None
