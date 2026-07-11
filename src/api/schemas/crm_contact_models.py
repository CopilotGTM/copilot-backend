"""CRM contact API schemas and field metadata.

Contact list/detail responses combine canonical CRMContact fields with compact
account, owner, and deal involvement snapshots. The detail field metadata tells
clients which fields are directly editable and which must be changed on related
records or by enrichment workflows.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from src.service.crm.utils.account import PERSON_LINKEDIN_URL_ERROR, is_person_linkedin_url


class ContactDealInvolvement(BaseModel):
    """A deal this contact participates in as a stakeholder."""

    deal_id: UUID
    deal_name: str | None
    deal_status: str | None
    stage_name: str | None
    last_activity_at: datetime | None
    participant_id: UUID
    relationship_role: str | None


class ContactListCompany(BaseModel):
    """Account context returned by the contacts list endpoint."""

    id: UUID | None
    name: str | None
    domain: str | None


class ContactListOwner(BaseModel):
    """Owner context returned by the contacts list endpoint."""

    id: UUID | None
    name: str | None
    email: str | None


class ContactDetailCompany(ContactListCompany):
    """Account context returned by the contact detail endpoint."""

    website: str | None
    industry: str | None
    linkedin_url: str | None
    country: str | None


class ContactResearchSummary(BaseModel):
    """Contact-level research displayed in the main overview."""

    summary: str | None
    last_researched_at: datetime | None


class ContactProfileEnrichment(BaseModel):
    """Structured enrichment metadata for a contact profile."""

    is_enriched: bool
    source: str | None
    enriched_at: datetime | None
    data: dict[str, Any] = Field(default_factory=dict)


class ContactDetailFieldDefinition(BaseModel):
    """Frontend display metadata for predefined contact detail fields."""

    key: str
    label: str
    data_type: str
    order: int
    section: str
    filter_control: str
    editable: bool
    patch_field: str | None
    edit_control: str | None
    edit_policy: str
    locked_reason: str | None = None


_CONTACT_DETAIL_FIELD_ROWS = (
    ("full_name", "Name", "string", 1, "contact", "text"),
    ("job_title", "Job title", "string", 2, "contact", "text"),
    ("email", "Email", "email", 3, "contact", "email"),
    ("additional_emails", "Additional emails", "array<email>", 4, "contact", "chips"),
    ("phone", "Phone", "phone", 5, "contact", "phone"),
    ("mobile_phone", "Mobile", "phone", 6, "contact", "phone"),
    ("linkedin_url", "LinkedIn", "url", 7, "contact", "link"),
    ("website", "Website", "url", 8, "contact", "link"),
    ("lifecycle_stage", "Lifecycle stage", "string", 9, "contact", "select"),
    ("lead_status", "Lead status", "string", 10, "contact", "select"),
    ("marketing_status", "Marketing status", "string", 11, "contact", "select"),
    ("source", "Source", "string", 12, "contact", "select"),
    ("company.id", "Account ID", "uuid", 13, "account", "link"),
    ("company.name", "Account", "string", 14, "account", "account_link"),
    ("company.domain", "Account domain", "string", 15, "account", "text"),
    ("company.website", "Account website", "url", 16, "account", "link"),
    ("company.industry", "Account industry", "string", 17, "account", "select"),
    ("company.linkedin_url", "Account LinkedIn", "url", 18, "account", "link"),
    ("company.country", "Account country", "string", 19, "account", "select"),
    ("owner.id", "Owner ID", "uuid", 20, "owner", "link"),
    ("owner.name", "Owner", "string", 21, "owner", "people"),
    ("owner.email", "Owner email", "email", 22, "owner", "email"),
    ("address", "Address", "string", 23, "location", "text"),
    ("city", "City", "string", 24, "location", "select"),
    ("state", "State", "string", 25, "location", "select"),
    ("postal_code", "Postal code", "string", 26, "location", "text"),
    ("country", "Country", "string", 27, "location", "select"),
    ("timezone", "Timezone", "string", 28, "location", "select"),
    ("last_contacted_at", "Last contacted", "datetime", 29, "activity", "date_range"),
    ("last_activity_at", "Last activity", "datetime", 30, "activity", "date_range"),
    ("source_created_at", "CRM created date", "datetime", 31, "activity", "date_range"),
    ("source_modified_at", "CRM modified date", "datetime", 32, "activity", "date_range"),
)


_ALWAYS_EDITABLE_CONTACT_FIELDS = {
    "full_name": ("full_name", "text"),
    "job_title": ("job_title", "text"),
    "phone": ("phone", "phone"),
    "mobile_phone": ("mobile_phone", "phone"),
}

_RELATED_ENTITY_FIELDS = {
    "company.name",
    "company.domain",
    "company.website",
    "company.industry",
    "company.linkedin_url",
    "company.country",
    "deal_involvement",
}


def _contact_field_edit_metadata(key: str, contact: Any | None) -> dict[str, Any]:
    if key in _ALWAYS_EDITABLE_CONTACT_FIELDS:
        patch_field, edit_control = _ALWAYS_EDITABLE_CONTACT_FIELDS[key]
        return {
            "editable": True,
            "patch_field": patch_field,
            "edit_control": edit_control,
            "edit_policy": "always",
            "locked_reason": None,
        }

    if key == "email":
        email = getattr(contact, "email", None) if contact is not None else None
        return {
            "editable": email is None,
            "patch_field": "email",
            "edit_control": "email",
            "edit_policy": "set_if_empty",
            "locked_reason": None
            if email is None
            else "Existing email is used for dedupe and activity matching.",
        }

    if key == "linkedin_url":
        linkedin_url = getattr(contact, "linkedin_url", None) if contact is not None else None
        return {
            "editable": linkedin_url is None,
            "patch_field": "linkedin_url",
            "edit_control": "url",
            "edit_policy": "set_if_empty",
            "locked_reason": None
            if linkedin_url is None
            else "Existing LinkedIn URL is treated as verified profile identity.",
        }

    if key == "company.id":
        email = getattr(contact, "email", None) if contact is not None else None
        company_id = getattr(contact, "company_id", None) if contact is not None else None
        return {
            "editable": company_id is None and email is not None,
            "patch_field": "company_id",
            "edit_control": "account_picker",
            "edit_policy": "set_once",
            "locked_reason": None
            if company_id is None and email is not None
            else (
                "Add an email before linking an account."
                if company_id is None
                else "Account link is set once and cannot be changed from contact detail."
            ),
        }

    if key in _RELATED_ENTITY_FIELDS:
        return {
            "editable": False,
            "patch_field": None,
            "edit_control": None,
            "edit_policy": "related_entity",
            "locked_reason": "Edit this on the related record.",
        }

    return {
        "editable": False,
        "patch_field": None,
        "edit_control": None,
        "edit_policy": "read_only",
        "locked_reason": "Read-only field.",
    }


def contact_detail_field_definitions(
    contact: Any | None = None,
) -> list[ContactDetailFieldDefinition]:
    """Return ordered metadata for predefined contact detail fields.

    Email and LinkedIn editability depends on the current contact because those
    identifiers are set-once values used for dedupe and activity matching.
    """
    return [
        ContactDetailFieldDefinition(
            key=key,
            label=label,
            data_type=data_type,
            order=order,
            section=section,
            filter_control=filter_control,
            **_contact_field_edit_metadata(key, contact),
        )
        for key, label, data_type, order, section, filter_control in _CONTACT_DETAIL_FIELD_ROWS
    ]


class ContactListItem(BaseModel):
    """Single contact in a list response."""

    contact_id: UUID
    full_name: str | None
    email: str | None
    additional_emails: list[str] = Field(default_factory=list)
    job_title: str | None
    phone: str | None
    mobile_phone: str | None
    linkedin_url: str | None
    company: ContactListCompany | None
    owner: ContactListOwner | None
    deal_count: int = Field(0)
    last_contacted_at: datetime | None
    last_activity_at: datetime | None
    source: str = Field("crm_sync")
    is_profile_enriched: bool = Field(False)
    profile_enriched_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ContactMutationItem(BaseModel):
    """Legacy contact shape returned by create and update endpoints."""

    contact_id: UUID
    first_name: str | None
    last_name: str | None
    full_name: str | None
    email: str | None
    job_title: str | None
    phone: str | None
    company_name: str | None
    company_id: UUID | None
    company_domain: str | None
    lifecycle_stage: str | None
    owner_name: str | None
    deal_count: int = Field(0)
    source: str = Field("crm_sync")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ContactDeleteImpact(BaseModel):
    """Impact summary returned after archiving and detaching a contact."""

    cleared_deal_primary_contacts: int = 0
    archived_deal_participants: int = 0
    cleared_lead_contacts: int = 0
    archived_lead_participants: int = 0
    detached_integration_participants: int = 0
    removed_contact_activity_links: int = 0
    cleared_activity_contact_refs: int = 0
    archived_contact_only_activities: int = 0
    cancelled_contact_research_jobs: int = 0
    import_rows_retained: int = 0


class ContactDeleteResult(BaseModel):
    """Archived contact response with explicit detach impact counts."""

    action: Literal["archived_and_detached"] = "archived_and_detached"
    contact: ContactMutationItem
    impact: ContactDeleteImpact


class ContactDeleteBlockedData(BaseModel):
    """Guidance payload for contacts that cannot be archived by this API."""

    code: str
    blocked_reason: str
    suggested_actions: list[str] = Field(default_factory=list)


class ContactDetail(BaseModel):
    """Page-ready contact detail including research and compact deal involvement."""

    detail_field_definitions: list[ContactDetailFieldDefinition] = Field(
        default_factory=contact_detail_field_definitions,
        description="Ordered frontend metadata for the predefined contact Get Details fields.",
    )
    contact_id: UUID
    first_name: str | None
    last_name: str | None
    full_name: str | None
    job_title: str | None
    email: str | None
    additional_emails: list[str] = Field(default_factory=list)
    phone: str | None
    mobile_phone: str | None
    linkedin_url: str | None
    website: str | None
    source: str = Field("crm_sync")
    lifecycle_stage: str | None
    lead_status: str | None
    marketing_status: str | None
    company: ContactDetailCompany | None
    owner: ContactListOwner | None
    address: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    country: str | None
    timezone: str | None
    last_contacted_at: datetime | None
    last_activity_at: datetime | None
    created_at: datetime
    updated_at: datetime
    source_created_at: datetime | None
    source_modified_at: datetime | None
    synced_at: datetime
    research_summary: ContactResearchSummary | None
    profile_enrichment: ContactProfileEnrichment
    deal_count: int = Field(0)
    deal_involvement: list[ContactDealInvolvement] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class ContactsPage(BaseModel):
    """Paginated list of contacts."""

    items: list[ContactListItem]
    page: int
    limit: int
    total_count: int
    total_pages: int


class ContactSearchItem(BaseModel):
    """Contact search suggestion shown in contact pickers."""

    contact_id: UUID
    full_name: str | None
    email: str | None
    job_title: str | None
    company: ContactListCompany | None
    match_type: str


class ContactSearchPage(BaseModel):
    """Cursor page of contact search suggestions."""

    items: list[ContactSearchItem]
    next_cursor: str | None = None
    has_more: bool = False


class CreateContactRequest(BaseModel):
    """Request to create a new contact."""

    model_config = ConfigDict(extra="forbid")

    email: str | None = Field(None, description="Email address when already known")
    name: str | None = Field(None, description="Full name when already known")
    job_title: str | None = Field(None, description="Job title when already known")
    linkedin_url: str | None = Field(None, description="LinkedIn profile URL when already known")

    @field_validator("linkedin_url")
    @classmethod
    def validate_linkedin_url(cls, value: str | None) -> str | None:
        """Accept only person profile LinkedIn URLs for contact records."""
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if not is_person_linkedin_url(cleaned):
            raise ValueError(PERSON_LINKEDIN_URL_ERROR)
        return cleaned

    @model_validator(mode="after")
    def validate_identity(self) -> CreateContactRequest:
        """Require an email, LinkedIn URL, or placeholder name before contact creation."""
        if self.email and self.email.strip():
            return self
        if self.linkedin_url and self.linkedin_url.strip():
            return self
        if self.name and self.name.strip():
            return self
        raise ValueError("email, LinkedIn URL, or name is required")


class UpdateContactRequest(BaseModel):
    """Request to update an existing contact (all fields optional).

    email can only be set if the contact currently has no email.
    Changing an existing email is not allowed.
    """

    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    job_title: str | None = None
    phone: str | None = None
    mobile_phone: str | None = None
    linkedin_url: str | None = None
    company_name: str | None = None
    company_id: UUID | None = None

    @field_validator("linkedin_url")
    @classmethod
    def validate_linkedin_url(cls, value: str | None) -> str | None:
        """Accept only person profile LinkedIn URLs for contact records."""
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if not is_person_linkedin_url(cleaned):
            raise ValueError(PERSON_LINKEDIN_URL_ERROR)
        return cleaned


class AddContactToDealRequest(BaseModel):
    """Request to associate a contact with a deal."""

    deal_id: UUID
    relationship_role: str | None = None
