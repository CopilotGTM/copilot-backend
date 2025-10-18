from datetime import UTC, date, datetime

from sqlalchemy import DateTime, func
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """Return timezone-aware UTC datetime"""
    return datetime.now(UTC)


def today() -> date:
    return date.today()


class CreatedAtMixin(SQLModel):
    created_at: datetime = Field(
        default=None,
        nullable=False,
        index=True,
        sa_column_kwargs={"server_default": func.now()},
        sa_type=DateTime(timezone=True),
    )


class TimestampMixin(CreatedAtMixin):
    """For main entities where you want updated_at as well."""

    updated_at: datetime = Field(
        default=None,
        nullable=False,
        index=True,
        sa_column_kwargs={"server_default": func.now(), "onupdate": func.now()},
        sa_type=DateTime(timezone=True),
    )
