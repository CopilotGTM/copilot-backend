from datetime import datetime
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, EmailStr
from src.domain.models.master_tables import (
    AdminUserStatus,
    SupabaseUserStatus,
    User,
    UserAppRole,
    UserAuthenticationMethod,
)

T = TypeVar("T")


class CreateUserRequest(BaseModel):
    first_name: str
    last_name: str
    email: EmailStr
    organisation_id: UUID
    user_role: UserAppRole
    job_title: str | None = None
    organisation_role: str
    authentication_method: UserAuthenticationMethod = UserAuthenticationMethod.PASSWORD


class UpdateUserRequest(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    job_title: str | None = None
    organisation_role: str | None = None
    user_role: UserAppRole | None = None

    class Config:
        extra = "forbid"


class UserResponse(BaseModel):
    user_id: UUID
    first_name: str
    last_name: str
    email: EmailStr
    organisation_id: UUID
    organisation_role: str
    user_role: UserAppRole
    job_title: str | None = None
    status: SupabaseUserStatus
    admin_status: AdminUserStatus
    authentication_method: UserAuthenticationMethod
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    @classmethod
    def from_orm(cls: type["UserResponse"], user: User) -> "UserResponse":
        return cls(
            user_id=user.user_id,
            first_name=user.first_name,
            last_name=user.last_name,
            email=user.email,
            organisation_id=user.organisation_id,
            organisation_role=user.organisation_role,
            user_role=user.user_role,
            job_title=user.job_title,
            status=user.status,
            admin_status=user.admin_status,
            authentication_method=user.authentication_method,
            created_at=user.created_at,
            updated_at=user.updated_at,
        )


class CreateUserResponse(BaseModel):
    user_id: UUID
    email: EmailStr
    created_at: datetime


class PaginatedUsers(BaseModel, Generic[T]):
    users: list[UserResponse]
    cursor: str | None = None


class UpdateUserStatusRequest(BaseModel):
    admin_status: AdminUserStatus
    note: str | None = None


class UpdateUserStatusResponse(BaseModel):
    user_id: UUID
    status: SupabaseUserStatus
    admin_status: AdminUserStatus
    updated_at: datetime
    approved_at: datetime | None = None
    approved_by: UUID | None = None


class UserActionHistoryResponse(BaseModel):
    id: UUID
    user_id: UUID
    user_email: EmailStr | None = None
    user_first_name: str | None = None
    user_last_name: str | None = None
    organisation_id: UUID | None = None
    organisation_name: str | None = None
    action: str
    note: str | None = None
    details: dict | None = None
    created_at: datetime
    actor_admin_id: UUID | None = None
    actor_admin_name: str | None = None
    actor_admin_email: EmailStr | None = None

    class Config:
        from_attributes = True


class PaginatedUserActionHistoryResponse(BaseModel):
    history: list[UserActionHistoryResponse]
    cursor: str | None = None
