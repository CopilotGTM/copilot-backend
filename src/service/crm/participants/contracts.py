"""Shared contracts for the participant-backed CRM deal people services.

The participant module owns storage-facing IDs (`participant_id`) while routers
and tools can still expose stakeholder language. These small dataclasses keep
compatibility facades from leaking tuple shapes into the new service boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from src.domain.models.organisation_tables.sync.crm.deals import CRMDealParticipant

ParticipantChangedReason = Literal["created", "restored", "updated", "skipped"]
ParticipantMatchedBy = Literal["contact_id", "email", "company_id", "user_id", "created"]


@dataclass(frozen=True)
class ParticipantWriteContext:
    """Caller metadata for future participant audit events."""

    source: str
    actor_user_id: UUID
    source_label: str | None = None


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

    participant_id: UUID
    participant: CRMDealParticipant
    created: bool
    matched_by: ParticipantMatchedBy


class ParticipantServiceError(Exception):
    """HTTP-shaped participant service error for routers and tools."""

    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)
