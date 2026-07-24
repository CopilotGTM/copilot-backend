"""Owner-scoped SQL for agent run history and execution metadata."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables import AgentRun, AgentRunStatus


class AgentRunReadAccess:
    """Read runs inside immutable organisation and owner boundaries."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        owner_user_id: UUID,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.owner_user_id = owner_user_id

    def _owned_conditions(self) -> list[Any]:
        columns = cast(Any, AgentRun).__table__.c
        # Run UUIDs are locators, not authorization. Both predicates prevent
        # cross-tenant and cross-user execution metadata disclosure.
        return [
            columns.organisation_id == self.organisation_id,
            columns.owner_user_id == self.owner_user_id,
        ]

    async def list_runs(
        self,
        *,
        agent_id: UUID | None,
        trigger_id: UUID | None,
        status: AgentRunStatus | None,
        cursor_created_at: datetime | None,
        cursor_run_id: UUID | None,
        limit: int,
    ) -> list[AgentRun]:
        """Return a descending keyset page under optional owned-run filters."""

        columns = cast(Any, AgentRun).__table__.c
        conditions = self._owned_conditions()
        if agent_id is not None:
            conditions.append(columns.agent_id == agent_id)
        if trigger_id is not None:
            conditions.append(columns.trigger_id == trigger_id)
        if status is not None:
            conditions.append(columns.status == status.value)
        if cursor_created_at is not None and cursor_run_id is not None:
            conditions.append(
                or_(
                    columns.created_at < cursor_created_at,
                    and_(
                        columns.created_at == cursor_created_at,
                        columns.run_id < cursor_run_id,
                    ),
                )
            )

        statement = (
            select(AgentRun)
            .where(*conditions)
            .order_by(columns.created_at.desc(), columns.run_id.desc())
            .limit(limit)
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def get_run(self, run_id: UUID) -> AgentRun | None:
        """Return one owned run without revealing foreign identifiers."""

        columns = cast(Any, AgentRun).__table__.c
        statement = select(AgentRun).where(
            *self._owned_conditions(),
            columns.run_id == run_id,
        )
        return (await self.session.execute(statement)).scalars().first()
