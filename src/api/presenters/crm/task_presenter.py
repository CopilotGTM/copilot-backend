"""Presentation helpers for CRM task API responses."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.api.schemas.crm_task_models import TaskAssignee, TaskDetail, TaskListItem
from src.domain.models.organisation_tables.sync.crm import CRMTask
from src.service.crm.tasks.contracts import TaskReadRecord, TaskReminderSummary

_TASK_ACTIVITY_TYPES = {"TASK", "TODO"}


def compute_task_is_overdue(due_date: datetime | None, status: str | None) -> bool:
    """Return True when a task is past due and not in a terminal state."""

    if not due_date or status in ("COMPLETED", "CANCELLED"):
        return False

    reference = datetime.now(UTC)
    due = due_date if due_date.tzinfo else due_date.replace(tzinfo=UTC)
    return due < reference


def normalize_task_email(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized or None


def parse_task_uuid(value: str | UUID | None) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if not value:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def task_due_datetime(task: CRMTask) -> datetime | None:
    if task.due_at:
        return task.due_at
    if task.due_date:
        return datetime.combine(task.due_date, datetime.min.time(), tzinfo=UTC)
    return None


def task_source(task: CRMTask) -> str | None:
    value = (task.task_metadata or {}).get("source")
    return value if isinstance(value, str) else None


def present_task_assignee(task: Any) -> TaskAssignee | None:
    """Return a stable assignee contract over CRMTask assignee fields."""

    metadata = getattr(task, "task_metadata", None) or {}
    assignee_kind = metadata.get("assignee_kind")
    email = normalize_task_email(getattr(task, "assigned_to_email", None))
    name = getattr(task, "assigned_to_name", None)
    public_user_id = parse_task_uuid(metadata.get("assigned_to_public_user_id"))
    crm_user_id = getattr(task, "assigned_to_user_id", None)

    if assignee_kind == "external":
        if email:
            filter_key = f"email:{email}"
        elif name:
            filter_key = f"external_name:{str(name).strip().lower()}"
        else:
            return None
        return TaskAssignee(
            kind="external",
            filter_key=filter_key,
            name=name,
            email=email,
        )

    if email:
        filter_key = f"email:{email}"
    elif public_user_id:
        filter_key = f"public_user:{public_user_id}"
    elif crm_user_id:
        filter_key = f"crm_user:{crm_user_id}"
    elif name:
        filter_key = f"name:{str(name).strip().lower()}"
    else:
        return None

    kind = "internal_user" if public_user_id else "crm_owner" if crm_user_id else "unknown"
    return TaskAssignee(
        kind=kind,
        filter_key=filter_key,
        name=name,
        email=email,
        public_user_id=public_user_id,
        crm_user_id=crm_user_id,
    )


def build_task_due_metadata(task: CRMTask) -> dict[str, str | bool | None]:
    metadata = task.task_metadata or {}
    is_all_day = bool(task.is_all_day)
    timezone_name = metadata.get("due_timezone")
    due_local_date = metadata.get("due_local_date")

    if is_all_day and task.due_date is not None and not due_local_date:
        due_local_date = task.due_date.isoformat()
    elif is_all_day and timezone_name and not due_local_date:
        try:
            due_date = task_due_datetime(task)
            if due_date is None:
                raise ValueError("missing due date")
            due = due_date if due_date.tzinfo else due_date.replace(tzinfo=UTC)
            due_local_date = due.astimezone(ZoneInfo(timezone_name)).date().isoformat()
        except (ZoneInfoNotFoundError, ValueError):
            due_local_date = None

    return {
        "due_local_date": due_local_date if isinstance(due_local_date, str) else None,
        "due_timezone": timezone_name if isinstance(timezone_name, str) else None,
        "is_all_day": is_all_day,
    }


def present_task_list_item(record: TaskReadRecord | CRMTask) -> TaskListItem:
    """Build a TaskListItem from a CRMTask source row or enriched read record."""

    task = record.task if isinstance(record, TaskReadRecord) else record
    deal_name = record.deal_name if isinstance(record, TaskReadRecord) else None
    has_reminder = record.has_active_reminder if isinstance(record, TaskReadRecord) else False
    due_date = task_due_datetime(task)
    metadata = task.task_metadata or {}
    is_external = metadata.get("assignee_kind") == "external"
    due_metadata = build_task_due_metadata(task)
    due_local_date = due_metadata["due_local_date"]
    due_timezone = due_metadata["due_timezone"]
    is_all_day = due_metadata["is_all_day"]
    task_type = metadata.get("task_type")

    return TaskListItem(
        task_id=task.task_id,
        subject=task.title,
        body=task.description,
        status=task.status,
        task_type=task_type if task_type in _TASK_ACTIVITY_TYPES else "TASK",
        priority=task.priority,
        due_date=due_date,
        completed_date=task.completed_at,
        reminder_date=task.reminder_at,
        due_local_date=due_local_date if isinstance(due_local_date, str) else None,
        due_timezone=due_timezone if isinstance(due_timezone, str) else None,
        is_all_day=bool(is_all_day),
        owner_id=task.assigned_to_user_id,
        owner_name=None if is_external else task.assigned_to_name,
        owner_email=None if is_external else task.assigned_to_email,
        assigned_to=present_task_assignee(task),
        external_assignee_email=task.assigned_to_email if is_external else None,
        external_assignee_name=task.assigned_to_name if is_external else None,
        deal_id=task.deal_id,
        deal_name=deal_name,
        source=task_source(task),
        created_by_user_id=task.created_by_user_id,
        is_overdue=compute_task_is_overdue(due_date, task.status),
        has_reminder=has_reminder,
        is_read_only=task.integration_id is not None,
        created_at=task.created_at,
        updated_at=task.updated_at,
        synced_at=task.source_modified_at or task.updated_at,
    )


def present_task_detail(record: TaskReadRecord | CRMTask) -> TaskDetail:
    """Build a TaskDetail with computed fields."""

    item = present_task_list_item(record)
    task = record.task if isinstance(record, TaskReadRecord) else record
    reminder = record.reminder if isinstance(record, TaskReadRecord) else None

    return TaskDetail(
        **item.model_dump(),
        source_created_at=task.source_created_at,
        source_modified_at=task.source_modified_at,
        is_deleted_remote=bool(task.is_deleted),
        reminder=_present_reminder_summary(reminder),
        properties=task.task_metadata,
        metrics=None,
        integration_id=task.integration_id,
        organisation_id=task.organisation_id,
        last_seen_at=task.source_modified_at,
    )


def _present_reminder_summary(summary: TaskReminderSummary | None) -> dict[str, Any] | None:
    if summary is None:
        return None
    return {
        "reminder_id": str(summary.reminder_id),
        "status": summary.status,
        "trigger_time": summary.trigger_time.isoformat() if summary.trigger_time else None,
        "recurrence": summary.recurrence,
        "delivery_channel": summary.delivery_channel,
    }
