"""Owner-scoped persistence for user-agent definitions.

All reads and writes repeat the authenticated organisation and owner predicates. An agent UUID
is only a resource locator and is never trusted as authorization, including after a prior read.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables import AgentSourceType, AgentStatus, UserAgent

_MUTABLE_COLUMN_NAMES = {
    "name",
    "description",
    "prompt",
    "model_name",
    "model_config",
    "allowed_tools",
    "allowed_skills",
    "max_turns",
    "input_schema",
    "output_mode",
    "output_schema",
    "status",
}


class AgentDefinitionAccess:
    """Execute SQL for one authenticated user's private agent definitions."""

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
        columns = cast(Any, UserAgent).__table__.c
        # Keep both predicates here so every caller inherits the tenant and user isolation boundary.
        return [
            columns.organisation_id == self.organisation_id,
            columns.owner_user_id == self.owner_user_id,
        ]

    async def list_agents(
        self,
        *,
        status: AgentStatus | None,
        limit: int,
        offset: int,
    ) -> tuple[list[UserAgent], int]:
        """Return one owner page and the total rows matching the same filters."""

        columns = cast(Any, UserAgent).__table__.c
        conditions = [*self._owned_conditions(), columns.status != AgentStatus.ARCHIVED.value]
        if status is not None:
            conditions.append(columns.status == status.value)

        total_count = int(
            await self.session.scalar(select(func.count(columns.agent_id)).where(*conditions)) or 0
        )
        statement = (
            select(UserAgent)
            .where(*conditions)
            .order_by(columns.updated_at.desc(), columns.agent_id.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = list((await self.session.execute(statement)).scalars().all())
        return rows, total_count

    async def list_template_copies(self, template_ids: set[UUID]) -> list[UserAgent]:
        """Return this owner's live agents copied from the requested system templates."""

        if not template_ids:
            return []
        columns = cast(Any, UserAgent).__table__.c
        statement = (
            select(UserAgent)
            .where(
                *self._owned_conditions(),
                columns.status != AgentStatus.ARCHIVED.value,
                columns.source_type == AgentSourceType.SYSTEM.value,
                columns.source_agent_id.in_(template_ids),
            )
            .order_by(columns.updated_at.desc(), columns.agent_id.desc())
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def get_agent(
        self,
        agent_id: UUID,
        *,
        include_archived: bool = False,
    ) -> UserAgent | None:
        """Load one owned definition while hiding logical deletes by default."""

        columns = cast(Any, UserAgent).__table__.c
        conditions = [*self._owned_conditions(), columns.agent_id == agent_id]
        if not include_archived:
            conditions.append(columns.status != AgentStatus.ARCHIVED.value)
        statement = select(UserAgent).where(*conditions)
        return (await self.session.execute(statement)).scalars().first()

    async def create_agent(self, agent: UserAgent) -> None:
        """Stage a new server-owned definition without committing the caller's transaction."""

        self.session.add(agent)
        await self.session.flush()
        await self.session.refresh(agent)

    async def replace_agent(
        self,
        agent_id: UUID,
        *,
        expected_revision: int,
        values: dict[str, Any],
    ) -> UserAgent | None:
        """Atomically replace mutable fields if owner, lifecycle, and revision still match."""

        unsupported_columns = set(values) - _MUTABLE_COLUMN_NAMES
        if unsupported_columns:
            names = ", ".join(sorted(unsupported_columns))
            raise ValueError(f"Unsupported mutable agent columns: {names}")

        columns = cast(Any, UserAgent).__table__.c
        assignments = {getattr(columns, name): value for name, value in values.items()}
        assignments[columns.revision] = columns.revision + 1
        assignments[columns.updated_at] = datetime.now(UTC)

        statement = (
            update(UserAgent)
            .where(
                *self._owned_conditions(),
                columns.agent_id == agent_id,
                columns.revision == expected_revision,
                columns.status != AgentStatus.ARCHIVED.value,
            )
            .values(assignments)
            .returning(UserAgent)
        )
        return (await self.session.execute(statement)).scalars().first()

    async def archive_agent(
        self,
        agent_id: UUID,
        *,
        expected_revision: int,
    ) -> UserAgent | None:
        """Soft-delete one owned agent if the caller still has its current revision."""

        columns = cast(Any, UserAgent).__table__.c
        statement = (
            update(UserAgent)
            .where(
                *self._owned_conditions(),
                columns.agent_id == agent_id,
                columns.revision == expected_revision,
                columns.status != AgentStatus.ARCHIVED.value,
            )
            .values(
                status=AgentStatus.ARCHIVED.value,
                revision=columns.revision + 1,
                updated_at=datetime.now(UTC),
            )
            .returning(UserAgent)
        )
        return (await self.session.execute(statement)).scalars().first()

    async def replace_output_delivery(
        self,
        agent_id: UUID,
        *,
        expected_updated_at: datetime,
        channels: list[str],
    ) -> UserAgent | None:
        """Replace delivery channels without changing the executable agent revision."""

        columns = cast(Any, UserAgent).__table__.c
        now = datetime.now(UTC)
        statement = (
            update(UserAgent)
            .where(
                *self._owned_conditions(),
                columns.agent_id == agent_id,
                columns.output_delivery_updated_at == expected_updated_at,
                columns.status != AgentStatus.ARCHIVED.value,
            )
            .values(
                output_delivery_config={"channels": channels},
                output_delivery_updated_at=now,
                updated_at=now,
            )
            .returning(UserAgent)
        )
        return (await self.session.execute(statement)).scalars().first()
