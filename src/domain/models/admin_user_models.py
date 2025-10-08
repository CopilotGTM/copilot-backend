from typing import Optional, List, Type, TypeVar, Generic
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel, EmailStr
from pydantic.generics import GenericModel as _Base

from src.domain.models.master_tables import (
    AdminUserStatus,
    User,
    UserAppRole,
    SupabaseUserStatus,
)

T = TypeVar("T")


class CreateUserRequest(BaseModel):
    first_name: str
    last_name: str
    email: EmailStr
    organisation_id: UUID
    user_role: UserAppRole
    job_title: Optional[str] = None
    organisation_role: str


class UpdateUserRequest(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    job_title: Optional[str] = None
    organisation_role: Optional[str] = None
    user_role: Optional[UserAppRole] = None

    class Config:
        extra = "forbid"


class UserResponse(BaseModel):
    user_id: UUID
    first_name: str
    last_name: str
    email: EmailStr
    organisation_id: UUID
    organisation_role: str
    job_title: Optional[str] = None
    status: SupabaseUserStatus
    admin_status: AdminUserStatus
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    @classmethod
    def from_orm(cls: Type["UserResponse"], user: User) -> "UserResponse":
        return cls(
            user_id=user.user_id,
            first_name=user.first_name,
            last_name=user.last_name,
            email=user.email,
            organisation_id=user.organisation_id,
            organisation_role=user.organisation_role,
            job_title=user.job_title,
            status=user.status,
            admin_status=user.admin_status,
            created_at=user.created_at,
            updated_at=user.updated_at,
        )


class CreateUserResponse(BaseModel):
    user_id: UUID
    email: EmailStr
    created_at: datetime


class PaginatedUsers(_Base, Generic[T]):
    users: List[UserResponse]
    cursor: Optional[str] = None


class UpdateUserStatusRequest(BaseModel):
    admin_status: AdminUserStatus
    note: Optional[str] = None


class UpdateUserStatusResponse(BaseModel):
    user_id: UUID
    status: SupabaseUserStatus
    admin_status: AdminUserStatus
    updated_at: datetime
    approved_at: Optional[datetime] = None
    approved_by: Optional[UUID] = None


class UserActionHistoryResponse(BaseModel):
    id: UUID
    user_id: UUID
    action: str
    note: Optional[str] = None
    details: Optional[dict] = None
    created_at: datetime
    actor_admin_id: Optional[UUID] = None

    class Config:
        from_attributes = True
