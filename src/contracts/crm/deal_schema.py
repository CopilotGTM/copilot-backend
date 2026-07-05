"""Shared CRM deal schema DTOs for API, service, agent, and worker layers.

These models intentionally live outside ``src.api`` because light-worker
runtime images do not package the API layer.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class DealCustomColumnDefinition(BaseModel):
    """Custom column metadata for frontend control rendering.

    Returned inline with deal detail to avoid an additional fetch for custom
    field schema before rendering edit controls. This keeps frontend rendering
    deterministic even when custom values are null.
    """

    key: str = Field(..., description="Stable custom field key")
    label: str = Field(..., description="Display label for the field")
    data_type: str = Field(
        ...,
        description=(
            "Custom field type used to choose UI controls "
            "(text/number/integer/boolean/date/datetime/currency/etc.)"
        ),
    )
    required: bool = Field(False, description="Whether value is required")
    options: dict[str, Any] = Field(default_factory=dict, description="Type-specific UI options")
    validation: dict[str, Any] = Field(
        default_factory=dict, description="Validation metadata for frontend hints"
    )
    currency_code: str | None = Field(
        None,
        description=(
            "Currency code context for currency custom fields. "
            "Defaults to the deal currency when field-level override is absent."
        ),
    )
    currency_symbol: str | None = Field(
        None, description="Currency symbol resolved from currency_code (for example $, ₹)."
    )
    editable: bool = Field(
        False,
        description=(
            "True when this custom field can be edited through custom_columns "
            "on internal CRM deals."
        ),
    )
    patch_field: str | None = Field(
        None,
        description="PATCH path for custom field edits, for example custom_columns.custom_key.",
    )
    edit_control: str | None = Field(None, description="Frontend edit control hint")
    edit_policy: str = Field(
        "read_only",
        description="custom_columns for editable custom fields, formula/read_only otherwise.",
    )
    locked_reason: str | None = Field(None, description="Reason shown when editing is locked")


class DealDetailFieldDefinition(BaseModel):
    """Frontend metadata for rendering one predefined deal detail field."""

    key: str = Field(..., description="Dotted or top-level path in DealDetail")
    label: str = Field(..., description="Display label")
    data_type: str = Field(..., description="Frontend data type")
    order: int = Field(..., description="Sort order for Get Details rendering")
    section: str = Field(..., description="Logical detail section")
    filter_control: str = Field(..., description="Frontend filter/control hint")
    editable: bool = Field(
        False,
        description=(
            "True when PATCH /deals/{deal_id} accepts it for internal CRM deals. "
            "External CRM deals remain read-only through is_internal=false."
        ),
    )
    patch_field: str | None = Field(None, description="PATCH field for inline edits")
    edit_control: str | None = Field(None, description="Inline edit control hint")
    edit_policy: str = Field("read_only", description="Why or how this field can be edited")
    locked_reason: str | None = Field(None, description="Reason shown when editing is locked")


class CRMFieldSchemaItem(BaseModel):
    """One field definition in a CRM entity schema response."""

    field_id: UUID | None = Field(None, description="Custom field definition ID, if applicable.")
    entity: str = Field(..., description="Entity scope for the field, for example deal.")
    key: str = Field(..., description="Stable API/storage key for the field.")
    label: str = Field(..., description="Display label for UI and mapping surfaces.")
    type: str = Field(..., description="Canonical value type.")
    order: int = Field(..., description="Stable display order within this schema.")
    section: str | None = Field(None, description="Logical UI section for standard fields.")
    source: str = Field(..., description="standard, internal, or provider.")
    ownership: str = Field(..., description="Field owner: system, internal, or provider name.")
    integration_id: UUID | None = Field(None, description="Provider integration ID, if any.")
    provider_name: str | None = Field(None, description="Provider name for mirrored fields.")
    provider_field_key: str | None = Field(None, description="Provider-native field key.")
    required: bool = Field(False, description="Whether internal writes must provide a value.")
    read_only: bool = Field(False, description="Whether direct field editing is blocked.")
    nullable: bool = Field(True, description="Whether an empty value is acceptable.")
    editable: bool = Field(
        False, description="Whether direct internal CRM writes can edit this field."
    )
    sortable: bool = Field(
        False, description="Whether list/reporting surfaces can sort by this field."
    )
    filterable: bool = Field(
        False, description="Whether list/reporting surfaces can filter by this field."
    )
    searchable: bool = Field(False, description="Whether keyword search can match this field.")
    input_control: str | None = Field(None, description="Suggested frontend control.")
    options: dict[str, Any] = Field(default_factory=dict, description="Type-specific options.")
    validation: dict[str, Any] = Field(
        default_factory=dict,
        description="Type-specific frontend/backend validation hints.",
    )
    lifecycle_state: str = Field("active", description="Field lifecycle state.")
    write_path: str | None = Field(None, description="Request path accepted by write APIs.")
    write_policy: str = Field("read_only", description="Write semantic for this field.")
    read_only_reason: str | None = Field(
        None, description="Why the field cannot be directly edited."
    )
    internal_write: bool = Field(
        False, description="Whether internal CRM writes accept this field."
    )
    internal_write_validation: dict[str, bool] = Field(
        default_factory=dict,
        description="Backend validation gates applied to internal writes.",
    )


class CRMEntitySchemaResponse(BaseModel):
    """Ordered field schema for one CRM entity."""

    entity: str
    fields: list[CRMFieldSchemaItem]


_DEAL_DETAIL_FIELD_ROWS: tuple[tuple[str, str, str, int, str, str], ...] = (
    ("name", "Deal name", "string", 1, "core", "text"),
    ("deal_status", "Status", "enum", 2, "core", "select"),
    ("stage_name", "Stage", "string", 3, "core", "select"),
    ("pipeline_name", "Pipeline", "string", 4, "core", "select"),
    ("amount", "Amount", "number", 5, "value", "currency"),
    ("currency", "Currency", "string", 6, "value", "select"),
    ("predicted_amount", "Predicted amount", "number", 7, "value", "currency"),
    ("probability", "Probability", "number", 8, "value", "percent"),
    ("forecast_category", "Forecast category", "string", 9, "value", "select"),
    ("owner_details", "Owner", "array", 12, "ownership", "people"),
    ("deal_team", "Internal team", "array", 13, "ownership", "people"),
    ("company_name", "Account", "string", 14, "account", "account"),
    ("company_domain", "Account domain", "string", 15, "account", "domain"),
    ("company_industry", "Industry", "string", 16, "account", "select"),
    ("company_website", "Website", "url", 17, "account", "link"),
    ("company_linkedin_url", "Company LinkedIn", "url", 18, "account", "link"),
    ("company_country", "Account country", "string", 19, "account", "select"),
    ("primary_contact_name", "Primary contact", "string", 20, "contact", "contact"),
    ("primary_contact_email", "Primary contact email", "email", 21, "contact", "email"),
    ("source_created_at", "CRM created date", "datetime", 22, "timeline", "date_range"),
    ("close_date", "Close date", "datetime", 23, "timeline", "date_range"),
    ("expected_close_date", "Expected close date", "datetime", 24, "timeline", "date_range"),
    ("closed_won_at", "Won date", "datetime", 25, "timeline", "date_range"),
    ("closed_lost_at", "Lost date", "datetime", 26, "timeline", "date_range"),
    ("deal_age_days", "Deal age", "integer", 27, "timeline", "number"),
    ("last_activity_at", "Last activity", "datetime", 28, "timeline", "date_range"),
    ("stage_entered_at", "Stage entered", "datetime", 29, "timeline", "date_range"),
    ("stage_updated_at", "Stage updated", "datetime", 30, "timeline", "date_range"),
    ("deal_type", "Deal type", "string", 31, "process", "select"),
    ("priority", "Priority", "string", 32, "process", "select"),
    ("lead_source", "Lead source", "string", 33, "process", "select"),
    ("closed_reason", "Closed reason", "string", 34, "process", "long_text"),
    ("crm_url", "CRM link", "url", 35, "process", "link"),
    ("description", "Description", "string", 36, "process", "long_text"),
)

_INLINE_EDITABLE_DEAL_FIELDS = {
    "name",
    "amount",
    "currency",
    "probability",
    "forecast_category",
    "source_created_at",
    "expected_close_date",
    "deal_type",
    "priority",
    "lead_source",
    "description",
}
_DEAL_EDIT_CONTROLS = {
    "name": "text",
    "amount": "currency",
    "currency": "select",
    "probability": "percent",
    "forecast_category": "select",
    "source_created_at": "datetime",
    "expected_close_date": "datetime",
    "deal_type": "select",
    "priority": "select",
    "lead_source": "select",
    "description": "textarea",
}
_DEAL_STAGE_FIELDS = {"deal_status", "stage_name", "pipeline_name"}
_DEAL_ACCOUNT_FIELDS = {
    "company_name",
    "company_domain",
    "company_industry",
    "company_website",
    "company_linkedin_url",
    "company_country",
}
_DEAL_PRIMARY_CONTACT_FIELDS = {"primary_contact_name", "primary_contact_email"}
_DEAL_LIFECYCLE_FIELDS = {"close_date", "closed_won_at", "closed_lost_at", "closed_reason"}
_DEAL_SYSTEM_FIELDS = {
    "predicted_amount",
    "deal_age_days",
    "last_activity_at",
    "stage_entered_at",
    "stage_updated_at",
    "crm_url",
}


def _deal_edit_metadata(key: str) -> tuple[bool, str | None, str | None, str, str | None]:
    if key in _INLINE_EDITABLE_DEAL_FIELDS:
        return True, key, _DEAL_EDIT_CONTROLS.get(key), "always", None
    if key in _DEAL_STAGE_FIELDS:
        return False, None, None, "stage_action", "Use the deal stage/lifecycle action."
    if key == "owner_details":
        return False, None, None, "owner_assignment", "Use the deal owner assignment action."
    if key == "deal_team":
        return False, None, None, "participants_action", "Use deal participant actions."
    if key in _DEAL_ACCOUNT_FIELDS:
        return False, None, None, "account_action", "Use the deal account update action."
    if key in _DEAL_PRIMARY_CONTACT_FIELDS:
        return (
            False,
            None,
            None,
            "primary_contact_action",
            "Use the deal primary-contact update action.",
        )
    if key in _DEAL_LIFECYCLE_FIELDS:
        return False, None, None, "lifecycle_action", "Use close or reopen actions."
    if key in _DEAL_SYSTEM_FIELDS:
        return False, None, None, "system", "System-managed value."
    return False, None, None, "read_only", "Read-only deal detail."


def deal_detail_field_definitions() -> list[DealDetailFieldDefinition]:
    """Return ordered predefined deal detail metadata for the Get Details panel."""
    definitions: list[DealDetailFieldDefinition] = []
    for key, label, data_type, order, section, filter_control in _DEAL_DETAIL_FIELD_ROWS:
        editable, patch_field, edit_control, edit_policy, locked_reason = _deal_edit_metadata(key)
        definitions.append(
            DealDetailFieldDefinition(
                key=key,
                label=label,
                data_type=data_type,
                order=order,
                section=section,
                filter_control=filter_control,
                editable=editable,
                patch_field=patch_field,
                edit_control=edit_control,
                edit_policy=edit_policy,
                locked_reason=locked_reason,
            )
        )
    return definitions
