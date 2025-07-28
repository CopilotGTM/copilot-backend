from typing import Optional, List
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel, EmailStr


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


class OrganizationResponse(BaseModel):
    organisation_id: UUID
    organisation_name: str
    schema_name: Optional[str] = None
    role_name: Optional[str] = None
    is_active: bool
    country: Optional[str] = None
    sales_team_size: Optional[int] = None
    company_address: Optional[str] = None
    billing_address: Optional[str] = None
    linked_urls: Optional[str] = None
    website_urls: Optional[str] = None
    summary: Optional[str] = None
    logo_url: Optional[str] = None
    created_at: datetime
    updated_at: datetime


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


class ListOrganizationsResponse(BaseModel):
    organizations: List[OrganizationResponse]


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


class ApproveOrganizationRequest(BaseModel):
    organisation_id: UUID
    note: Optional[str] = None


class ApproveOrganizationResponse(BaseModel):
    organisation_id: UUID
    status: str
    approved_at: datetime
    approved_by: UUID