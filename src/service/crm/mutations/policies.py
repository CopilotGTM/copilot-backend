"""Entity-specific CRM mutation event policies."""

from __future__ import annotations

from typing import Any

from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.service.crm.mutations.json_safe import changed_field


class ContactMutationPolicy:
    """Build contact mutation payloads without exposing every edit as a workflow trigger."""

    CREATE_AUDIT_FIELDS = (
        "email",
        "first_name",
        "last_name",
        "full_name",
        "job_title",
        "phone",
        "linkedin_url",
        "company_id",
        "company_name",
        "lifecycle_stage",
        "owner_id",
        "owner_name",
        "owner_email",
        "profile_enrichment",
        "profile_enrichment_source",
        "profile_enriched_at",
    )

    UPDATE_AUDIT_FIELDS = CREATE_AUDIT_FIELDS + (
        "mobile_phone",
        "website",
        "lead_status",
        "marketing_status",
        "city",
        "state",
        "country",
        "address",
        "timezone",
    )

    @classmethod
    def build_create_changed_fields(cls, contact: CRMContact) -> dict[str, dict[str, Any]]:
        """Return initial contact values that are useful for audit and workflows."""
        changed_fields: dict[str, dict[str, Any]] = {}
        for field_name in cls.CREATE_AUDIT_FIELDS:
            value = getattr(contact, field_name, None)
            if value is not None:
                changed_fields[field_name] = {"old": None, "new": value}
        return changed_fields

    @staticmethod
    def semantic_for_create() -> list[str]:
        """Contact creation is a curated workflow trigger in V1."""
        return ["crm.contact.created"]

    @classmethod
    def changed_field(cls, field_name: str, old: Any, new: Any) -> dict[str, Any] | None:
        """Return an audit change only for whitelisted contact fields."""
        if field_name not in cls.UPDATE_AUDIT_FIELDS:
            return None
        return changed_field(old, new)

    @staticmethod
    def semantic_for_update(changed_fields: dict[str, dict[str, Any]]) -> list[str]:
        """Expose only V1 contact workflow triggers derived from real field changes."""
        semantic_event_types: list[str] = []
        if "email" in changed_fields and changed_fields["email"]["old"] is None:
            semantic_event_types.append("crm.contact.email_added")
        if "company_id" in changed_fields and changed_fields["company_id"]["old"] is None:
            semantic_event_types.append("crm.contact.account_linked")
        return semantic_event_types
