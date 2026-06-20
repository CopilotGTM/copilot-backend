"""Task write service for user-created CRM task activities."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.master_tables import User
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivity,
    CRMActivityObjectType,
    CRMActivityPriority,
    CRMActivitySource,
    CRMActivityStatus,
    CRMActivityType,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.infra.database.sessions import master_async_session_scope
from src.infra.utils.logger import get_logger
from src.infra.utils.timezone import DEFAULT_TIMEZONE, resolve_timezone_name
from src.service.crm.access.crm_user_service import build_user_display_name
from src.service.crm.internal.internal_crm_user_service import (
    InternalCRMUserService,
    InternalCRMUserServiceError,
)
from src.service.crm.tasks.contracts import TaskWriteServiceError
from src.service.crm.tasks.notification_service import TaskNotificationService
from src.service.crm.tasks.reminder_service import TaskReminderService

logger = get_logger("task_write_service")

_ALLOWED_TASK_TYPES = {CRMActivityType.TASK.value, CRMActivityType.TODO.value}
_ALLOWED_STATUSES = {status.value for status in CRMActivityStatus}
_ALLOWED_PRIORITIES = {priority.value for priority in CRMActivityPriority}


class TaskWriteService:
    """Write operations for user-created tasks."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        actor_user_id: UUID,
        *,
        user_timezone: str | None = None,
    ):
        self.session = session
        self.organisation_id = organisation_id
        self.actor_user_id = actor_user_id
        self.user_timezone = resolve_timezone_name(user_timezone)
        self._reminders = TaskReminderService(session, organisation_id, actor_user_id)
        self._notifications = TaskNotificationService(session, organisation_id, actor_user_id)

    async def close(self) -> None:
        try:
            await self.session.close()
        except Exception:
            logger.debug("TaskWriteService close exception", exc_info=True)

    async def create_task(
        self, payload: dict[str, Any], *, deal_id: UUID | None = None
    ) -> CRMActivity:
        deal = await self._load_deal(deal_id) if deal_id else None

        task_type = self._normalize_enum(payload.get("task_type")) or CRMActivityType.TASK.value
        if task_type not in _ALLOWED_TASK_TYPES:
            raise TaskWriteServiceError("task_type must be TASK or TODO", 400)

        status = self._normalize_enum(payload.get("status")) or CRMActivityStatus.NOT_STARTED.value
        if status not in _ALLOWED_STATUSES:
            raise TaskWriteServiceError("Invalid task status", 400)

        priority = self._normalize_enum(payload.get("priority")) or CRMActivityPriority.NONE.value
        if priority not in _ALLOWED_PRIORITIES:
            raise TaskWriteServiceError("Invalid task priority", 400)

        owner_user_id = (
            payload.get("owner_user_id") if "owner_user_id" in payload else self.actor_user_id
        )
        owner_id, owner_name, owner_email = await self._resolve_owner(owner_user_id)
        due_date, is_all_day, due_properties = self._normalize_due_payload(payload)
        assignment_properties = (
            {"assigned_to_public_user_id": str(owner_user_id)} if owner_user_id else {}
        )

        now = datetime.now(UTC)
        completed_date = payload.get("completed_date")
        if status != CRMActivityStatus.COMPLETED.value:
            completed_date = None
        elif completed_date is None:
            completed_date = now

        external_assignee_email = payload.get("external_assignee_email")
        external_assignee_name = payload.get("external_assignee_name")

        if external_assignee_email:
            await self._notifications.validate_external_assignee_domain(external_assignee_email)

        task_id = uuid4()
        task = CRMActivity(
            activity_id=task_id,
            organisation_id=self.organisation_id,
            integration_id=None,
            activity_type=task_type,
            subject=payload.get("subject"),
            body=payload.get("body"),
            status=status,
            priority=priority,
            due_date=due_date,
            reminder_date=payload.get("reminder_date"),
            completed_date=completed_date,
            deal_id=deal.deal_id if deal else None,
            related_object_type=CRMActivityObjectType.DEAL.value if deal else None,
            related_local_id=deal.deal_id if deal else None,
            related_name=deal.name if deal else None,
            owner_name=owner_name,
            owner_email=owner_email,
            owner_id=owner_id,
            external_assignee_email=external_assignee_email,
            external_assignee_name=external_assignee_name,
            source=CRMActivitySource.MANUAL.value,
            created_by_user_id=self.actor_user_id,
            is_all_day=is_all_day,
            source_created_at=now,
            source_modified_at=now,
            synced_at=now,
            last_seen_at=now,
            properties={
                "is_user_created": True,
                "created_via": "api",
                **assignment_properties,
                **due_properties,
            },
        )

        self.session.add(task)
        await self.session.flush()
        await self.session.refresh(task)

        # Create linked reminder if requested
        reminder_cfg = payload.get("reminder")
        if reminder_cfg and reminder_cfg.get("enabled"):
            await self._reminders.create_task_reminder(task, reminder_cfg)

        # Send email to external assignee on creation
        if external_assignee_email:
            await self._notifications.send_external_assignment_email(task, deal, is_update=False)

        return task

    async def update_task(self, task_id: UUID, payload: dict[str, Any]) -> CRMActivity:
        if not payload:
            raise TaskWriteServiceError("No fields to update", 400)

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
                raise TaskWriteServiceError("task_type cannot be null", 400)
            if task_type and task_type not in _ALLOWED_TASK_TYPES:
                raise TaskWriteServiceError("task_type must be TASK or TODO", 400)
            task.activity_type = task_type
            updated = True

        if "priority" in payload:
            priority = self._normalize_enum(payload.get("priority"))
            if priority is None:
                raise TaskWriteServiceError("priority cannot be null", 400)
            if priority and priority not in _ALLOWED_PRIORITIES:
                raise TaskWriteServiceError("Invalid task priority", 400)
            task.priority = priority
            updated = True

        if "due_date" in payload:
            due_date, is_all_day, due_properties = self._normalize_due_payload(payload)
            task.due_date = due_date
            task.is_all_day = is_all_day
            properties = task.properties or {}
            properties.pop("due_local_date", None)
            properties.pop("due_timezone", None)
            properties.update(due_properties)
            task.properties = properties
            updated = True
        elif "is_all_day" in payload:
            task.is_all_day = bool(payload.get("is_all_day"))
            properties = task.properties or {}
            if not task.is_all_day:
                properties.pop("due_local_date", None)
                properties.pop("due_timezone", None)
            task.properties = properties
            updated = True

        if "reminder_date" in payload:
            task.reminder_date = payload.get("reminder_date")
            updated = True

        if "owner_user_id" in payload:
            owner_id, owner_name, owner_email = await self._resolve_owner(
                payload.get("owner_user_id")
            )
            task.owner_id = owner_id
            task.owner_name = owner_name
            task.owner_email = owner_email
            properties = task.properties or {}
            if payload.get("owner_user_id"):
                properties["assigned_to_public_user_id"] = str(payload["owner_user_id"])
            else:
                properties.pop("assigned_to_public_user_id", None)
            task.properties = properties
            updated = True

        # External assignee
        external_assignee_changed = False
        if "external_assignee_email" in payload:
            new_ext_email = payload.get("external_assignee_email")
            if new_ext_email:
                await self._notifications.validate_external_assignee_domain(new_ext_email)
            task.external_assignee_email = new_ext_email
            external_assignee_changed = True
            updated = True
        if "external_assignee_name" in payload:
            task.external_assignee_name = payload.get("external_assignee_name")
            updated = True

        # Deal reassociation
        if "deal_id" in payload:
            new_deal_id = payload.get("deal_id")
            if new_deal_id is not None:
                deal = await self._load_deal(new_deal_id)
                task.deal_id = deal.deal_id
                task.related_object_type = CRMActivityObjectType.DEAL.value
                task.related_local_id = deal.deal_id
                task.related_name = deal.name
            else:
                task.deal_id = None
                task.related_object_type = None
                task.related_local_id = None
                task.related_name = None
            updated = True

        status_in_payload = "status" in payload
        completed_in_payload = "completed_date" in payload
        status = self._normalize_enum(payload.get("status")) if status_in_payload else task.status
        if status_in_payload and status is None:
            raise TaskWriteServiceError("status cannot be null", 400)
        if status and status not in _ALLOWED_STATUSES:
            raise TaskWriteServiceError("Invalid task status", 400)
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
            raise TaskWriteServiceError("No fields to update", 400)

        if task.subject is None and task.body is None:
            raise TaskWriteServiceError("subject or body is required", 400)

        task.source_modified_at = now
        task.synced_at = now
        task.last_seen_at = now
        properties = task.properties or {}
        properties["updated_via"] = "api"
        task.properties = properties

        await self.session.flush()
        await self.session.refresh(task)

        # Handle reminder lifecycle
        reminder_cfg = payload.get("reminder")
        if reminder_cfg is not None:
            if reminder_cfg.get("enabled"):
                existing = await self._reminders.get_active_reminder(task.activity_id)
                if existing:
                    await self._reminders.update_task_reminder(existing, reminder_cfg)
                else:
                    await self._reminders.create_task_reminder(task, reminder_cfg)
            else:
                await self._reminders.cancel_task_reminders(task.activity_id)

        # Send email to external assignee on relevant changes
        if task.external_assignee_email:
            if external_assignee_changed:
                # New external assignee — send as new assignment
                assignment_deal = await self._load_deal(task.deal_id) if task.deal_id else None
                await self._notifications.send_external_assignment_email(
                    task, assignment_deal, is_update=False
                )
            elif updated:
                # Task details changed — send update notification
                update_deal = await self._load_deal(task.deal_id) if task.deal_id else None
                await self._notifications.send_external_assignment_email(
                    task, update_deal, is_update=True
                )

        return task

    async def delete_task(self, task_id: UUID) -> CRMActivity:
        task = await self._load_user_task(task_id)

        # Cancel any linked reminders before soft-deleting
        await self._reminders.cancel_task_reminders(task.activity_id)

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

    # ── Deal & task loading ──────────────────────────────────────────

    async def _load_deal(self, deal_id: UUID) -> CRMDeal:
        deal = await self.session.get(CRMDeal, deal_id)
        if not deal or deal.organisation_id != self.organisation_id:
            raise TaskWriteServiceError("Deal not found", 404)
        if deal.is_deleted:
            raise TaskWriteServiceError("Deal is deleted", 404)
        return deal

    async def _load_user_task(self, task_id: UUID) -> CRMActivity:
        task = await self.session.get(CRMActivity, task_id)
        if not task or task.organisation_id != self.organisation_id:
            raise TaskWriteServiceError("Task not found", 404)
        if task.is_deleted:
            raise TaskWriteServiceError("Task not found", 404)
        if task.activity_type not in _ALLOWED_TASK_TYPES:
            raise TaskWriteServiceError("Task not found", 404)
        # Authoritative read-only predicate: any task with integration_id set is a
        # CRM-synced record and cannot be edited via the user API. `source` is
        # informational only — it is nullable and was only backfilled once (in
        # migration 035), so it can drift from integration_id and MUST NOT be used
        # here. The response-layer `is_read_only` flag in TaskListItem/TaskDetail
        # mirrors this same predicate so the frontend stays in sync.
        if task.integration_id is not None:
            raise TaskWriteServiceError("External CRM tasks are read-only", 409)
        return task

    def _normalize_due_payload(self, payload: dict[str, Any]) -> tuple[datetime | None, bool, dict]:
        due_date = payload.get("due_date")
        is_all_day = bool(payload.get("is_all_day")) if "is_all_day" in payload else False
        if due_date is None:
            return None, False, {}

        timezone_name = (
            resolve_timezone_name(
                payload.get("due_timezone"),
                self.user_timezone,
                default=DEFAULT_TIMEZONE,
            )
            or DEFAULT_TIMEZONE
        )
        timezone = ZoneInfo(timezone_name)
        due = due_date if due_date.tzinfo else due_date.replace(tzinfo=timezone)
        due_utc = due.astimezone(UTC)

        properties: dict[str, Any] = {}
        if is_all_day:
            properties["due_timezone"] = timezone_name
            properties["due_local_date"] = due.astimezone(timezone).date().isoformat()

        return due_utc, is_all_day, properties

    async def _resolve_owner(
        self, user_id: UUID | None
    ) -> tuple[UUID | None, str | None, str | None]:
        if user_id is None:
            return None, None, None

        async with master_async_session_scope() as master_session:
            user = await master_session.get(User, user_id)
            if not user:
                raise TaskWriteServiceError("User not found", 404)
            if user.organisation_id != self.organisation_id:
                raise TaskWriteServiceError("User not in organisation", 403)

            crm_user_id: UUID | None = None
            try:
                user_service = InternalCRMUserService(
                    org_session=self.session,
                    master_session=master_session,
                    organisation_id=self.organisation_id,
                    actor_user_id=self.actor_user_id,
                )
                crm_user = await user_service.ensure_crm_user(user_id)
                crm_user_id = crm_user.user_id
            except InternalCRMUserServiceError:
                logger.debug("Could not resolve task assignee to crm_users", exc_info=True)

            return (
                crm_user_id,
                build_user_display_name(user),
                str(user.email),
            )

    @staticmethod
    def _normalize_enum(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, Enum):
            return str(value.value)
        return str(value)
