"""CRM lead conversion service."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from src.domain.models.organisation_tables.sync.crm.leads import (
    CRMLead,
    CRMLeadStatus,
    CRMLeadStatusHistory,
)
from src.service.crm.deals.contracts import DealWriteServiceError
from src.service.crm.deals.write_service import DealWriteService
from src.service.crm.leads.contracts import LeadServiceError
from src.service.crm.leads.participant_service import LeadParticipantService


class LeadConversionService(LeadParticipantService):
    """Convert CRM leads to deals."""

    async def convert_lead(self, lead_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        """Convert a lead into an internal CRM deal and stamp conversion metadata."""
        if self.actor_user_id is None or self.master_session is None:
            raise LeadServiceError("Lead conversion requires an authenticated user", 401)

        lead, participants = await self.get_lead(lead_id)
        if self._enum_value(lead.status) == CRMLeadStatus.CONVERTED.value or (
            lead.converted_deal_id is not None
        ):
            raise LeadServiceError("Lead is already converted", status_code=409)

        deal_service = DealWriteService(
            org_session=self.org_session,
            master_session=self.master_session,
            organisation_id=self.organisation_id,
            actor_user_id=self.actor_user_id,
        )
        try:
            deal = await deal_service.create_deal(self._build_deal_payload_from_lead(lead, payload))
        except DealWriteServiceError as exc:
            raise LeadServiceError(exc.message, exc.status_code) from exc
        deal.properties = {
            **(deal.properties or {}),
            "converted_from_lead_id": str(lead.lead_id),
        }

        copied_count = await self.copy_participants_to_deal(
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

    @staticmethod
    def _build_deal_payload_from_lead(lead: CRMLead, payload: dict[str, Any]) -> dict[str, Any]:
        deal_payload: dict[str, Any] = {
            "name": payload.get("deal_name") or LeadConversionService._default_deal_name(lead),
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
            "priority": payload.get("priority") or LeadConversionService._enum_value(lead.priority),
            "lead_source": lead.source_channel
            or lead.source_name
            or LeadConversionService._enum_value(lead.source_type),
            "primary_contact_name": lead.full_name,
            "primary_contact_email": lead.email,
            "created_via": "lead_conversion",
        }
        if payload.get("custom_properties"):
            deal_payload["custom_properties"] = payload["custom_properties"]
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
