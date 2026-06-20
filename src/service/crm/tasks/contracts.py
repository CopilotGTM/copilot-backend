from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from src.domain.models.organisation_tables.sync.crm import CRMActivity


@dataclass(frozen=True)
class TaskWriteContext:
    """Caller metadata for internal task writes and future task mutation events."""

    source: str
    actor_user_id: UUID
    created_via: str = "api"
    user_timezone: str | None = None


@dataclass(frozen=True)
class TaskWriteResult:
    """Canonical task write result returned to API, agents, bot forms, and workflows."""

    task: CRMActivity


class TaskReadServiceError(Exception):
    """HTTP-shaped task read error."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class TaskWriteServiceError(Exception):
    """HTTP-shaped task write error."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)
