"""Presentation helpers for CRM lead API responses."""

from __future__ import annotations

import math
from typing import Any

from src.api.schemas.crm_lead_models import (
    LeadConversionResponse,
    LeadDetail,
    LeadListItem,
    LeadParticipantItem,
    LeadsPage,
)
from src.domain.models.organisation_tables.sync.crm.leads import CRMLead, CRMLeadParticipant


def present_lead_list_item(lead: CRMLead) -> LeadListItem:
    return LeadListItem.model_validate(lead)


def present_lead_participant_item(participant: CRMLeadParticipant) -> LeadParticipantItem:
    return LeadParticipantItem.model_validate(participant)


def present_lead_detail(lead: CRMLead, participants: list[CRMLeadParticipant]) -> LeadDetail:
    data = LeadDetail.model_validate(lead)
    data.participants = [present_lead_participant_item(participant) for participant in participants]
    return data


def present_lead_conversion_response(result: dict[str, Any]) -> LeadConversionResponse:
    lead = result["lead"]
    return LeadConversionResponse(
        lead_id=lead.lead_id,
        status=lead.status,
        converted_deal_id=lead.converted_deal_id,
        converted_at=lead.converted_at,
        copied_participant_count=result.get("copied_participant_count", 0),
    )


def present_leads_page(
    *,
    leads: list[CRMLead],
    page: int,
    limit: int,
    total_count: int,
) -> LeadsPage:
    total_pages = max(1, math.ceil(total_count / limit)) if total_count > 0 else 1
    return LeadsPage(
        items=[present_lead_list_item(row) for row in leads],
        page=page,
        limit=limit,
        total_count=total_count,
        total_pages=total_pages,
    )
