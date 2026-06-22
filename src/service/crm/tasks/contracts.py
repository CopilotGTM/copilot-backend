from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from src.domain.models.organisation_tables.sync.crm import CRMTask


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

    task: CRMTask


@dataclass(frozen=True)
class TaskReminderSummary:
    """API-neutral reminder fields needed by task read responses."""

    reminder_id: UUID
    status: str
    trigger_time: datetime | None
    recurrence: str | None
    delivery_channel: str | None


@dataclass(frozen=True)
class TaskReadRecord:
    """Task source row plus API display fields resolved outside crm_tasks."""

    task: CRMTask
    deal_name: str | None = None
    has_active_reminder: bool = False
    reminder: TaskReminderSummary | None = None


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
