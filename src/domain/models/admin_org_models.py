from typing import Optional, List, TypeVar, Generic
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel, EmailStr
from pydantic.generics import GenericModel as _Base

from src.domain.models.master_tables import OrganisationStatus

T = TypeVar("T")


class CreateOrganizationRequest(BaseModel):
    organisation_name: str
    country: Optional[str] = None
    sales_team_size: Optional[int] = None
    company_address: Optional[str] = None
    billing_address: Optional[str] = None
    linked_urls: Optional[List[str]] = None
    website_urls: Optional[List[str]] = None
    summary: Optional[str] = None
    logo_url: Optional[str] = None


class ListOrganizationResponse(BaseModel):
    organisation_id: UUID
    organisation_name: str
    is_active: bool
    country: Optional[str] = None
    sales_team_size: Optional[int] = None
    company_address: Optional[str] = None
    linked_urls: Optional[List[str]] = None
    website_urls: Optional[List[str]] = None
    summary: Optional[str] = None
    logo_url: Optional[str] = None
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



class PaginationOrganizations(_Base, Generic[T]):
    organizations: List[ListOrganizationResponse]
    cursor: Optional[str] = None


class UpdateOrganizationRequest(BaseModel):
    organisation_name: Optional[str] = None
    country: Optional[str] = None
    sales_team_size: Optional[int] = None
    company_address: Optional[str] = None
    billing_address: Optional[str] = None
    linked_urls: Optional[str] = None
    website_urls: Optional[str] = None
    summary: Optional[str] = None
    logo_url: Optional[str] = None
    is_active: Optional[bool] = None


class DeleteOrganizationResponse(BaseModel):
    organisation_id: UUID
    organisation_name: str
    schema_name: Optional[str] = None


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
    note: Optional[str] = None


class UpdateOrganisationStatusResponse(BaseModel):
    organisation_id: UUID
    status: OrganisationStatus
    updated_at: datetime
    approved_at: Optional[datetime] = None
    approved_by: Optional[UUID] = None


class OrganisationActionHistoryResponse(BaseModel):
    id: UUID
    organisation_id: UUID
    action: str
    note: Optional[str] = None
    details: Optional[dict] = None
    created_at: datetime
    actor_admin_id: Optional[UUID] = None

    class Config:
        from_attributes = True


class PaginatedOrganisationActionHistoryResponse(BaseModel):
    history: List[OrganisationActionHistoryResponse]
    cursor: Optional[str] = None