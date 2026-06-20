"""Compatibility facade for legacy task write callers."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm import CRMActivity
from src.service.crm.tasks.contracts import TaskWriteServiceError
from src.service.crm.tasks.write_service import TaskWriteService

TasksWriteServiceError = TaskWriteServiceError


class TasksWriteService:
    """Compatibility facade; use TaskWriteService for new code."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        actor_user_id: UUID,
        *,
        user_timezone: str | None = None,
    ):
        self._service = TaskWriteService(
            session,
            organisation_id,
            actor_user_id,
            user_timezone=user_timezone,
        )

    @property
    def session(self) -> AsyncSession:
        return self._service.session

    async def close(self) -> None:
        await self._service.close()

    async def create_task(
        self,
        payload: dict[str, Any],
        *,
        deal_id: UUID | None = None,
    ) -> CRMActivity:
        return await self._service.create_task(payload, deal_id=deal_id)

    async def update_task(self, task_id: UUID, payload: dict[str, Any]) -> CRMActivity:
        return await self._service.update_task(task_id, payload)

    async def delete_task(self, task_id: UUID) -> CRMActivity:
        return await self._service.delete_task(task_id)
