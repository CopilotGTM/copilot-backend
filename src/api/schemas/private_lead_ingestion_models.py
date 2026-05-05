"""Schemas and limits for private lead-ingestion API contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from src.domain.models.master_tables import PublicIngressKeyStatus
from src.domain.models.organisation_tables.sync.crm.leads import (
    CRMLeadPriority,
    CRMLeadSourceType,
    CRMLeadStatus,
)

MAX_PRIVATE_ALLOWED_IPS = 100
MAX_PRIVATE_PAYLOAD_DEPTH = 8
MAX_PRIVATE_PAYLOAD_LIST_ITEMS = 200
MAX_PRIVATE_PAYLOAD_NODES = 2000
MAX_PRIVATE_PAYLOAD_BYTES = 256_000
MAX_PRIVATE_SCALAR_STRING_LENGTH = 10_000


class CreateLeadIngestionKeyRequest(BaseModel):
    """Create an organisation-level private lead ingestion key."""

    name: str = Field(min_length=1, max_length=255)
    allowed_ips: list[str] = Field(default_factory=list, max_length=MAX_PRIVATE_ALLOWED_IPS)
    expires_at: datetime | None = None
    rate_limit_policy: dict[str, Any] = Field(default_factory=dict)


class LeadIngestionKeyCreateResponse(BaseModel):
    """Create response that returns the private secret exactly once."""

    key_id: str
    secret: str
    status: PublicIngressKeyStatus
    created_at: datetime


class LeadIngestionKeyItem(BaseModel):
    """Private ingestion key metadata safe for authenticated UI/API clients."""

    key_id: str
    name: str | None = None
    status: PublicIngressKeyStatus
    allowed_ips: list[str] = Field(default_factory=list)
    rate_limit_policy: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime


class UpdateLeadIngestionKeyStatusRequest(BaseModel):
    """Change the runtime status of a private ingestion key."""

    status: PublicIngressKeyStatus


class PrivateLeadIngestionRequest(BaseModel):
    """Normalized private API lead payload."""

    model_config = ConfigDict(extra="forbid")

    external_id: str | None = Field(default=None, max_length=255)
    source_type: CRMLeadSourceType = CRMLeadSourceType.API
    source_channel: str | None = Field(default=None, max_length=128)
    source_name: str | None = Field(default=None, max_length=255)
    source_ref: str | None = Field(default=None, max_length=255)
    status: CRMLeadStatus = CRMLeadStatus.NEW
    priority: CRMLeadPriority | None = None
    score: float | None = Field(default=None, ge=0)

    first_name: str | None = Field(default=None, max_length=255)
    last_name: str | None = Field(default=None, max_length=255)
    full_name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=64)
    job_title: str | None = Field(default=None, max_length=255)
    linkedin_url: str | None = Field(default=None, max_length=500)

    company_name: str | None = Field(default=None, max_length=255)
    company_domain: str | None = Field(default=None, max_length=255)
    company_website: str | None = Field(default=None, max_length=500)
    industry: str | None = Field(default=None, max_length=255)

    country: str | None = Field(default=None, max_length=128)
    region: str | None = Field(default=None, max_length=128)
    city: str | None = Field(default=None, max_length=128)

    message: str | None = Field(default=None, max_length=MAX_PRIVATE_SCALAR_STRING_LENGTH)
    interest: str | None = Field(default=None, max_length=255)
    budget_amount: float | None = Field(default=None, ge=0)
    budget_currency: str | None = Field(default=None, min_length=3, max_length=3)

    owner_name: str | None = Field(default=None, max_length=255)
    owner_email: str | None = Field(default=None, max_length=255)

    utm_source: str | None = Field(default=None, max_length=255)
    utm_medium: str | None = Field(default=None, max_length=255)
    utm_campaign: str | None = Field(default=None, max_length=255)
    utm_term: str | None = Field(default=None, max_length=255)
    utm_content: str | None = Field(default=None, max_length=255)

    attribution: dict[str, Any] = Field(default_factory=dict)
    consent: dict[str, Any] = Field(default_factory=dict)
    properties: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    data_payload: dict[str, Any] = Field(default_factory=dict)
    source_created_at: datetime | None = None


class PrivateLeadIngestionResponse(BaseModel):
    """External success response for private lead ingestion."""

    lead_id: UUID
    status: Literal["accepted"] = "accepted"


class PrivateLeadIngestionError(BaseModel):
    """Stable external error payload."""

    code: str
    message: str
    field: str | None = None


class PrivateLeadIngestionErrorResponse(BaseModel):
    """External error wrapper, intentionally separate from GenericResponse."""

    error: PrivateLeadIngestionError
