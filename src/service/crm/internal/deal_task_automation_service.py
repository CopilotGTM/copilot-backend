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
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
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
from src.service.crm.access.crm_user_service import build_user_display_name

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

    task: CRMActivity
    created: bool
    reason: str | None = None


class DealTaskAutomationService:
    """Deterministic write boundary for unattended AI-created deal tasks.

    The service receives an already-open organisation session and must enforce
    tenant isolation itself before any read/write is allowed.
    """

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id

    async def list_deal_tasks(self, deal_id: UUID) -> list[CRMActivity]:
        """List non-deleted task-style rows for one deal in the current org.

        This is read-heavy and intentionally filters by `organisation_id` before
        `deal_id`, so one tenant's query can never match another tenant's
        rows even when IDs collide across organisations.
        """
        # Read path filters first by org and then by deal; this preserves tenant
        # isolation even if the caller sends a guessed UUID from another org.
        c = cast(Any, CRMActivity).__table__.c
        stmt = (
            select(CRMActivity)
            .where(
                c.organisation_id == self.organisation_id,
                c.deal_id == deal_id,
                c.activity_type.in_(list(_TASK_ACTIVITY_TYPES)),
                or_(c.is_deleted.is_(False), c.is_deleted.is_(None)),
            )
            .order_by(c.created_at.desc(), c.activity_id.desc())
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

        # Idempotency key is the contract for replay safety. Automation can
        # re-run after a transient crash; returning the first write prevents
        # duplicate internal tasks while keeping user-visible task IDs stable.
        existing = await self._find_existing_task(idempotency_key)
        if existing:
            return TaskWriteResult(existing, False, "duplicate")

        # --- Step 4: resolve assignee locally to avoid hard dependency on caller IDs ---
        owner_user_id, owner_name, owner_email, assignee_resolution = await self._resolve_assignee(
            assignee_name_or_email
        )
        now = datetime.now(UTC)
        activity_id = uuid4()

        # Build a write-only, internal task row. `source` marks automation origin,
        # while `created_by_user_id=None` intentionally records that no user made
        # this write in-band.
        task_data: dict[str, Any] = {
            "activity_id": activity_id,
            "organisation_id": self.organisation_id,
            "integration_id": None,
            "external_id": str(activity_id),
            "activity_type": CRMActivityType.TASK.value,
            "subject": cleaned_subject,
            "body": body.strip() if body and body.strip() else None,
            "status": CRMActivityStatus.NOT_STARTED.value,
            "priority": priority,
            "due_date": due_date,
            "deal_id": deal.deal_id,
            "deal_external_id": deal.external_id,
            "related_object_type": CRMActivityObjectType.DEAL.value,
            "related_external_id": deal.external_id,
            "related_local_id": deal.deal_id,
            "related_name": deal.name,
            "owner_external_id": str(owner_user_id) if owner_user_id else None,
            "owner_name": owner_name,
            "owner_email": owner_email,
            "source": CRMActivitySource.AI_MEETING.value,
            "created_by_user_id": None,
            "source_created_at": now,
            "source_modified_at": now,
            "synced_at": now,
            "last_seen_at": now,
            "properties": {
                "is_user_created": False,
                "created_via": "deal_task_agent",
                "source_event_id": str(source_event_id) if source_event_id else None,
                "source_association_id": str(source_association_id)
                if source_association_id
                else None,
                "source_summary_job_id": str(source_summary_job_id)
                if source_summary_job_id
                else None,
                "idempotency_key": idempotency_key,
                "assignee_resolution": assignee_resolution,
            },
        }
        task = CRMActivity.model_validate(task_data)
        self.session.add(task)
        await self.session.flush()
        await self.session.refresh(task)
        return TaskWriteResult(task, True)

    async def complete_task(
        self,
        task_id: UUID,
        *,
        deal_id: UUID,
        completion_reason: str,
    ) -> CRMActivity:
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
        task = await self.session.get(CRMActivity, task_id)
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
        task.completed_date = now
        task.source_modified_at = now
        task.synced_at = now
        task.last_seen_at = now
        properties = task.properties or {}
        properties["completed_via"] = "deal_task_agent"
        properties["completion_reason"] = completion_reason
        task.properties = properties
        await self.session.flush()
        await self.session.refresh(task)
        return task

    async def _load_deal(self, deal_id: UUID) -> CRMDeal:
        """Load one org-scoped deal, guarding soft-delete and tenant mismatch."""
        deal = await self.session.get(CRMDeal, deal_id)
        if not deal or deal.organisation_id != self.organisation_id:
            raise DealTaskAutomationError("Deal not found", 404)
        if deal.is_deleted:
            raise DealTaskAutomationError("Deal is deleted", 404)
        return deal

    async def _find_existing_task(self, idempotency_key: str) -> CRMActivity | None:
        """Return an existing task by idempotency key for this organisation."""
        c = cast(Any, CRMActivity).__table__.c

        # Idempotency only lives inside the target org; external key fragments are
        # useful for dedupe but never trusted as the only tenancy boundary.
        stmt = select(CRMActivity).where(
            c.organisation_id == self.organisation_id,
            c.properties["idempotency_key"].as_string() == idempotency_key,
            or_(c.is_deleted.is_(False), c.is_deleted.is_(None)),
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def _resolve_assignee(
        self, value: str | None
    ) -> tuple[UUID | None, str | None, str | None, dict[str, Any]]:
        """Resolve a user-provided name/email to an org user when possible."""
        # Empty or whitespace input is an explicit no-op for automation callers;
        # this avoids accidental blocking when summaries do not name an owner.
        if not value or not value.strip():
            return None, None, None, {"status": "not_provided"}

        lookup = value.strip()

        # User lookup is done against public users, so the master session is required.
        # Keep this method narrow: only user identity metadata is enriched here, all
        # task write decisions remain in the org session.
        async with master_async_session_scope() as master_session:
            user = await self._resolve_user(master_session, lookup)

        if not user:
            return None, None, None, {"status": "unresolved", "input": lookup}

        display_name = build_user_display_name(user) or str(user.email)
        return (
            user.user_id,
            display_name,
            str(user.email),
            {"status": "resolved", "input": lookup},
        )

    async def _resolve_user(self, master_session: AsyncSession, lookup: str) -> User | None:
        """Find a user by exact email or normalized display-name match."""
        normalized_lookup = lookup.lower().strip()
        stmt = select(User).where(col(User.organisation_id) == self.organisation_id)
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
