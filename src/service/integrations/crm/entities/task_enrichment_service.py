"""Task enrichment helper service.

Enriches CRMDealTask records with denormalised data pulled from related
entities (users, deals). Intended to be used by the task sync flow to
populate owner details after syncing.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm import CRMDealTask, CRMUser


class TaskEnrichmentService:
    """Populate denormalised task fields from related CRM entities."""

    def __init__(self, *, db: AsyncSession, organisation_id: UUID) -> None:
        self.db = db
        self.organisation_id = organisation_id

    async def enrich_tasks(self, tasks: Iterable[CRMDealTask]) -> None:
        """Enrich the provided task records in-place.

        Args:
            tasks: Iterable of task ORM objects already attached to the session.
        """
        task_list = [task for task in tasks if isinstance(task, CRMDealTask)]
        if not task_list:
            return

        # Collect owner IDs that need enrichment
        owner_ids = {task.owner_external_id for task in task_list if task.owner_external_id}

        if not owner_ids:
            return

        # Load users
        users = await self._load_users(owner_ids)

        # Apply enrichment
        for task in task_list:
            if task.owner_external_id:
                user = users.get(task.owner_external_id)
                if user:
                    self._apply_owner_details(task, user)

    async def enrich_tasks_for_integration(self, integration_id: UUID) -> None:
        """Enrich all tasks for a given integration."""
        result = await self.db.execute(
            select(CRMDealTask)
            .where(CRMDealTask.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMDealTask.integration_id == integration_id)  # type: ignore[arg-type]
            .where(CRMDealTask.is_deleted.is_(False))  # type: ignore[attr-defined]
        )
        tasks = result.scalars().all()
        if not tasks:
            return
        await self.enrich_tasks(tasks)

    async def _load_users(self, owner_ids: set[str]) -> dict[str, CRMUser]:
        """Load CRM users by external IDs.

        Args:
            owner_ids: Set of owner external IDs to load

        Returns:
            Dictionary mapping external_id to CRMUser
        """
        result = await self.db.execute(
            select(CRMUser)
            .where(CRMUser.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMUser.external_id.in_(owner_ids))  # type: ignore[attr-defined]
            .where(CRMUser.is_deleted.is_(False))  # type: ignore[attr-defined]
        )
        return {user.external_id: user for user in result.scalars().all()}

    def _apply_owner_details(self, task: CRMDealTask, user: CRMUser) -> None:
        """Apply owner details from user to task.

        Args:
            task: Task record to enrich
            user: User record with owner details
        """
        full_name = user.full_name or self._compose_name(user.first_name, user.last_name)
        if full_name and full_name != task.owner_name:
            task.owner_name = full_name

        if user.email and user.email != task.owner_email:
            task.owner_email = user.email

    def _compose_name(self, first: Any, last: Any) -> str | None:
        """Compose full name from first and last name.

        Args:
            first: First name
            last: Last name

        Returns:
            Composed full name or None
        """
        parts = [str(part).strip() for part in (first, last) if part]
        return " ".join(parts) if parts else None
