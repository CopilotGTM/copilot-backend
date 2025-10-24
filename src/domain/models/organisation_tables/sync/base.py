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
        foreign_key="knowledge_integrations.integration_id",
        description="Integration owning this record.",
    )
    is_deleted: bool = Field(
        default=False,
        description="Soft delete flag reflecting provider state.",
    )
