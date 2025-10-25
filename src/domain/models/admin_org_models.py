from datetime import datetime
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, EmailStr
from src.domain.models.master_tables import OrganisationStatus

T = TypeVar("T")


class CreateOrganizationRequest(BaseModel):
    organisation_name: str
    country: str = None
    sales_team_size: int | None = None
    company_address: str | None = None
    linked_urls: list[str] | None = None
    website_urls: list[str] | None = None
    summary: str | None = None
    logo_url: str | None = None

    class Config:
        extra = "ignore"


class ListOrganizationResponse(BaseModel):
    organisation_id: UUID
    organisation_name: str
    is_active: bool
    country: str | None = None
    sales_team_size: int | None = None
    company_address: str | None = None
    linked_urls: list[str] | None = None
    website_urls: list[str] | None = None
    summary: str | None = None
    logo_url: str | None = None
    status: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AdminUserResponse(BaseModel):
    user_id: UUID
    email: EmailStr
    first_name: str
    last_name: str
    organisation_role: str


class CreateOrganizationResponse(BaseModel):
    organisation_id: UUID
    organisation_name: str
    schema_name: str
    created_at: datetime
    updated_at: datetime


class PaginationOrganizations(BaseModel, Generic[T]):
    organizations: list[ListOrganizationResponse]
    cursor: str | None = None


class UpdateOrganizationRequest(BaseModel):
    organisation_name: str | None = None
    country: str | None = None
    sales_team_size: int | None = None
    company_address: str | None = None
    linked_urls: str | None = None
    website_urls: str | None = None
    summary: str | None = None
    logo_url: str | None = None
    is_active: bool | None = None

    class Config:
        extra = "ignore"


class DeleteOrganizationResponse(BaseModel):
    organisation_id: UUID
    organisation_name: str
    schema_name: str | None = None


class RunMigrationsRequest(BaseModel):
    target_version: str = "head"


class RunMigrationsResponse(BaseModel):
    organisation_id: UUID
    schema_name: str
    target_version: str


class RunMasterMigrationsResponse(BaseModel):
    target_version: str
    admin_user_id: UUID


class UpdateOrganisationStatusRequest(BaseModel):
    status: OrganisationStatus
    note: str | None = None


class UpdateOrganisationStatusResponse(BaseModel):
    organisation_id: UUID
    status: OrganisationStatus
    updated_at: datetime
    approved_at: datetime | None = None
    approved_by: UUID | None = None


class OrganisationActionHistoryResponse(BaseModel):
    id: UUID
    organisation_id: UUID
    action: str
    note: str | None = None
    details: dict | None = None
    created_at: datetime
    actor_admin_id: UUID | None = None

    class Config:
        from_attributes = True


class PaginatedOrganisationActionHistoryResponse(BaseModel):
    history: list[OrganisationActionHistoryResponse]
    cursor: str | None = None
