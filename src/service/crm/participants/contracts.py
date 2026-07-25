"""Shared contracts for the participant-backed CRM deal people services.

The participant module owns storage-facing IDs (`participant_id`) while routers
and tools can still expose stakeholder language. These small dataclasses keep
compatibility facades from leaking tuple shapes into the new service boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal
from uuid import UUID

from src.contracts.crm.stakeholders import StakeholderDetail
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealParticipant

ParticipantChangedReason = Literal["created", "restored", "updated", "skipped"]
ParticipantMatchedBy = Literal["contact_id", "email", "company_id", "user_id", "created"]


class StakeholderChangeType(str, Enum):
    """Stable reasons serialized in stakeholder-sync task payloads."""

    NEW_DEAL = "NEW_DEAL"
    PARTICIPANT_ADDED = "PARTICIPANT_ADDED"
    PARTICIPANT_UPDATED = "PARTICIPANT_UPDATED"
    PARTICIPANT_REMOVED = "PARTICIPANT_REMOVED"
    MANUAL_REFRESH = "MANUAL_REFRESH"


@dataclass(frozen=True)
class ParticipantWriteContext:
    """Caller metadata for future participant audit events."""

    source: str
    actor_user_id: UUID
    source_label: str | None = None


@dataclass(frozen=True)
class StakeholderWriteContext:
    """Caller provenance for stakeholder semantic writes.

    The entrypoint sets this context from trusted runtime state, rather than
    accepting provenance from client payloads that could spoof API/agent/system.
    """

    source: str
    actor_user_id: UUID | None = None
    source_label: str | None = None
    created_via: str | None = None


@dataclass(frozen=True)
class ParticipantSkipped:
    """Participant request skipped by the canonical participant writer."""

    participant_type: str
    related_id: UUID
    relationship_role: str | None
    reason: str


@dataclass(frozen=True)
class ParticipantWriteResult:
    """Canonical result for raw participant membership writes."""

    created: list[CRMDealParticipant]
    skipped: list[ParticipantSkipped]
    contact_participant_changed: bool
    integration_id: UUID


@dataclass(frozen=True)
class StakeholderWriteResult:
    """Canonical result for stakeholder semantic writes."""

    stakeholder: StakeholderDetail
    created: bool
    reactivated: bool
    reused: bool


@dataclass(frozen=True)
class CommunicationStakeholderWriteResult:
    """Outcome of one communication-discovered participant write batch."""

    approved_contact_ids: frozenset[UUID] = frozenset()
    approved_added: int = 0
    pending_added: int = 0
    skipped_domain: int = 0


class ParticipantServiceError(Exception):
    """HTTP-shaped participant service error for routers and tools."""

    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)
