import uuid
from uuid import UUID, uuid4
from typing import List, Optional
from datetime import date, datetime

from sqlmodel import SQLModel, Field, Relationship


def utcnow() -> datetime:
    return datetime.utcnow()


def today() -> date:
    return date.today()


class Organisation(SQLModel, table=True):
    __tablename__ = "organisations"

    organisation_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
    )
    organisation_name: str = Field(
        ...,
        max_length=255,
        nullable=False,
        unique=True,
    )
    sales_team_size: Optional[int] = Field(default=None)
    country: Optional[str] = Field(default=None)
    company_address: Optional[str] = Field(default=None)
    billing_address: Optional[str] = Field(default=None)
    linked_urls: Optional[str] = Field(default=None)
    website_urls: Optional[str] = Field(default=None)
    summary: Optional[str] = Field(default=None)
    is_active: bool = Field(default=True)
    logo_url: Optional[str] = Field(default=None, max_length=500)
    joined_date: date = Field(default_factory=today)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    # back‑ref to users
    users: List["User"] = Relationship(back_populates="organisation")


class User(SQLModel, table=True):
    __tablename__ = "users"

    user_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
    )
    organisation_id: UUID = Field(
        foreign_key="organisations.organisation_id",
        nullable=False,
        index=True,
    )
    first_name: str = Field(..., max_length=100)
    last_name: str = Field(..., max_length=100)
    email: str = Field(
        ...,
        max_length=255,
        nullable=False,
        unique=True,
    )
    organisation_role: str = Field(..., nullable=False)
    title: Optional[str] = Field(default=None, max_length=100)
    is_active: bool = Field(default=True)
    is_admin: bool = Field(default=False)
    last_login_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    organisation: Organisation = Relationship(back_populates="users")
    credentials: List["UserCredential"] = Relationship(back_populates="user")


class UserCredential(SQLModel, table=True):
    __tablename__ = "user_credentials"

    user_id: UUID = Field(
        foreign_key="users.user_id",
        primary_key=True,
    )
    email: str = Field(
        ...,
        primary_key=True,
    )
    organisation_id: UUID = Field(
        foreign_key="organisations.organisation_id",
        primary_key=True,
    )
    hashed_password: str = Field(...)
    last_password_change: datetime = Field(default_factory=utcnow)
    password_reset_token: Optional[str] = Field(default=None)

    user: User = Relationship(back_populates="credentials")
