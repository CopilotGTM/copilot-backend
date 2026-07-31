"""CRM-only tenancy declarations layered over the shared sync mixins.

The generic sync mixins still expose legacy single-column integration foreign
keys for domains scheduled in Task 5. CRM tables override only that field here
so Task 4 can replace it with an explicit composite tenant relationship without
weakening integrity elsewhere.
"""

from __future__ import annotations

from uuid import UUID

from sqlmodel import Field

from ...tenant_constraints import tenant_foreign_key
from ..base import FlexibleSyncedRecordMixin, SyncedRecordMixin

__all__ = (
    "CRMFlexibleSyncedRecordMixin",
    "CRMSyncedRecordMixin",
    "tenant_foreign_key",
)


class CRMSyncedRecordMixin(SyncedRecordMixin):
    """Require a provider integration while leaving its FK to each CRM table."""

    integration_id: UUID = Field(
        index=True,
        description="Integration owning this CRM record.",
    )


class CRMFlexibleSyncedRecordMixin(FlexibleSyncedRecordMixin):
    """Allow provider-neutral CRM rows while leaving their FK to each table."""

    integration_id: UUID | None = Field(
        default=None,
        index=True,
        description="Integration owning this CRM record.",
    )
