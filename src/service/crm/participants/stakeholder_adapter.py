"""DTO adapter for rendering contact participants as product stakeholders.

Stakeholder reads use linked CRMContact rows for durable person profile and
research data. Participant rows only contribute deal-local participation fields.
"""

from __future__ import annotations

from typing import Any

from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealParticipant


def _contact_external_links(contact: CRMContact | None) -> list[dict[str, Any]] | None:
    if not contact:
        return None
    links = contact.properties.get("external_links")
    return links if isinstance(links, list) else None


def _contact_linkedin_url(contact: CRMContact | None) -> str | None:
    if not contact:
        return None
    linkedin_url = contact.linkedin_url or contact.properties.get("linkedin_url")
    return linkedin_url if isinstance(linkedin_url, str) else None


def participant_to_stakeholder_detail_dict(
    participant: CRMDealParticipant,
    *,
    contact: CRMContact | None = None,
) -> dict[str, Any]:
    """Build the stakeholder response payload from the canonical participant row.

    The API contract intentionally exposes `participant_id`, not a legacy
    stakeholder ID. Contact profile fields win because they are the durable
    person record; participant fields remain deal-local snapshots.
    """
    return {
        "participant_id": participant.participant_id,
        "deal_id": participant.deal_id,
        "contact_id": participant.related_local_id,
        "email": contact.email if contact and contact.email else participant.related_email,
        "name": contact.full_name if contact and contact.full_name else participant.related_name,
        "job_title": contact.job_title if contact else None,
        "relationship_role": participant.relationship_role,
        "department": contact.properties.get("department") if contact else None,
        "phone_number": (
            contact.phone
            if contact and contact.phone
            else contact.mobile_phone
            if contact and contact.mobile_phone
            else None
        ),
        "linkedin_url": _contact_linkedin_url(contact),
        "external_links": _contact_external_links(contact),
        "stance": participant.stance,
        "decision_power": participant.decision_power,
        "motivation": participant.motivation,
        "pain_points": participant.pain_points,
        "keywords": participant.keywords,
        "last_contact_at": participant.last_contact_at,
        "temperature": participant.temperature,
        "source": participant.participant_source,
        "source_reference_id": participant.source_reference_id,
        "approval_status": participant.approval_status,
        "stakeholder_research": contact.contact_research if contact else None,
        "last_researched_at": contact.last_researched_at if contact else None,
        "is_deleted": participant.is_deleted,
        "created_at": participant.created_at,
        "updated_at": participant.updated_at,
    }
