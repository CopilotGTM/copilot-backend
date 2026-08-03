"""Task write service for user-created CRM task source rows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.master_tables import User
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivityPriority,
    CRMActivityType,
    CRMTask,
)
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMParticipantType,
)
from src.infra.database.sessions import master_async_session_scope
from src.infra.utils.logger import get_logger
from src.infra.utils.timezone import DEFAULT_TIMEZONE, resolve_timezone_name
from src.service.crm.access.crm_user_service import build_user_display_name
from src.service.crm.access.record_access_policy import (
    CRMAccessActor,
    CRMRecordAccessError,
    DealAccessPolicy,
)
from src.service.crm.activity_feed_projection_service import ActivityFeedProjectionService
from src.service.crm.participants.contracts import ParticipantServiceError
from src.service.crm.participants.participant_write_service import ParticipantWriteService
from src.service.crm.tasks.contracts import TaskReadRecord, TaskWriteServiceError
from src.service.crm.tasks.notification_service import TaskNotificationService
from src.service.crm.tasks.read_service import TaskReadService
from src.service.crm.tasks.reminder_service import TaskReminderService
from src.service.crm.utils.crm_user_identity import (
    CRMUserIdentityResolutionError,
    ensure_app_user_crm_identity,
)

logger = get_logger("task_write_service")

_ALLOWED_TASK_TYPES = {CRMActivityType.TASK.value, CRMActivityType.TODO.value}
_ALLOWED_STATUSES = {"NOT_STARTED", "IN_PROGRESS", "COMPLETED", "CANCELLED"}
_ALLOWED_PRIORITIES = {priority.value for priority in CRMActivityPriority}


@dataclass(frozen=True)
class _PendingAssignmentNotification:
    task: CRMTask
    deal: CRMDeal | None
    is_update: bool


class TaskWriteService:
    """Write operations for user-created tasks."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        actor_user_id: UUID,
        *,
        user_timezone: str | None = None,
        access_actor: CRMAccessActor | None = None,
        master_session: AsyncSession | None = None,
    ):
        self.session = session
        self.organisation_id = organisation_id
        self.actor_user_id = actor_user_id
        self.user_timezone = resolve_timezone_name(user_timezone)
        self.access_actor = access_actor
        self.master_session = master_session
        self._access = DealAccessPolicy(session, organisation_id, access_actor)
        self._reminders = TaskReminderService(session, organisation_id, actor_user_id)
        self._notifications = TaskNotificationService(session, organisation_id, actor_user_id)
        self._projection = ActivityFeedProjectionService(session, organisation_id)
        self._pending_assignment_notifications: list[_PendingAssignmentNotification] = []

    async def close(self) -> None:
        """Close the owned session when dependency cleanup calls the service."""
        try:
            await self.session.close()
        except Exception:
            logger.debug("TaskWriteService close exception", exc_info=True)

    async def create_task(self, payload: dict[str, Any], *, deal_id: UUID | None = None) -> CRMTask:
        """Create one user-owned task row and queue post-commit assignee notification."""
        deal = await self._load_deal(deal_id) if deal_id else None
        if deal is not None:
            await self._require_deal_access(deal.deal_id, "collaborate")

        if payload.get("external_assignee_email") or payload.get("external_assignee_name"):
            raise TaskWriteServiceError("Tasks can only be assigned to internal members", 400)

        task_type = self._normalize_enum(payload.get("task_type")) or CRMActivityType.TASK.value
        if task_type not in _ALLOWED_TASK_TYPES:
            raise TaskWriteServiceError("task_type must be TASK or TODO", 400)

        status = self._normalize_enum(payload.get("status")) or "NOT_STARTED"
        if status not in _ALLOWED_STATUSES:
            raise TaskWriteServiceError("Invalid task status", 400)

        priority = self._normalize_enum(payload.get("priority")) or CRMActivityPriority.NONE.value
        if priority not in _ALLOWED_PRIORITIES:
            raise TaskWriteServiceError("Invalid task priority", 400)

        owner_user_id = (
            payload.get("owner_user_id") if "owner_user_id" in payload else self.actor_user_id
        )
        owner_id, owner_name, owner_email = await self._resolve_owner(owner_user_id)
        if deal is not None and owner_user_id is not None:
            await self._ensure_deal_participant(deal.deal_id, owner_user_id)
        actor_crm_user_id, _, _ = await self._resolve_owner(self.actor_user_id)
        due_at, due_date, is_all_day, due_metadata = self._normalize_due_payload(payload)

        now = datetime.now(UTC)
        completed_at = payload.get("completed_date")
        completed_by_user_id = None
        if status != "COMPLETED":
            completed_at = None
        else:
            completed_at = completed_at or now
            completed_by_user_id, _, _ = await self._resolve_owner(self.actor_user_id)

        external_assignee_email = None
        external_assignee_name = None

        metadata = {
            "is_user_created": True,
            "created_via": "api",
            "source": "MANUAL",
            "task_type": task_type,
            "actor_user_id": str(self.actor_user_id),
            **due_metadata,
        }
        if owner_user_id:
            metadata["assigned_to_public_user_id"] = str(owner_user_id)
        if external_assignee_email or external_assignee_name:
            metadata["assignee_kind"] = "external"

        task = CRMTask(
            task_id=uuid4(),
            organisation_id=self.organisation_id,
            integration_id=None,
            deal_id=deal.deal_id if deal else None,
            title=self._task_title_from_payload(payload),
            description=payload.get("body"),
            status=status,
            priority=priority,
            due_date=due_date,
            due_at=due_at,
            is_all_day=is_all_day,
            reminder_at=payload.get("reminder_date"),
            completed_at=completed_at,
            assigned_to_user_id=None if external_assignee_email else owner_id,
            assigned_to_email=external_assignee_email or owner_email,
            assigned_to_name=external_assignee_name or owner_name,
            created_by_user_id=actor_crm_user_id,
            owner_user_id=owner_id,
            owner_email=owner_email,
            completed_by_user_id=completed_by_user_id,
            source_created_at=now,
            source_modified_at=now,
            task_metadata=metadata,
        )

        self.session.add(task)
        await self.session.flush()
        await self.session.refresh(task)
        await self._projection.upsert_task(task)

        reminder_cfg = payload.get("reminder")
        if reminder_cfg and reminder_cfg.get("enabled"):
            await self._reminders.create_task_reminder(task, reminder_cfg)

        if task.assigned_to_email:
            self._queue_assignment_notification(task, deal, is_update=False)

        return task

    async def update_task(self, task_id: UUID, payload: dict[str, Any]) -> CRMTask:
        """Update one editable task row and queue assignment/update notification after commit."""
        if not payload:
            raise TaskWriteServiceError("No fields to update", 400)

        task = await self._load_user_task(task_id)
        await self._require_task_update(task, payload)
        updated = False
        assignment_changed = False

        if "subject" in payload:
            task.title = self._task_title_from_payload(
                {"subject": payload.get("subject"), "body": task.description}
            )
            updated = True
        if "body" in payload:
            task.description = payload.get("body")
            updated = True

        metadata = dict(task.task_metadata or {})
        if "task_type" in payload:
            task_type = self._normalize_enum(payload.get("task_type"))
            if task_type is None:
                raise TaskWriteServiceError("task_type cannot be null", 400)
            if task_type not in _ALLOWED_TASK_TYPES:
                raise TaskWriteServiceError("task_type must be TASK or TODO", 400)
            metadata["task_type"] = task_type
            updated = True

        if "priority" in payload:
            priority = self._normalize_enum(payload.get("priority"))
            if priority is None:
                raise TaskWriteServiceError("priority cannot be null", 400)
            if priority not in _ALLOWED_PRIORITIES:
                raise TaskWriteServiceError("Invalid task priority", 400)
            task.priority = priority
            updated = True

        if "due_date" in payload:
            due_at, due_date, is_all_day, due_metadata = self._normalize_due_payload(payload)
            task.due_at = due_at
            task.due_date = due_date
            task.is_all_day = is_all_day
            metadata.pop("due_local_date", None)
            metadata.pop("due_timezone", None)
            metadata.update(due_metadata)
            updated = True
        elif "is_all_day" in payload:
            task.is_all_day = bool(payload.get("is_all_day"))
            if task.is_all_day:
                if task.due_date is None:
                    raise TaskWriteServiceError("due_date is required for all-day tasks", 400)
                task.due_at = None
                metadata["due_timezone"] = self.user_timezone or DEFAULT_TIMEZONE
                metadata["due_local_date"] = task.due_date.isoformat()
            else:
                if task.due_date is not None and task.due_at is None:
                    raise TaskWriteServiceError(
                        "due_date is required when changing an all-day task to a timed task",
                        400,
                    )
                metadata.pop("due_local_date", None)
                metadata.pop("due_timezone", None)
            updated = True

        if "reminder_date" in payload:
            task.reminder_at = payload.get("reminder_date")
            updated = True

        if "owner_user_id" in payload:
            owner_id, owner_name, owner_email = await self._resolve_owner(
                payload.get("owner_user_id")
            )
            if task.deal_id and payload.get("owner_user_id"):
                await self._ensure_deal_participant(task.deal_id, payload["owner_user_id"])
            task.assigned_to_user_id = owner_id
            task.assigned_to_name = owner_name
            task.assigned_to_email = owner_email
            task.owner_user_id = owner_id
            task.owner_email = owner_email
            metadata.pop("assignee_kind", None)
            if payload.get("owner_user_id"):
                metadata["assigned_to_public_user_id"] = str(payload["owner_user_id"])
            else:
                metadata.pop("assigned_to_public_user_id", None)
            updated = True
            assignment_changed = True

        if "external_assignee_email" in payload or "external_assignee_name" in payload:
            raise TaskWriteServiceError("Tasks can only be assigned to internal members", 400)

        if "deal_id" in payload:
            new_deal_id = payload.get("deal_id")
            if new_deal_id:
                deal = await self._load_deal(new_deal_id)
                await self._require_deal_access(deal.deal_id, "collaborate")
                task.deal_id = deal.deal_id
            else:
                task.deal_id = None
            updated = True

        status_in_payload = "status" in payload
        completed_in_payload = "completed_date" in payload
        status = self._normalize_enum(payload.get("status")) if status_in_payload else task.status
        if status_in_payload and status is None:
            raise TaskWriteServiceError("status cannot be null", 400)
        if status and status not in _ALLOWED_STATUSES:
            raise TaskWriteServiceError("Invalid task status", 400)
        if completed_in_payload and payload.get("completed_date") and status != "COMPLETED":
            status = "COMPLETED"
        if status is None:
            raise TaskWriteServiceError("status cannot be null", 400)

        if status_in_payload or (
            completed_in_payload and payload.get("completed_date") is not None
        ):
            task.status = status
            updated = True

        now = datetime.now(UTC)
        if status == "COMPLETED":
            if completed_in_payload and payload.get("completed_date") is not None:
                task.completed_at = payload.get("completed_date")
                updated = True
            elif (completed_in_payload and payload.get("completed_date") is None) or (
                task.completed_at is None
            ):
                task.completed_at = now
                updated = True
            if task.completed_by_user_id is None:
                task.completed_by_user_id, _, _ = await self._resolve_owner(self.actor_user_id)
        elif completed_in_payload or status_in_payload:
            task.completed_at = None
            task.completed_by_user_id = None
            updated = True

        if not updated:
            raise TaskWriteServiceError("No fields to update", 400)

        if not task.title and not task.description:
            raise TaskWriteServiceError("subject or body is required", 400)

        metadata["updated_via"] = "api"
        task.task_metadata = metadata
        task.source_modified_at = now

        await self.session.flush()
        await self.session.refresh(task)
        await self._projection.upsert_task(task)

        reminder_cfg = payload.get("reminder")
        if reminder_cfg is not None:
            if reminder_cfg.get("enabled"):
                existing = await self._reminders.get_active_reminder(task.task_id)
                if existing:
                    await self._reminders.update_task_reminder(existing, reminder_cfg)
                else:
                    await self._reminders.create_task_reminder(task, reminder_cfg)
            else:
                await self._reminders.cancel_task_reminders(task.task_id)

        if task.assigned_to_email:
            if assignment_changed:
                assignment_deal = await self._load_deal(task.deal_id) if task.deal_id else None
                self._queue_assignment_notification(task, assignment_deal, is_update=False)
            elif updated:
                update_deal = await self._load_deal(task.deal_id) if task.deal_id else None
                self._queue_assignment_notification(task, update_deal, is_update=True)

        return task

    async def delete_task(self, task_id: UUID) -> CRMTask:
        """Soft-delete one editable task and cancel active reminders."""
        task = await self._load_user_task(task_id)
        await self._require_task_control(task)
        if task.status in {"COMPLETED", "CANCELLED"} and not (
            self.access_actor and self.access_actor.is_admin
        ):
            raise TaskWriteServiceError("Completed or cancelled tasks are immutable", 409)

        await self._reminders.cancel_task_reminders(task.task_id)

        now = datetime.now(UTC)
        task.is_deleted = True
        task.source_modified_at = now
        metadata = dict(task.task_metadata or {})
        metadata["deleted_via"] = "api"
        task.task_metadata = metadata

        await self.session.flush()
        await self.session.refresh(task)
        await self._projection.soft_delete_activity_projection(task.task_id)

        return task

    async def read_task_record(self, task_id: UUID) -> TaskReadRecord:
        """Reload a task through the read service so API presenters get enriched fields."""
        record = await TaskReadService(self.session, self.organisation_id).get_task_by_id(task_id)
        if record is None:
            raise TaskWriteServiceError("Task not found", 404)
        return record

    async def send_pending_assignment_notifications(self) -> None:
        """Send queued assignment emails after the caller commits the task write."""
        pending = self._pending_assignment_notifications
        self._pending_assignment_notifications = []
        for notification in pending:
            await self._notifications.send_assignment_email(
                notification.task,
                notification.deal,
                is_update=notification.is_update,
            )

    def _queue_assignment_notification(
        self, task: CRMTask, deal: CRMDeal | None, *, is_update: bool
    ) -> None:
        self._pending_assignment_notifications.append(
            _PendingAssignmentNotification(task=task, deal=deal, is_update=is_update)
        )

    async def _load_deal(self, deal_id: UUID) -> CRMDeal:
        deal = await self.session.get(CRMDeal, deal_id)
        if not deal or deal.organisation_id != self.organisation_id:
            raise TaskWriteServiceError("Deal not found", 404)
        if deal.is_deleted:
            raise TaskWriteServiceError("Deal is deleted", 404)
        return deal

    async def _load_user_task(self, task_id: UUID) -> CRMTask:
        task = await self.session.get(CRMTask, task_id)
        if not task or task.organisation_id != self.organisation_id:
            raise TaskWriteServiceError("Task not found", 404)
        if task.is_deleted:
            raise TaskWriteServiceError("Task not found", 404)
        if task.integration_id is not None:
            raise TaskWriteServiceError("External CRM tasks are read-only", 409)
        return task

    async def _require_deal_access(self, deal_id: UUID, level: str) -> None:
        try:
            await self._access.require_deal(deal_id, level)  # type: ignore[arg-type]
        except CRMRecordAccessError as exc:
            raise TaskWriteServiceError(exc.message, exc.status_code) from exc

    async def _require_task_update(self, task: CRMTask, payload: dict[str, Any]) -> None:
        actor = self.access_actor
        if actor is None:
            raise TaskWriteServiceError("Task not found", 404)

        metadata = task.task_metadata or {}
        is_creator = task.created_by_user_id == actor.user_id or metadata.get(
            "actor_user_id"
        ) == str(actor.user_id)
        is_assignee = task.assigned_to_user_id == actor.user_id or metadata.get(
            "assigned_to_public_user_id"
        ) == str(actor.user_id)
        control_fields = {
            "owner_user_id",
            "deal_id",
            "external_assignee_email",
            "external_assignee_name",
        } & set(payload)

        if task.deal_id is None:
            if not (actor.is_admin or is_creator or is_assignee):
                raise TaskWriteServiceError("Task not found", 404)
            if control_fields and not (actor.is_admin or is_creator):
                raise TaskWriteServiceError("Only the task creator may reassign or relink it", 403)
        elif not actor.is_admin:
            try:
                _deal, decision = await self._access.require_deal(task.deal_id, "view")
            except CRMRecordAccessError as exc:
                raise TaskWriteServiceError(exc.message, exc.status_code) from exc
            if not decision.can_control:
                if not is_assignee:
                    raise TaskWriteServiceError(
                        "Only the deal owner or task assignee may update it", 403
                    )
                if control_fields:
                    raise TaskWriteServiceError(
                        "Only the deal owner may reassign or relink a task", 403
                    )

        if task.status in {"COMPLETED", "CANCELLED"} and not (actor.is_admin):
            raise TaskWriteServiceError("Completed or cancelled tasks are immutable", 409)

    async def _require_task_control(self, task: CRMTask) -> None:
        actor = self.access_actor
        if actor is None:
            raise TaskWriteServiceError("Task not found", 404)
        if actor.is_admin:
            return
        if task.deal_id:
            await self._require_deal_access(task.deal_id, "control")
            return
        if task.created_by_user_id != actor.user_id and (task.task_metadata or {}).get(
            "actor_user_id"
        ) != str(actor.user_id):
            raise TaskWriteServiceError("Task not found", 404)

    async def _ensure_deal_participant(self, deal_id: UUID, user_id: UUID) -> None:
        """Add an internal task assignee to the deal when not already participating."""
        c = CRMDealParticipant
        stmt = select(CRMDealParticipant.participant_id).where(
            col(c.organisation_id) == self.organisation_id,
            col(c.deal_id) == deal_id,
            col(c.participant_type) == CRMParticipantType.USER.value,
            col(c.related_local_id) == user_id,
            col(c.is_deleted).is_(False),
        )
        if (await self.session.execute(stmt)).scalar_one_or_none() is not None:
            return
        if self.master_session is None:
            raise TaskWriteServiceError("Cannot add task assignee to deal", 500)
        writer = ParticipantWriteService(
            self.session,
            self.organisation_id,
            self.actor_user_id,
            self.master_session,
        )
        try:
            await writer.add_participants(
                deal_id,
                [{"participant_type": "user", "related_id": user_id}],
            )
        except ParticipantServiceError as exc:
            raise TaskWriteServiceError(exc.message, exc.status_code) from exc

    def _normalize_due_payload(
        self, payload: dict[str, Any]
    ) -> tuple[datetime | None, date | None, bool, dict[str, Any]]:
        due_date = payload.get("due_date")
        is_all_day = bool(payload.get("is_all_day")) if "is_all_day" in payload else False
        if due_date is None:
            return None, None, False, {}

        timezone_name = (
            resolve_timezone_name(self.user_timezone, default=DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE
        )
        timezone = ZoneInfo(timezone_name)
        due = due_date if due_date.tzinfo else due_date.replace(tzinfo=timezone)
        due_utc = due.astimezone(UTC)

        metadata: dict[str, Any] = {}
        local_date = due.astimezone(timezone).date()
        if is_all_day:
            metadata["due_timezone"] = timezone_name
            metadata["due_local_date"] = local_date.isoformat()
            return None, local_date, True, metadata

        return due_utc, local_date, False, metadata

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

            try:
                crm_user = await ensure_app_user_crm_identity(
                    org_session=self.session,
                    master_session=master_session,
                    organisation_id=self.organisation_id,
                    app_user_id=user_id,
                    actor_user_id=self.actor_user_id,
                )
            except CRMUserIdentityResolutionError as exc:
                raise TaskWriteServiceError(exc.message, exc.status_code) from exc

            return (
                crm_user.user_id,
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

    @staticmethod
    def _task_title_from_payload(payload: dict[str, Any]) -> str:
        subject = payload.get("subject")
        if isinstance(subject, str) and subject.strip():
            return subject.strip()
        body = payload.get("body")
        if isinstance(body, str):
            compact = " ".join(body.split())
            if compact:
                return compact[:80].rstrip()
        return "Untitled task"
