"""Backend-owned fields available on CRM collection responses."""

from __future__ import annotations

from src.api.schemas.crm_list_preference_models import CRMColumnEntity, CRMListColumn

ColumnRow = tuple[str, str, str, str, bool]

_STANDARD_COLUMNS: dict[CRMColumnEntity, tuple[ColumnRow, ...]] = {
    "deals": (
        ("name", "Deal name", "text", "name", True),
        ("company_name", "Account", "text", "company_name", False),
        ("stage", "Stage", "text", "stage_label", True),
        ("deal_status", "Status", "text", "deal_status", False),
        ("pipeline_name", "Pipeline", "text", "pipeline_name", False),
        ("amount", "Amount", "currency", "amount", True),
        ("predicted_amount", "Predicted amount", "currency", "predicted_amount", False),
        ("probability", "Probability", "percentage", "probability", False),
        ("currency", "Currency", "text", "currency", False),
        ("expected_close_date", "Expected close date", "datetime", "expected_close_date", True),
        ("close_date", "Close date", "datetime", "close_date", False),
        ("owner", "Owner", "person", "owner_details", True),
        ("company_domain", "Account domain", "domain", "company_domain", False),
        ("primary_contact_name", "Primary contact", "text", "primary_contact_name", False),
        ("primary_contact_email", "Primary contact email", "email", "primary_contact_email", False),
        ("last_activity_at", "Last activity", "datetime", "last_activity_at", True),
        ("created_at", "Created", "datetime", "created_at", False),
        ("updated_at", "Updated", "datetime", "updated_at", False),
    ),
    "contacts": (
        ("full_name", "Contact name", "text", "full_name", True),
        ("job_title", "Job title", "text", "job_title", False),
        ("company.name", "Account", "text", "company.name", False),
        ("email", "Email", "email", "email", False),
        ("additional_emails", "Additional emails", "list", "additional_emails", False),
        ("phone", "Phone", "phone", "phone", False),
        ("mobile_phone", "Mobile phone", "phone", "mobile_phone", False),
        ("linkedin_url", "LinkedIn", "url", "linkedin_url", False),
        ("company.domain", "Account domain", "domain", "company.domain", False),
        ("owner.name", "Owner", "person", "owner", True),
        ("owner.email", "Owner email", "email", "owner.email", False),
        ("deal_count", "Deals", "integer", "deal_count", False),
        ("last_contacted_at", "Last contacted", "datetime", "last_contacted_at", True),
        ("last_activity_at", "Last activity", "datetime", "last_activity_at", True),
        ("source", "Source", "text", "source", False),
        ("is_profile_enriched", "Profile enriched", "boolean", "is_profile_enriched", False),
        ("profile_enriched_at", "Profile enriched at", "datetime", "profile_enriched_at", False),
        ("created_at", "Created", "datetime", "created_at", False),
        ("updated_at", "Updated", "datetime", "updated_at", False),
    ),
    "accounts": (
        ("name", "Account name", "text", "name", True),
        ("domain", "Domain", "domain", "domain", False),
        ("website", "Website", "url", "website", False),
        ("industry", "Industry", "text", "industry", False),
        ("lifecycle_stage", "Lifecycle stage", "text", "lifecycle_stage", True),
        ("owner.name", "Owner", "person", "owner", True),
        ("owner.email", "Owner email", "email", "owner.email", False),
        ("contact_count", "Contacts", "integer", "contact_count", False),
        ("deal_count", "Deals", "integer", "deal_count", False),
        ("pipeline_value", "Pipeline value", "currency", "pipeline_value", False),
        ("number_of_employees", "Employees", "integer", "number_of_employees", True),
        ("annual_revenue", "Annual revenue", "currency", "annual_revenue", True),
        ("phone", "Phone", "phone", "phone", False),
        ("country", "Country", "text", "country", False),
        ("linkedin_url", "LinkedIn", "url", "linkedin_url", False),
        ("is_active", "Active", "boolean", "is_active", False),
        ("created_at", "Created", "datetime", "created_at", True),
        ("updated_at", "Updated", "datetime", "updated_at", False),
    ),
}

DEFAULT_COLUMNS: dict[CRMColumnEntity, tuple[str, ...]] = {
    "contacts": (
        "full_name",
        "job_title",
        "company.name",
        "email",
        "phone",
        "linkedin_url",
        "owner.name",
        "deal_count",
        "last_contacted_at",
        "last_activity_at",
    ),
    "accounts": (
        "name",
        "domain",
        "industry",
        "lifecycle_stage",
        "owner.name",
        "contact_count",
        "deal_count",
        "pipeline_value",
        "number_of_employees",
        "annual_revenue",
        "country",
        "linkedin_url",
        "created_at",
    ),
    "deals": (
        "name",
        "company_name",
        "stage",
        "amount",
        "expected_close_date",
        "owner",
        "primary_contact_name",
        "last_activity_at",
    ),
}

REQUIRED_COLUMN: dict[CRMColumnEntity, str] = {
    "contacts": "full_name",
    "accounts": "name",
    "deals": "name",
}


def standard_columns(entity: CRMColumnEntity) -> list[CRMListColumn]:
    """Return immutable standard metadata as fresh response models."""
    defaults = set(DEFAULT_COLUMNS[entity])
    required = REQUIRED_COLUMN[entity]
    return [
        CRMListColumn(
            id=column_id,
            label=label,
            data_type=data_type,
            source="standard",
            value_path=value_path,
            visible=False,
            default_visible=column_id in defaults,
            position=index,
            sortable=sortable,
            hideable=column_id != required,
        )
        for index, (column_id, label, data_type, value_path, sortable) in enumerate(
            _STANDARD_COLUMNS[entity]
        )
    ]
