from typing import List, Optional
from datetime import date, datetime

from sqlmodel import Field, SQLModel, Relationship


def utcnow() -> datetime:
    return datetime.utcnow()


def today() -> date:
    return date.today()


class Organisation(SQLModel, table=True):
    __tablename__ = "organisations"
    organisation_id: Optional[str] = Field(
        default=None,
        primary_key=True,
        max_length=36
    )
    organisation_name: str = Field(
        ...,
        max_length=255,
        nullable=False,
        unique=True
    )
    sales_team_size: Optional[int] = None
    country: Optional[str] = None
    company_address: Optional[str] = None
    billing_address: Optional[str] = None
    linked_urls: Optional[str] = None
    website_urls: Optional[str] = None
    summary: Optional[str] = None
    is_active: bool = Field(default=True)
    logo_url: Optional[str] = Field(default=None, max_length=500)
    joined_date: date = Field(default_factory=today)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    # Relationship to users
    users: List["User"] = Relationship(back_populates="organisation")


class User(SQLModel, table=True):
    __tablename__ = "users"
    user_id: Optional[str] = Field(
        default=None,
        primary_key=True,
        max_length=36
    )
    organisation_id: str = Field(
        foreign_key="organisations.organisation_id",
        nullable=False
    )
    first_name: str
    last_name: str
    email: str = Field(
        ...,
        max_length=255,
        nullable=False,
        unique=True
    )
    organisation_role: str = Field(nullable=False)
    title: Optional[str] = None
    is_active: bool = Field(default=True)
    is_admin: bool = Field(default=False)
    last_login_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    # Relationships
    organisation: Optional[Organisation] = Relationship(back_populates="users")
    credentials: Optional["UserCredential"] = Relationship(back_populates="user")


class UserCredential(SQLModel, table=True):
    __tablename__ = "user_credentials"
    user_id: str = Field(
        foreign_key="users.user_id",
        primary_key=True
    )
    email: str = Field(
        ...,
        primary_key=True
    )
    organisation_id: str = Field(
        ...,
        primary_key=True
    )
    hashed_password: str
    last_password_change: datetime = Field(default_factory=utcnow)
    password_reset_token: Optional[str] = None

    # Relationship back to the user
    user: User = Relationship(back_populates="credentials")