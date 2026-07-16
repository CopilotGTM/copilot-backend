"""Automation-safe CRM task writes for AI-generated deal actions.

This module owns deterministic create/complete logic for tasks that come from
non-interactive AI agents. It is intended to be called from a deal-scoped
automation path (for example, post-meeting summaries), not from direct user UI
edits.

The service writes only internal CRM tasks (`integration_id is None`) and never
updates CRM-synced records. That boundary is the key safety rule that keeps
automated tasks from overwriting external source-of-truth data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any, cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.master_tables import SupabaseUserStatus, User
from src.domain.models.organisation_tables.sync.calendar import CalendarAttendee, CalendarEvent
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivityPriority,
    CRMActivitySource,
    CRMActivityStatus,
    CRMActivityType,
    CRMTask,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.infra.database.sessions import master_async_session_scope
from src.infra.utils.timezone import DEFAULT_TIMEZONE, resolve_timezone_name
from src.service.crm.access.crm_user_service import build_user_display_name
from src.service.crm.activity_feed_projection_service import ActivityFeedProjectionService
from src.service.crm.tasks.reminder_service import TaskReminderService
from src.service.crm.utils.crm_user_identity import (
    CRMUserIdentityResolutionError,
    ensure_app_user_crm_identity,
)

_TASK_ACTIVITY_TYPES = {CRMActivityType.TASK.value, CRMActivityType.TODO.value}
_ALLOWED_PRIORITIES = {priority.value for priority in CRMActivityPriority}


class DealTaskAutomationError(Exception):
    """Raised when an automated task write attempt breaks a safety contract."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True)
class TaskWriteResult:
    """Result of an automated task create attempt."""

    task: CRMTask
    created: bool
    reason: str | None = None
    assignee_public_user_id: UUID | None = None
    assignee_timezone: str = DEFAULT_TIMEZONE
    reminder_at: datetime | None = None


class DealTaskAutomationService:
    """Deterministic write boundary for unattended AI-created deal tasks.

    The service receives an already-open organisation session and must enforce
    tenant isolation itself before any read/write is allowed.
    """

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self._projection = ActivityFeedProjectionService(session, organisation_id)
        self._reminders = TaskReminderService(session, organisation_id, actor_user_id=None)

    async def list_deal_tasks(self, deal_id: UUID) -> list[CRMTask]:
        """List non-deleted task-style rows for one deal in the current org.

        This is read-heavy and intentionally filters by `organisation_id` before
        `deal_id`, so one tenant's query can never match another tenant's
        rows even when IDs collide across organisations.
        """
        # Read path filters first by org and then by deal; this preserves tenant
        # isolation even if the caller sends a guessed UUID from another org.
        c = cast(Any, CRMTask).__table__.c
        stmt = (
            select(CRMTask)
            .where(
                c.organisation_id == self.organisation_id,
                c.deal_id == deal_id,
                c.task_metadata["task_type"].as_string().in_(list(_TASK_ACTIVITY_TYPES)),
                c.status.in_(
                    [
                        CRMActivityStatus.NOT_STARTED.value,
                        CRMActivityStatus.IN_PROGRESS.value,
                    ]
                ),
                or_(c.is_deleted.is_(False), c.is_deleted.is_(None)),
            )
            .order_by(c.created_at.desc(), c.task_id.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def create_task(
        self,
        *,
        deal_id: UUID,
        subject: str,
        body: str | None,
        assignee_name_or_email: str | None,
        fallback_assignee_name_or_emails: list[str] | None = None,
        source_event_id: UUID | None,
        source_association_id: UUID | None,
        source_summary_job_id: UUID | None,
        priority: str = CRMActivityPriority.NONE.value,
        due_date: datetime | None = None,
    ) -> TaskWriteResult:
        """Create an AI meeting task or return an idempotent existing task.

        This is the primary write entry point used by automation jobs after
        summarization or action extraction. The service keeps a narrow contract:
        normalize + validate input, resolve cross-org assignee hints, enforce
        idempotency, and persist only internal tasks.
        """
        # --- Step 1: validate caller-provided inputs ---
        cleaned_subject = subject.strip()
        if not cleaned_subject:
            raise DealTaskAutomationError("subject is required", 400)
        if priority not in _ALLOWED_PRIORITIES:
            raise DealTaskAutomationError("Invalid task priority", 400)

        # --- Step 2: load the requested deal inside this org boundary ---
        deal = await self._load_deal(deal_id)

        # --- Step 3: compute deterministic idempotency key for dedupe on retries ---
        idempotency_key = self._build_idempotency_key(
            deal_id=deal_id,
            source_event_id=source_event_id,
            source_association_id=source_association_id,
            subject=cleaned_subject,
        )

        # Serialize identical writes across Celery retries. Read-before-write is
        # not sufficient when two workers start before either transaction commits.
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:idempotency_key))"),
            {"idempotency_key": idempotency_key},
        )

        # Idempotency key is the contract for replay safety. Automation can
        # re-run after a transient crash; returning the first write prevents
        # duplicate internal tasks while keeping user-visible task IDs stable.
        existing = await self._find_existing_task(idempotency_key)
        if existing:
            public_user_id = self._metadata_public_user_id(existing)
            timezone_name = await self._user_timezone(public_user_id)
            return TaskWriteResult(
                existing,
                False,
                "duplicate",
                assignee_public_user_id=public_user_id,
                assignee_timezone=timezone_name,
                reminder_at=self._reminder_time(existing.due_at, timezone_name),
            )

        # --- Step 4: resolve assignee locally to avoid hard dependency on caller IDs ---
        allowed_assignee_emails = await self._allowed_assignee_emails(deal, source_event_id)
        (
            owner_user_id,
            public_user_id,
            owner_name,
            owner_email,
            assignee_timezone,
            assignee_resolution,
        ) = await self._resolve_assignee(
            assignee_name_or_email,
            fallback_assignee_name_or_emails or [],
            allowed_assignee_emails,
        )
        normalized_due_date = self._normalize_due_date(due_date, assignee_timezone)
        local_due_date = normalized_due_date.astimezone(ZoneInfo(assignee_timezone)).date()
        now = datetime.now(UTC)
        activity_id = uuid4()

        # Build a write-only, internal task row. `source` marks automation origin,
        # while `created_by_user_id=None` intentionally records that no user made
        # this write in-band.
        task_data: dict[str, Any] = {
            "task_id": activity_id,
            "organisation_id": self.organisation_id,
            "integration_id": None,
            "title": cleaned_subject,
            "description": body.strip() if body and body.strip() else None,
            "status": CRMActivityStatus.NOT_STARTED.value,
            "priority": priority,
            "due_date": local_due_date,
            "due_at": normalized_due_date,
            "is_all_day": False,
            "deal_id": deal.deal_id,
            "assigned_to_user_id": owner_user_id,
            "assigned_to_name": owner_name,
            "assigned_to_email": owner_email,
            "owner_user_id": owner_user_id,
            "owner_email": owner_email,
            "created_by_user_id": None,
            "source_created_at": now,
            "source_modified_at": now,
            "task_metadata": {
                "is_user_created": False,
                "created_via": "deal_task_agent",
                "source": CRMActivitySource.AI_MEETING.value,
                "task_type": CRMActivityType.TASK.value,
                "related_object_type": "DEAL",
                "related_local_id": str(deal.deal_id),
                "related_name": deal.name,
                "source_event_id": str(source_event_id) if source_event_id else None,
                "source_association_id": str(source_association_id)
                if source_association_id
                else None,
                "source_summary_job_id": str(source_summary_job_id)
                if source_summary_job_id
                else None,
                "idempotency_key": idempotency_key,
                "assignee_resolution": assignee_resolution,
                "assigned_to_public_user_id": str(public_user_id)
                if public_user_id
                else None,
                "due_timezone": assignee_timezone,
                "due_local_date": local_due_date.isoformat(),
            },
        }
        task = CRMTask.model_validate(task_data)
        self.session.add(task)
        await self.session.flush()
        await self.session.refresh(task)
        await self._projection.upsert_task(task)
        return TaskWriteResult(
            task,
            True,
            assignee_public_user_id=public_user_id,
            assignee_timezone=assignee_timezone,
            reminder_at=self._reminder_time(normalized_due_date, assignee_timezone),
        )

    async def complete_task(
        self,
        task_id: UUID,
        *,
        deal_id: UUID,
        completion_reason: str,
    ) -> CRMTask:
        """Mark a same-org, same-deal internal task as completed.

        The method intentionally refuses:
        - cross-org reads
        - wrong-deal writes
        - non-task activity types
        - CRM-synced activities

        That prevents a summary/job worker from accidentally mutating unrelated
        tenant data or external read-only task records.
        """
        # --- Step 1: read and validate tenant-bound ownership and type ---
        task = await self.session.get(CRMTask, task_id)
        if not task or task.organisation_id != self.organisation_id:
            raise DealTaskAutomationError("Task not found", 404)
        if task.is_deleted:
            raise DealTaskAutomationError("Task not found", 404)
        if task.deal_id != deal_id:
            raise DealTaskAutomationError("Task not found", 404)
        if task.activity_type not in _TASK_ACTIVITY_TYPES:
            raise DealTaskAutomationError("Task not found", 404)
        if task.integration_id is not None:
            raise DealTaskAutomationError("External CRM tasks are read-only", 409)

        # --- Step 2: transition internal state and stamp audit-like task metadata ---
        now = datetime.now(UTC)
        task.status = CRMActivityStatus.COMPLETED.value
        task.completed_at = now
        task.source_modified_at = now
        task_metadata = task.task_metadata or {}
        task_metadata["completed_via"] = "deal_task_agent"
        task_metadata["completion_reason"] = completion_reason
        task.task_metadata = task_metadata
        await self.session.flush()
        await self.session.refresh(task)
        await self._projection.upsert_task(task)
        # Completion and reminder cancellation are one transaction so a retry
        # cannot leave a completed task with an active user notification.
        await self._reminders.cancel_task_reminders(task.task_id)
        return task

    async def _load_deal(self, deal_id: UUID) -> CRMDeal:
        """Load one org-scoped deal, guarding soft-delete and tenant mismatch."""
        deal = await self.session.get(CRMDeal, deal_id)
        if not deal or deal.organisation_id != self.organisation_id:
            raise DealTaskAutomationError("Deal not found", 404)
        if deal.is_deleted:
            raise DealTaskAutomationError("Deal is deleted", 404)
        return deal

    async def _find_existing_task(self, idempotency_key: str) -> CRMTask | None:
        """Return an existing task by idempotency key for this organisation."""
        c = cast(Any, CRMTask).__table__.c

        # Idempotency only lives inside the target org; external key fragments are
        # useful for dedupe but never trusted as the only tenancy boundary.
        stmt = select(CRMTask).where(
            c.organisation_id == self.organisation_id,
            c.task_metadata["idempotency_key"].as_string() == idempotency_key,
            or_(c.is_deleted.is_(False), c.is_deleted.is_(None)),
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def _resolve_assignee(
        self,
        value: str | None,
        fallbacks: list[str],
        allowed_emails: set[str],
    ) -> tuple[UUID | None, UUID | None, str | None, str | None, str, dict[str, Any]]:
        """Resolve ordered owner hints to the first active internal app user."""
        candidates = [candidate.strip() for candidate in [value, *fallbacks] if candidate and candidate.strip()]
        if not candidates:
            return None, None, None, None, DEFAULT_TIMEZONE, {"status": "not_provided"}

        # Public app IDs and tenant CRM user IDs are different namespaces. Resolve
        # both in one boundary so task foreign keys always receive the CRM identity.
        async with master_async_session_scope() as master_session:
            for lookup in candidates:
                user = await self._resolve_user(master_session, lookup)
                if not user:
                    continue
                if str(user.email).lower() not in allowed_emails:
                    continue
                try:
                    crm_user = await ensure_app_user_crm_identity(
                        org_session=self.session,
                        master_session=master_session,
                        organisation_id=self.organisation_id,
                        app_user_id=user.user_id,
                        actor_user_id=user.user_id,
                    )
                except CRMUserIdentityResolutionError as exc:
                    raise DealTaskAutomationError(exc.message, exc.status_code) from exc

                display_name = build_user_display_name(user) or str(user.email)
                timezone_name = (
                    resolve_timezone_name(user.timezone, default=DEFAULT_TIMEZONE)
                    or DEFAULT_TIMEZONE
                )
                return (
                    crm_user.user_id,
                    user.user_id,
                    display_name,
                    str(user.email),
                    timezone_name,
                    {"status": "resolved", "input": lookup, "candidates": candidates},
                )

        return (
            None,
            None,
            None,
            None,
            DEFAULT_TIMEZONE,
            {"status": "unresolved", "candidates": candidates},
        )

    async def _allowed_assignee_emails(
        self,
        deal: CRMDeal,
        source_event_id: UUID | None,
    ) -> set[str]:
        """Return deal-owner and exact-meeting participant emails eligible for assignment."""
        allowed = {deal.owner_email.lower()} if deal.owner_email else set()
        if not source_event_id:
            return allowed

        event_c = cast(Any, CalendarEvent).__table__.c
        event_stmt = select(CalendarEvent).where(
            event_c.organisation_id == self.organisation_id,
            event_c.event_id == source_event_id,
        )
        event = (await self.session.execute(event_stmt)).scalar_one_or_none()
        if not event:
            return allowed
        if event.organiser_email:
            allowed.add(event.organiser_email.lower())

        # Calendar attendees have no organisation column, so the org-scoped
        # event lookup above is the trust boundary before following its ID.
        attendee_stmt = select(col(CalendarAttendee.email)).where(
            col(CalendarAttendee.event_id) == source_event_id
        )
        attendee_emails = (await self.session.execute(attendee_stmt)).scalars().all()
        allowed.update(email.lower() for email in attendee_emails if email)
        return allowed

    async def _resolve_user(self, master_session: AsyncSession, lookup: str) -> User | None:
        """Find a user by exact email or normalized display-name match."""
        normalized_lookup = lookup.lower().strip()
        stmt = select(User).where(
            col(User.organisation_id) == self.organisation_id,
            col(User.status) == SupabaseUserStatus.ACTIVE,
        )
        if "@" in normalized_lookup:
            # Email is treated as the highest-fidelity key; case-insensitive exact
            # match keeps false positives low and avoids broad scans.
            stmt = stmt.where(func.lower(col(User.email)) == normalized_lookup)
            result = await master_session.execute(stmt)
            return result.scalar_one_or_none()

        # Name search is intentionally fuzzy to support nickname/casing variations in
        # user-entered notes. If more than one match exists, do not auto-resolve.
        result = await master_session.execute(stmt)
        normalized_name = self._normalize_name(lookup)
        matches = [
            user
            for user in result.scalars().all()
            if self._normalize_name(build_user_display_name(user)) == normalized_name
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def _build_idempotency_key(
        self,
        *,
        deal_id: UUID,
        source_event_id: UUID | None,
        source_association_id: UUID | None,
        subject: str,
    ) -> str:
        """Build the replay-prevention key from org, deal, source and subject."""
        source_id = source_association_id or source_event_id or "deal"
        normalized_subject = self._normalize_subject(subject)
        return f"{self.organisation_id}:{deal_id}:{source_id}:{normalized_subject}"

    def _normalize_due_date(self, due_date: datetime | None, timezone_name: str) -> datetime:
        """Return an aware UTC due time, defaulting to next weekday at 5 PM local."""
        timezone = ZoneInfo(timezone_name)
        if due_date:
            localized = due_date if due_date.tzinfo else due_date.replace(tzinfo=timezone)
            return localized.astimezone(UTC)

        local_due = datetime.now(timezone) + timedelta(days=1)
        # copilotgtm: v1 skips weekends, not regional holidays; add a holiday
        # calendar only when organisations can configure one.
        while local_due.weekday() >= 5:
            local_due += timedelta(days=1)
        return datetime.combine(local_due.date(), time(hour=17), timezone).astimezone(UTC)

    @staticmethod
    def _reminder_time(due_at: datetime | None, timezone_name: str) -> datetime | None:
        """Schedule the single task reminder at 10 AM local on the due date."""
        if due_at is None:
            return None
        timezone = ZoneInfo(timezone_name)
        due_local = due_at.astimezone(timezone)
        trigger = datetime.combine(due_local.date(), time(hour=10), timezone).astimezone(UTC)
        now = datetime.now(UTC)
        if trigger > now:
            return trigger
        if due_at > now + timedelta(minutes=5):
            return min(due_at - timedelta(hours=1), now + timedelta(minutes=5))
        return None

    @staticmethod
    def _metadata_public_user_id(task: CRMTask) -> UUID | None:
        value = (task.task_metadata or {}).get("assigned_to_public_user_id")
        try:
            return UUID(str(value)) if value else None
        except ValueError:
            return None

    async def _user_timezone(self, user_id: UUID | None) -> str:
        if not user_id:
            return DEFAULT_TIMEZONE
        async with master_async_session_scope() as master_session:
            user = await master_session.get(User, user_id)
        if user and user.organisation_id != self.organisation_id:
            user = None
        return (
            resolve_timezone_name(
                user.timezone if user else None,
                default=DEFAULT_TIMEZONE,
            )
            or DEFAULT_TIMEZONE
        )

    @staticmethod
    def _normalize_subject(subject: str) -> str:
        # Normalize punctuation and case so equivalent summaries map to one idempotent key.
        return re.sub(r"[^a-z0-9]+", " ", subject.lower()).strip()

    @staticmethod
    def _normalize_name(name: str | None) -> str:
        """Normalize display-name-like input for deterministic in-memory matching."""
        if not name:
            return ""
        return " ".join(name.lower().split())
