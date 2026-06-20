"""DTO adapter for rendering contact participants as product stakeholders.

Stakeholder reads prefer durable contact profile fields when a CRMContact is
loaded, but fall back to participant snapshots so list views can remain cheap
and resilient when the contact profile is missing or stale.
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
        "job_title": (
            contact.job_title if contact and contact.job_title else participant.job_title
        ),
        "department": (
            contact.properties.get("department")
            if contact and contact.properties.get("department")
            else participant.department
        ),
        "phone_number": (
            contact.phone
            if contact and contact.phone
            else contact.mobile_phone
            if contact and contact.mobile_phone
            else participant.phone_number
        ),
        "linkedin_url": _contact_linkedin_url(contact) or participant.linkedin_url,
        "external_links": _contact_external_links(contact) or participant.external_links,
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
        "stakeholder_research": (
            contact.contact_research
            if contact and contact.contact_research
            else participant.stakeholder_research
        ),
        "last_researched_at": (
            contact.last_researched_at
            if contact and contact.last_researched_at
            else participant.last_researched_at
        ),
        "is_deleted": participant.is_deleted,
        "created_at": participant.created_at,
        "updated_at": participant.updated_at,
    }
