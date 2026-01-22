"""Service for creating and updating user-created CRM tasks."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.master_tables import User
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivity,
    CRMActivityObjectType,
    CRMActivityPriority,
    CRMActivityStatus,
    CRMActivityType,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.infra.utils.logger import get_logger
from src.service.crm.access.crm_user_service import build_user_display_name

logger = get_logger(__name__)

_ALLOWED_TASK_TYPES = {CRMActivityType.TASK.value, CRMActivityType.TODO.value}
_ALLOWED_STATUSES = {status.value for status in CRMActivityStatus}
_ALLOWED_PRIORITIES = {priority.value for priority in CRMActivityPriority}


class TasksWriteServiceError(Exception):
    """Custom exception for task write errors."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class TasksWriteService:
    """Write operations for user-created tasks."""

    def __init__(self, session: AsyncSession, organisation_id: UUID, actor_user_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self.actor_user_id = actor_user_id

    async def close(self) -> None:
        try:
            await self.session.close()
        except Exception:
            logger.debug("TasksWriteService close exception", exc_info=True)

    async def create_task(self, deal_id: UUID, payload: dict[str, Any]) -> CRMActivity:
        deal = await self._load_deal(deal_id)

        task_type = self._normalize_enum(payload.get("task_type")) or CRMActivityType.TASK.value
        if task_type not in _ALLOWED_TASK_TYPES:
            raise TasksWriteServiceError("task_type must be TASK or TODO", 400)

        status = self._normalize_enum(payload.get("status")) or CRMActivityStatus.NOT_STARTED.value
        if status not in _ALLOWED_STATUSES:
            raise TasksWriteServiceError("Invalid task status", 400)

        priority = self._normalize_enum(payload.get("priority")) or CRMActivityPriority.NONE.value
        if priority not in _ALLOWED_PRIORITIES:
            raise TasksWriteServiceError("Invalid task priority", 400)

        owner_user_id = (
            payload.get("owner_user_id") if "owner_user_id" in payload else self.actor_user_id
        )
        owner_external_id, owner_name, owner_email = await self._resolve_owner(owner_user_id)

        now = datetime.now(UTC)
        completed_date = payload.get("completed_date")
        if status != CRMActivityStatus.COMPLETED.value:
            completed_date = None
        elif completed_date is None:
            completed_date = now

        task_id = uuid4()
        task = CRMActivity(
            activity_id=task_id,
            organisation_id=self.organisation_id,
            integration_id=None,
            external_id=str(task_id),
            activity_type=task_type,
            subject=payload.get("subject"),
            body=payload.get("body"),
            status=status,
            priority=priority,
            due_date=payload.get("due_date"),
            reminder_date=payload.get("reminder_date"),
            completed_date=completed_date,
            deal_id=deal.deal_id,
            deal_external_id=deal.external_id,
            related_object_type=CRMActivityObjectType.DEAL.value,
            related_external_id=deal.external_id,
            related_local_id=deal.deal_id,
            related_name=deal.name,
            owner_external_id=owner_external_id,
            owner_name=owner_name,
            owner_email=owner_email,
            source_created_at=now,
            source_modified_at=now,
            synced_at=now,
            last_seen_at=now,
            properties={
                "is_user_created": True,
                "created_via": "api",
            },
        )

        self.session.add(task)
        await self.session.flush()
        await self.session.refresh(task)

        return task

    async def update_task(self, task_id: UUID, payload: dict[str, Any]) -> CRMActivity:
        if not payload:
            raise TasksWriteServiceError("No fields to update", 400)

        task = await self._load_user_task(task_id)

        updated = False
        if "subject" in payload:
            task.subject = payload.get("subject")
            updated = True
        if "body" in payload:
            task.body = payload.get("body")
            updated = True

        task_type = None
        if "task_type" in payload:
            task_type = self._normalize_enum(payload.get("task_type"))
            if task_type is None:
                raise TasksWriteServiceError("task_type cannot be null", 400)
            if task_type and task_type not in _ALLOWED_TASK_TYPES:
                raise TasksWriteServiceError("task_type must be TASK or TODO", 400)
            task.activity_type = task_type
            updated = True

        if "priority" in payload:
            priority = self._normalize_enum(payload.get("priority"))
            if priority is None:
                raise TasksWriteServiceError("priority cannot be null", 400)
            if priority and priority not in _ALLOWED_PRIORITIES:
                raise TasksWriteServiceError("Invalid task priority", 400)
            task.priority = priority
            updated = True

        if "due_date" in payload:
            task.due_date = payload.get("due_date")
            updated = True

        if "reminder_date" in payload:
            task.reminder_date = payload.get("reminder_date")
            updated = True

        if "owner_user_id" in payload:
            owner_external_id, owner_name, owner_email = await self._resolve_owner(
                payload.get("owner_user_id")
            )
            task.owner_external_id = owner_external_id
            task.owner_name = owner_name
            task.owner_email = owner_email
            updated = True

        status_in_payload = "status" in payload
        completed_in_payload = "completed_date" in payload
        status = self._normalize_enum(payload.get("status")) if status_in_payload else task.status
        if status_in_payload and status is None:
            raise TasksWriteServiceError("status cannot be null", 400)
        if status and status not in _ALLOWED_STATUSES:
            raise TasksWriteServiceError("Invalid task status", 400)
        if (
            completed_in_payload
            and payload.get("completed_date")
            and status != CRMActivityStatus.COMPLETED.value
        ):
            status = CRMActivityStatus.COMPLETED.value

        if status_in_payload or (
            completed_in_payload and payload.get("completed_date") is not None
        ):
            task.status = status
            updated = True

        now = datetime.now(UTC)
        if status == CRMActivityStatus.COMPLETED.value:
            if completed_in_payload and payload.get("completed_date") is not None:
                task.completed_date = payload.get("completed_date")
                updated = True
            elif (
                completed_in_payload
                and payload.get("completed_date") is None
                or task.completed_date is None
            ):
                task.completed_date = now
                updated = True
        elif completed_in_payload or status_in_payload:
            task.completed_date = None
            updated = True

        if not updated:
            raise TasksWriteServiceError("No fields to update", 400)

        if task.subject is None and task.body is None:
            raise TasksWriteServiceError("subject or body is required", 400)

        task.source_modified_at = now
        task.synced_at = now
        task.last_seen_at = now
        properties = task.properties or {}
        properties["updated_via"] = "api"
        task.properties = properties

        await self.session.flush()
        await self.session.refresh(task)

        return task

    async def delete_task(self, task_id: UUID) -> CRMActivity:
        task = await self._load_user_task(task_id)

        now = datetime.now(UTC)
        task.is_deleted = True
        task.source_modified_at = now
        task.synced_at = now
        task.last_seen_at = now
        properties = task.properties or {}
        properties["deleted_via"] = "api"
        task.properties = properties

        await self.session.flush()
        await self.session.refresh(task)

        return task

    async def _load_deal(self, deal_id: UUID) -> CRMDeal:
        deal = await self.session.get(CRMDeal, deal_id)
        if not deal or deal.organisation_id != self.organisation_id:
            raise TasksWriteServiceError("Deal not found", 404)
        if deal.is_deleted:
            raise TasksWriteServiceError("Deal is deleted", 404)
        return deal

    async def _load_user_task(self, task_id: UUID) -> CRMActivity:
        task = await self.session.get(CRMActivity, task_id)
        if not task or task.organisation_id != self.organisation_id:
            raise TasksWriteServiceError("Task not found", 404)
        if task.is_deleted:
            raise TasksWriteServiceError("Task not found", 404)
        if task.activity_type not in _ALLOWED_TASK_TYPES:
            raise TasksWriteServiceError("Task not found", 404)
        if task.integration_id is not None:
            raise TasksWriteServiceError("External CRM tasks are read-only", 409)
        return task

    async def _resolve_owner(
        self, user_id: UUID | None
    ) -> tuple[str | None, str | None, str | None]:
        if user_id is None:
            return None, None, None

        user = await self.session.get(User, user_id)
        if not user:
            raise TasksWriteServiceError("User not found", 404)
        if user.organisation_id != self.organisation_id:
            raise TasksWriteServiceError("User not in organisation", 403)

        return (
            str(user.user_id),
            build_user_display_name(user),
            str(user.email),
        )

    @staticmethod
    def _normalize_enum(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, Enum):
            return value.value
        return str(value)
