from datetime import UTC, date, datetime

from sqlalchemy import Column, DateTime, func
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """Return timezone-aware UTC datetime"""
    return datetime.now(UTC)


def today() -> date:
    return date.today()


class CreatedAtMixin(SQLModel):
    created_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            index=True,
            server_default=func.now(),
        ),
    )


class TimestampMixin(CreatedAtMixin):
    """For main entities where you want updated_at as well."""

    updated_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            index=True,
            server_default=func.now(),
            onupdate=func.now(),
        ),
    )
