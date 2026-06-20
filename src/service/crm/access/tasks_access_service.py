"""Compatibility facade for legacy task read callers."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm import CRMActivity
from src.service.crm.tasks.contracts import TaskReadServiceError
from src.service.crm.tasks.read_service import (
    TaskReadService,
)

TasksAccessServiceError = TaskReadServiceError


class TasksAccessService:
    """Compatibility facade; use TaskReadService for new code."""

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        self._service = TaskReadService(org_session, organisation_id)

    async def close(self) -> None:
        await self._service.close()

    @property
    def session(self) -> AsyncSession:
        return self._service.session

    async def list_tasks_by_deal(
        self,
        deal_id: UUID,
        limit: int = 50,
        page: int = 1,
        statuses: Iterable[str] | None = None,
        priorities: Iterable[str] | None = None,
        task_types: Iterable[str] | None = None,
    ) -> tuple[list[CRMActivity], int]:
        return await self._service.list_tasks_by_deal(
            deal_id=deal_id,
            limit=limit,
            page=page,
            statuses=statuses,
            priorities=priorities,
            task_types=task_types,
        )

    async def list_tasks(
        self,
        *,
        scope: str = "assigned_to_me",
        current_user_id: UUID | None = None,
        current_user_email: str | None = None,
        deal_id: UUID | None = None,
        statuses: Iterable[str] | None = None,
        priorities: Iterable[str] | None = None,
        source: Iterable[str] | None = None,
        owner_user_id: UUID | None = None,
        assigned_to_key: str | None = None,
        created_by_user_id: UUID | None = None,
        due_from: datetime | None = None,
        due_to: datetime | None = None,
        is_overdue: bool | None = None,
        has_due_date: bool | None = None,
        include_completed: bool = False,
        sort_by: str = "due_date",
        sort_order: str = "asc",
        page: int = 1,
        limit: int = 50,
    ) -> tuple[list[CRMActivity], int]:
        return await self._service.list_tasks(
            scope=scope,
            current_user_id=current_user_id,
            current_user_email=current_user_email,
            deal_id=deal_id,
            statuses=statuses,
            priorities=priorities,
            source=source,
            owner_user_id=owner_user_id,
            assigned_to_key=assigned_to_key,
            created_by_user_id=created_by_user_id,
            due_from=due_from,
            due_to=due_to,
            is_overdue=is_overdue,
            has_due_date=has_due_date,
            include_completed=include_completed,
            sort_by=sort_by,
            sort_order=sort_order,
            page=page,
            limit=limit,
        )

    async def resolve_current_user_task_assignees(
        self,
        *,
        user_id: UUID,
        user_email: str | None,
    ) -> list[dict[str, Any]]:
        return await self._service.resolve_current_user_task_assignees(
            user_id=user_id,
            user_email=user_email,
        )

    async def list_task_assignees(
        self,
        *,
        q: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        return await self._service.list_task_assignees(q=q, limit=limit)

    async def get_task_by_id(self, task_id: UUID) -> CRMActivity | None:
        return await self._service.get_task_by_id(task_id)
