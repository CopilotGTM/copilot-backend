from __future__ import annotations

from enum import Enum
from uuid import UUID

from sqlmodel import Field

from ..base_mixins import TimestampMixin


class SyncCategory(str, Enum):
    CALENDAR = "calendar"
    CRM = "crm"
    EMAIL = "email"


class SyncedRecordMixin(TimestampMixin):
    """Common columns shared across synced data tables."""

    organisation_id: UUID = Field(index=True)
    integration_id: UUID = Field(
        index=True,
        description="Integration owning this record.",
    )
    is_deleted: bool = Field(
        default=False,
        description="Soft delete flag reflecting provider state.",
    )


class FlexibleSyncedRecordMixin(TimestampMixin):
    """
    Similar to SyncedRecordMixin but allows integration_id to be None.
    Used for records that can be shared across multiple integrations (e.g. Calendar Events).
    """

    organisation_id: UUID = Field(index=True)
    integration_id: UUID | None = Field(
        default=None,
        index=True,
        description="Integration owning this record.",
    )
    is_deleted: bool = Field(
        default=False,
        description="Soft delete flag reflecting provider state.",
    )


class OrgScopedRecordMixin(TimestampMixin):
    """For records scoped to an org, not tied to any specific integration."""

    organisation_id: UUID = Field(index=True)
    is_deleted: bool = Field(
        default=False,
        description="Soft delete flag reflecting provider state.",
    )
