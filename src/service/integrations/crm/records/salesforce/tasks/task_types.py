from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from src.service.integrations.crm.records.common.record_sync_types import RecordSyncResult


@dataclass(slots=True, frozen=True)
class ActivityDimensionHit:
    internal_id: UUID
    name: str | None = None
    email: str | None = None


@dataclass(slots=True, frozen=True)
class NormalizedSalesforceTask:
    external_id: str
    subject: str | None
    body: str | None
    status: str | None
    priority: str | None
    activity_subtype: str | None
    owner_external_id: str | None
    due_date: datetime | None
    completed_date: datetime | None
    deal_external_id: str | None
    contact_external_id: str | None
    company_external_id: str | None
    related_object_type: str | None
    related_external_id: str | None
    source_created_at: datetime | None
    source_modified_at: datetime | None
    properties: dict[str, Any]
    data_payload: dict[str, Any]


@dataclass(slots=True)
class TaskWriteResult:
    tasks_created: int = 0
    tasks_updated: int = 0
    tasks_reactivated: int = 0
    tasks_deactivated: int = 0
    unresolved_owner_link: int = 0
    unresolved_deal_link: int = 0
    unresolved_contact_link: int = 0
    unresolved_company_link: int = 0


@dataclass(slots=True)
class TaskSyncResult(RecordSyncResult):
    fetched_tasks: int = 0
    tasks_created: int = 0
    tasks_updated: int = 0
    tasks_reactivated: int = 0
    tasks_deactivated: int = 0
    unresolved_owner_link: int = 0
    unresolved_deal_link: int = 0
    unresolved_contact_link: int = 0
    unresolved_company_link: int = 0
    skipped_malformed: int = 0
    error_class: str | None = None
    error_message: str | None = None
    counts: dict[str, int] = field(default_factory=dict)

    def to_metadata(self) -> dict[str, Any]:
        payload = self.base_metadata()
        payload.update(
            {
                "fetched_tasks": self.fetched_tasks,
                "tasks_created": self.tasks_created,
                "tasks_updated": self.tasks_updated,
                "tasks_reactivated": self.tasks_reactivated,
                "tasks_deactivated": self.tasks_deactivated,
                "unresolved_owner_link": self.unresolved_owner_link,
                "unresolved_deal_link": self.unresolved_deal_link,
                "unresolved_contact_link": self.unresolved_contact_link,
                "unresolved_company_link": self.unresolved_company_link,
                "skipped_malformed": self.skipped_malformed,
                "error_class": self.error_class,
                "error_message": self.error_message,
            }
        )
        return payload
