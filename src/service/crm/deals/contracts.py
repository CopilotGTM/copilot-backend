"""Shared contracts for internal CRM deal services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal

DealWriteSource = Literal["api", "agent", "workflow", "bot_form", "import_assistant"]


@dataclass(frozen=True)
class DealWriteContext:
    """Caller metadata used for audit, mutation events, and future workflow triggers."""

    source: DealWriteSource
    actor_user_id: UUID | None
    source_label: str | None = None
    approval_state_id: UUID | None = None
    correlation_id: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class DealWriteResult:
    """Canonical result for internal CRM deal writes."""

    deal: CRMDeal
    changed_fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    semantic_event_types: list[str] = field(default_factory=list)
    mutation_event_id: UUID | None = None
    changed: bool = True


@dataclass(frozen=True)
class DealStageChangeResult:
    """Stage mutation result returned to write/lifecycle orchestration."""

    deal: CRMDeal
    changed_fields: dict[str, dict[str, Any]]
    changed: bool


@dataclass(frozen=True)
class DealLifecycleChangeResult:
    """Lifecycle mutation result returned by close/reopen operations."""

    deal: CRMDeal
    changed_fields: dict[str, dict[str, Any]]
    semantic_event_type: str | None
    changed: bool


@dataclass(frozen=True)
class DealOwnerChangeResult:
    """Owner mutation result returned by owner assignment operations."""

    deal: CRMDeal
    changed_fields: dict[str, dict[str, Any]]
    changed: bool


@dataclass(frozen=True)
class DealRelationshipChangeResult:
    """Relationship mutation result for account/contact/team participant orchestration."""

    deal: CRMDeal
    changed_fields: dict[str, dict[str, Any]]
    created_contact_participant_ids: list[UUID] = field(default_factory=list)
    changed: bool = False


class DealReadServiceError(Exception):
    """HTTP-shaped deal read error for routers and tools."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class DealWriteServiceError(Exception):
    """HTTP-shaped deal write error for routers and tools."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)
