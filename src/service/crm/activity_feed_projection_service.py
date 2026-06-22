"""Maintain crm_activities feed projections for CRM source rows."""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivity,
    CRMActivityLink,
    CRMActivityObjectType,
    CRMActivityType,
    CRMNote,
    CRMTask,
)


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _compact_preview(value: str | None, *, limit: int = 1000) -> str | None:
    if not value:
        return None
    compact = " ".join(value.split())
    return compact[:limit].rstrip() if compact else None


def note_title_or_preview(note: CRMNote, *, limit: int = 30) -> str | None:
    if note.title and note.title.strip():
        return note.title.strip()
    return _compact_preview(note.body, limit=limit)


class ActivityFeedProjectionService:
    """Upsert feed-card rows and object links from canonical CRM source tables."""

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id

    async def upsert_task(self, task: CRMTask) -> None:
        due_date_at = (
            datetime.combine(task.due_date, time.min, tzinfo=UTC) if task.due_date else None
        )
        occurred_at = (
            task.due_at or due_date_at or task.source_created_at or task.created_at or _now_utc()
        )
        metadata = task.task_metadata or {}
        actor_name = (
            metadata.get("actor_name") if isinstance(metadata.get("actor_name"), str) else None
        )
        actor_email = (
            metadata.get("actor_email") if isinstance(metadata.get("actor_email"), str) else None
        )
        await self._upsert_activity(
            activity_id=task.task_id,
            integration_id=task.integration_id,
            activity_type=CRMActivityType.TASK.value,
            title=task.title,
            summary=task.description,
            preview=_compact_preview(task.description or task.title),
            occurred_at=occurred_at,
            source_type="TASK",
            source_record_id=str(task.task_id),
            actor_name=actor_name,
            actor_email=actor_email,
        )
        await self._sync_primary_deal_link(
            activity_id=task.task_id,
            deal_id=task.deal_id,
            occurred_at=occurred_at,
        )

    async def upsert_note(self, note: CRMNote) -> None:
        occurred_at = note.source_created_at or note.created_at or _now_utc()
        await self._upsert_activity(
            activity_id=note.note_id,
            integration_id=note.integration_id,
            activity_type=CRMActivityType.NOTE.value,
            title=note_title_or_preview(note),
            summary=note.body,
            preview=_compact_preview(note.body),
            occurred_at=occurred_at,
            source_type="NOTE",
            source_record_id=str(note.note_id),
            actor_name=note.author_name,
            actor_email=note.author_email,
        )
        await self._sync_primary_deal_link(
            activity_id=note.note_id,
            deal_id=note.deal_id,
            occurred_at=occurred_at,
        )

    async def soft_delete_activity_projection(self, activity_id: UUID) -> None:
        activity_c = cast(Any, CRMActivity).__table__.c
        link_c = cast(Any, CRMActivityLink).__table__.c
        await self.session.execute(
            update(CRMActivity)
            .where(
                activity_c.organisation_id == self.organisation_id,
                activity_c.activity_id == activity_id,
            )
            .values(is_deleted=True, updated_at=_now_utc())
        )
        await self.session.execute(
            delete(CRMActivityLink).where(
                link_c.organisation_id == self.organisation_id,
                link_c.activity_id == activity_id,
            )
        )

    async def _upsert_activity(
        self,
        *,
        activity_id: UUID,
        integration_id: UUID | None,
        activity_type: str,
        title: str | None,
        summary: str | None,
        preview: str | None,
        occurred_at: datetime,
        source_type: str,
        source_record_id: str,
        actor_name: str | None,
        actor_email: str | None,
    ) -> None:
        now = _now_utc()
        values: dict[str, Any] = {
            "activity_id": activity_id,
            "organisation_id": self.organisation_id,
            "integration_id": integration_id,
            "activity_type": activity_type,
            "title": title,
            "summary": summary,
            "preview": preview,
            "occurred_at": occurred_at,
            "source_type": source_type,
            "source_record_id": source_record_id,
            "source_integration_id": integration_id,
            "actor_name": actor_name,
            "actor_email": actor_email,
            "display_payload": {},
            "is_deleted": False,
            "updated_at": now,
        }
        stmt = (
            pg_insert(CRMActivity)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["activity_id"],
                set_={
                    "integration_id": integration_id,
                    "activity_type": activity_type,
                    "title": title,
                    "summary": summary,
                    "preview": preview,
                    "occurred_at": occurred_at,
                    "source_type": source_type,
                    "source_record_id": source_record_id,
                    "source_integration_id": integration_id,
                    "actor_name": actor_name,
                    "actor_email": actor_email,
                    "is_deleted": False,
                    "updated_at": now,
                },
            )
        )
        await self.session.execute(stmt)

    async def _sync_primary_deal_link(
        self,
        *,
        activity_id: UUID,
        deal_id: UUID | None,
        occurred_at: datetime,
    ) -> None:
        link_c = cast(Any, CRMActivityLink).__table__.c
        base_where = (
            link_c.organisation_id == self.organisation_id,
            link_c.activity_id == activity_id,
            link_c.object_type == CRMActivityObjectType.DEAL.value,
            link_c.link_role == "PRIMARY",
        )
        if deal_id is None:
            await self.session.execute(delete(CRMActivityLink).where(*base_where))
            return

        await self.session.execute(
            delete(CRMActivityLink).where(*base_where, link_c.object_id != deal_id)
        )
        stmt = pg_insert(CRMActivityLink).values(
            organisation_id=self.organisation_id,
            activity_id=activity_id,
            object_type=CRMActivityObjectType.DEAL.value,
            object_id=deal_id,
            link_role="PRIMARY",
            link_source="SYSTEM",
            occurred_at=occurred_at,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_crm_activity_link_target_role",
            set_={"occurred_at": occurred_at},
        )
        await self.session.execute(stmt)
