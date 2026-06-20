"""Shared CRM mutation-event foundation.

CRM entity write services import from this package instead of the deal-specific
ORM naming below it. The table name remains deal-shaped for compatibility, but
this public surface is intentionally generic across contacts, accounts, and deals.
"""

from src.service.crm.mutations.contracts import (
    CRMFieldChange,
    CRMMutationContext,
    CRMMutationEventServiceError,
    CRMMutationRecordCommand,
    CRMMutationRecordResult,
)
from src.service.crm.mutations.event_service import CRMMutationEventService
from src.service.crm.mutations.json_safe import changed_field, normalize_changed_fields

__all__ = [
    "CRMFieldChange",
    "CRMMutationContext",
    "CRMMutationEventService",
    "CRMMutationEventServiceError",
    "CRMMutationRecordCommand",
    "CRMMutationRecordResult",
    "changed_field",
    "normalize_changed_fields",
]
