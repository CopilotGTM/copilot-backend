"""Shared contracts for CRM mutation audit events.

These contracts are used by contact, account, and deal write services so API,
agent, and future workflow callers produce one consistent mutation envelope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

CRMEntityType = Literal["contact", "account", "deal", "custom_field", "pipeline", "stage"]
CRMMutationSource = str


@dataclass(frozen=True)
class CRMFieldChange:
    """Canonical old/new value pair for one changed CRM field.

    Write services build this shape before JSON normalization so tests and
    callers can reason about domain values without caring about storage formats.
    """

    old: Any
    new: Any


@dataclass(frozen=True)
class CRMMutationContext:
    """Caller metadata shared by CRM write services and future workflow writes.

    The context is intentionally separate from entity fields: it answers who
    caused the write, where it came from, and how retries should be correlated.
    """

    source: CRMMutationSource
    actor_user_id: UUID | None = None
    source_label: str | None = None
    approval_state_id: UUID | None = None
    correlation_id: str | None = None
    idempotency_key: str | None = None
    mutation_group_id: UUID | None = None


@dataclass(frozen=True)
class CRMMutationRecordCommand:
    """Insert command for one immutable CRM mutation event row.

    The command is the boundary between entity-specific write services and the
    generic audit ledger. Callers pass already-computed field changes so this
    service records intent without re-reading or re-diffing mutable CRM rows.
    """

    entity_type: CRMEntityType
    entity_id: UUID
    event_type: str
    operation: str
    changed_fields: dict[str, dict[str, Any]]
    semantic_event_types: list[str]
    context: CRMMutationContext
    summary: str | None = None
    deal_id: UUID | None = None
    integration_id: UUID | None = None
    provider: str | None = None
    source_object_mapping_id: UUID | None = None
    idempotency_scope: str | None = None


@dataclass(frozen=True)
class CRMMutationRecordResult:
    """Result returned after attempting to persist one CRM mutation event.

    `changed=False` means the caller performed a no-op write and should not feed
    workflow dispatchers or response summaries with a synthetic mutation event.
    """

    event_id: UUID | None
    changed: bool
    changed_fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    semantic_event_types: list[str] = field(default_factory=list)


class CRMMutationEventServiceError(Exception):
    """HTTP-shaped mutation-event error for CRM write services.

    CRM service facades can translate this directly into API errors while agent
    and workflow callers still receive a structured failure reason.
    """

    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)
