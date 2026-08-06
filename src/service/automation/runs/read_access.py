"""Audience-scoped SQL for agent run history and execution metadata."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables import AgentRun, AgentRunStatus


class AgentRunReadAccess:
    """Read runs inside immutable tenant, principal, and audience boundaries."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        audience_user_id: UUID,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.audience_user_id = audience_user_id

    def _visible_conditions(self) -> list[Any]:
        columns = cast(Any, AgentRun).__table__.c
        explicit_audience = columns.audience_config.contains(
            {"user_ids": [str(self.audience_user_id)]}
        )
        return [
            columns.organisation_id == self.organisation_id,
            or_(
                and_(
                    columns.execution_principal_type == "user",
                    columns.execution_principal_id == self.audience_user_id,
                    columns.audience_mode == "principal",
                ),
                and_(
                    columns.execution_principal_type == "organisation_service",
                    columns.audience_mode.in_(("explicit_users", "subject_participants")),
                    explicit_audience,
                ),
            ),
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
        conditions = self._visible_conditions()
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
            *self._visible_conditions(),
            columns.run_id == run_id,
        )
        return (await self.session.execute(statement)).scalars().first()
