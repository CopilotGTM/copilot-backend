"""Entity-specific CRM mutation event policies."""

from __future__ import annotations

from typing import Any

from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.service.crm.mutations.json_safe import changed_field


class DealMutationPolicy:
    """Build deal mutation payloads with curated workflow-trigger semantics."""

    CREATE_AUDIT_FIELDS = (
        "deal_id",
        "name",
        "amount",
        "currency",
        "pipeline_id",
        "pipeline_name",
        "stage_id",
        "stage_name",
        "deal_status",
        "probability",
        "forecast_category",
        "owner_id",
        "owner_name",
        "owner_email",
        "company_id",
        "company_name",
        "company_domain",
        "primary_contact_id",
        "primary_contact_name",
        "primary_contact_email",
        "close_date",
        "expected_close_date",
        "closed_won_at",
        "closed_lost_at",
        "closed_reason",
        "crm_url",
        "description",
        "deal_type",
        "priority",
        "lead_source",
    )

    PROPERTY_METADATA_FIELDS = {
        "created_via",
        "updated_via",
        "deleted_via",
        "closed_via",
        "reopened_via",
        "closed_by_user_id",
        "reopened_by_user_id",
        "is_internal_crm",
    }

    @classmethod
    def build_create_changed_fields(cls, deal: CRMDeal) -> dict[str, dict[str, Any]]:
        """Return initial deal values that are useful for audit and workflows."""
        changed_fields: dict[str, dict[str, Any]] = {}
        for field_name in cls.CREATE_AUDIT_FIELDS:
            value = getattr(deal, field_name, None)
            if value is not None:
                changed_fields[field_name] = {"old": None, "new": value}

        for key, value in (deal.properties or {}).items():
            if key not in cls.PROPERTY_METADATA_FIELDS:
                changed_fields[f"custom_properties.{key}"] = {"old": None, "new": value}
        return changed_fields

    @staticmethod
    def semantic_for_event(
        event_type: str,
        changed_fields: dict[str, dict[str, Any]],
    ) -> list[str]:
        """Derive workflow-matchable semantics from deal action events."""
        if not changed_fields:
            return []

        if event_type == "crm.deal.created":
            return ["crm.deal.created"]
        if event_type == "crm.deal.stage_changed":
            return ["crm.deal.stage_changed"]
        if event_type == "crm.deal.owner_changed":
            return ["crm.deal.owner_changed"]
        if event_type == "crm.deal.account_changed":
            return ["crm.deal.account_changed"]
        if event_type == "crm.deal.primary_contact_changed":
            return ["crm.deal.primary_contact_changed"]
        if event_type == "crm.deal.deleted":
            return ["crm.deal.deleted"]

        if event_type in {"crm.deal.closed", "crm.deal.reopened"}:
            semantics = [event_type]
            if "stage_id" in changed_fields:
                semantics.append("crm.deal.stage_changed")
            return semantics

        if event_type == "crm.deal.updated":
            update_semantics: list[str] = []
            if "stage_id" in changed_fields:
                update_semantics.append("crm.deal.stage_changed")
            if "amount" in changed_fields:
                update_semantics.append("crm.deal.amount_changed")
            if "owner_id" in changed_fields:
                update_semantics.append("crm.deal.owner_changed")
            return update_semantics

        return []


class AccountMutationPolicy:
    """Build account mutation payloads with curated workflow-trigger semantics."""

    CREATE_AUDIT_FIELDS = (
        "name",
        "domain",
        "website",
        "description",
        "industry",
        "company_type",
        "lifecycle_stage",
        "owner_id",
        "owner_name",
        "owner_email",
        "number_of_employees",
        "annual_revenue",
        "phone",
        "city",
        "state",
        "country",
        "address",
        "postal_code",
        "timezone",
        "linkedin_url",
    )

    UPDATE_AUDIT_FIELDS = CREATE_AUDIT_FIELDS

    @classmethod
    def build_create_changed_fields(cls, account: CRMCompany) -> dict[str, dict[str, Any]]:
        """Return initial account values that are useful for audit and workflows."""
        changed_fields: dict[str, dict[str, Any]] = {}
        for field_name in cls.CREATE_AUDIT_FIELDS:
            value = getattr(account, field_name, None)
            if value is not None:
                changed_fields[field_name] = {"old": None, "new": value}
        return changed_fields

    @staticmethod
    def semantic_for_create() -> list[str]:
        """Account creation is a curated workflow trigger in V1."""
        return ["crm.account.created"]

    @classmethod
    def changed_field(cls, field_name: str, old: Any, new: Any) -> dict[str, Any] | None:
        """Return an audit change only for whitelisted account fields."""
        if field_name not in cls.UPDATE_AUDIT_FIELDS:
            return None
        return changed_field(old, new)

    @staticmethod
    def semantic_for_update(changed_fields: dict[str, dict[str, Any]]) -> list[str]:
        """Expose only V1 account workflow triggers derived from real field changes."""
        semantic_event_types: list[str] = []
        if "domain" in changed_fields:
            semantic_event_types.append("crm.account.domain_changed")
        if "owner_id" in changed_fields:
            semantic_event_types.append("crm.account.owner_changed")
        if "lifecycle_stage" in changed_fields:
            semantic_event_types.append("crm.account.lifecycle_stage_changed")
        return semantic_event_types


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
