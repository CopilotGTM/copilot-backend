from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from src.domain.models.organisation_tables.sync.crm import CRMActivity


@dataclass(frozen=True)
class ParticipantInfo:
    """Mention target returned with deal notes."""

    email: str
    name: str | None


@dataclass(frozen=True)
class CreateUserNoteResult:
    """User note creation result with exact-duplicate idempotency semantics."""

    note: CRMActivity
    created_new: bool


@dataclass(frozen=True)
class NoteWriteContext:
    """Caller metadata for internal notes and future note mutation events."""

    source: str
    actor_user_id: UUID
    actor_name: str | None = None
    actor_email: str | None = None
    created_via: str = "api"


class NotesServiceError(Exception):
    """HTTP-shaped notes service error for routers, agents, and bot handlers."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)
