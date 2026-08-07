"""Owner-scoped persistence for agent automation triggers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables import (
    AgentStatus,
    AutomationPrincipalType,
    AutomationTargetType,
    AutomationTrigger,
    AutomationTriggerStatus,
    UserAgent,
)

_MUTABLE_COLUMNS = {
    "name",
    "status",
    "trigger_key",
    "family",
    "match_event_keys",
    "trigger_config",
    "watch_config",
    "filter_config",
    "execution_principal_type",
    "execution_principal_id",
    "scope_type",
    "scope_config",
    "audience_mode",
    "audience_config",
    "task_config",
    "delivery_config",
    "next_trigger_at",
    "config_hash",
    "target_revision",
    "target_snapshot",
}


class AutomationTriggerAccess:
    """Run all trigger SQL inside one immutable organization and owner boundary."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        created_by_user_id: UUID,
        *,
        is_admin: bool = False,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.created_by_user_id = created_by_user_id
        self.is_admin = is_admin

    def _owned_conditions(self, target_id: UUID | None = None) -> list[Any]:
        columns = cast(Any, AutomationTrigger).__table__.c
        conditions = [
            columns.organisation_id == self.organisation_id,
            columns.target_type == AutomationTargetType.AGENT.value,
        ]
        if self.is_admin:
            conditions.append(
                or_(
                    columns.created_by_user_id == self.created_by_user_id,
                    columns.execution_principal_type
                    == AutomationPrincipalType.ORGANISATION_SERVICE.value,
                )
            )
        else:
            conditions.append(columns.created_by_user_id == self.created_by_user_id)
        if target_id is not None:
            conditions.append(columns.target_id == target_id)
        return conditions

    async def get_agent(self, agent_id: UUID) -> UserAgent | None:
        """Resolve a non-archived target through the same owner boundary."""

        columns = cast(Any, UserAgent).__table__.c
        conditions = [
            columns.organisation_id == self.organisation_id,
            columns.agent_id == agent_id,
            columns.status != AgentStatus.ARCHIVED.value,
        ]
        if not self.is_admin:
            conditions.append(columns.owner_user_id == self.created_by_user_id)
        return (await self.session.execute(select(UserAgent).where(*conditions))).scalars().first()

    async def list_triggers(
        self, agent_id: UUID, *, status: AutomationTriggerStatus | None, limit: int, offset: int
    ) -> tuple[list[AutomationTrigger], int]:
        """Return one trigger page and total count under the same target boundary."""

        columns = cast(Any, AutomationTrigger).__table__.c
        conditions = [
            *self._owned_conditions(agent_id),
            columns.status != AutomationTriggerStatus.ARCHIVED.value,
        ]
        if status is not None:
            conditions.append(columns.status == status.value)
        total = int(
            await self.session.scalar(select(func.count(columns.trigger_id)).where(*conditions))
            or 0
        )
        rows = list(
            (
                await self.session.execute(
                    select(AutomationTrigger)
                    .where(*conditions)
                    .order_by(columns.updated_at.desc(), columns.trigger_id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return rows, total

    async def get_trigger(
        self, agent_id: UUID, trigger_id: UUID, *, include_archived: bool = False
    ) -> AutomationTrigger | None:
        """Load one trigger without revealing foreign owner or target identifiers."""

        columns = cast(Any, AutomationTrigger).__table__.c
        conditions = [*self._owned_conditions(agent_id), columns.trigger_id == trigger_id]
        if not include_archived:
            conditions.append(columns.status != AutomationTriggerStatus.ARCHIVED.value)
        return (
            (await self.session.execute(select(AutomationTrigger).where(*conditions)))
            .scalars()
            .first()
        )

    async def get_trigger_by_id(
        self,
        trigger_id: UUID,
        *,
        include_archived: bool = False,
    ) -> AutomationTrigger | None:
        """Load one manageable binding when its target ID is not part of the route."""

        columns = cast(Any, AutomationTrigger).__table__.c
        conditions = [*self._owned_conditions(), columns.trigger_id == trigger_id]
        if not include_archived:
            conditions.append(columns.status != AutomationTriggerStatus.ARCHIVED.value)
        return (
            (await self.session.execute(select(AutomationTrigger).where(*conditions)))
            .scalars()
            .first()
        )

    async def find_duplicate(
        self, agent_id: UUID, config_hash: str, *, exclude_trigger_id: UUID | None = None
    ) -> AutomationTrigger | None:
        """Find the same non-archived execution definition before the unique index does."""

        columns = cast(Any, AutomationTrigger).__table__.c
        conditions = [
            *self._owned_conditions(agent_id),
            columns.config_hash == config_hash,
            columns.status != AutomationTriggerStatus.ARCHIVED.value,
        ]
        if exclude_trigger_id is not None:
            conditions.append(columns.trigger_id != exclude_trigger_id)
        return (
            (await self.session.execute(select(AutomationTrigger).where(*conditions)))
            .scalars()
            .first()
        )

    async def create_trigger(self, trigger: AutomationTrigger) -> None:
        """Flush a server-owned trigger without committing the request transaction."""

        self.session.add(trigger)
        await self.session.flush()
        await self.session.refresh(trigger)

    async def replace_trigger(
        self,
        agent_id: UUID,
        trigger_id: UUID,
        *,
        expected_updated_at: datetime,
        values: dict[str, Any],
    ) -> AutomationTrigger | None:
        """Conditionally update allowed columns when the last-read timestamp still matches."""

        unsupported = set(values) - _MUTABLE_COLUMNS
        if unsupported:
            raise ValueError(
                f"Unsupported mutable trigger columns: {', '.join(sorted(unsupported))}"
            )
        columns = cast(Any, AutomationTrigger).__table__.c
        assignments = {getattr(columns, key): value for key, value in values.items()}
        assignments[columns.updated_at] = datetime.now(UTC)
        statement = (
            update(AutomationTrigger)
            .where(
                *self._owned_conditions(agent_id),
                columns.trigger_id == trigger_id,
                columns.status != AutomationTriggerStatus.ARCHIVED.value,
                columns.updated_at == expected_updated_at,
            )
            .values(assignments)
            .returning(AutomationTrigger)
        )
        return (await self.session.execute(statement)).scalars().first()

    async def archive_trigger(
        self, agent_id: UUID, trigger_id: UUID, *, expected_updated_at: datetime
    ) -> AutomationTrigger | None:
        """Soft-delete one trigger using the same optimistic concurrency boundary."""

        return await self.replace_trigger(
            agent_id,
            trigger_id,
            expected_updated_at=expected_updated_at,
            values={"status": AutomationTriggerStatus.ARCHIVED.value},
        )


class AutomationScheduleAccess:
    """Claim bounded due schedule rows inside one tenant transaction."""

    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None:
        self.session = session
        self.organisation_id = organisation_id

    async def claim_due_schedules(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> list[AutomationTrigger]:
        """Lock one due batch without blocking another scheduler worker."""

        columns = cast(Any, AutomationTrigger).__table__.c
        statement = (
            select(AutomationTrigger)
            .where(
                columns.organisation_id == self.organisation_id,
                columns.status == AutomationTriggerStatus.ACTIVE.value,
                columns.family == "schedule",
                columns.next_trigger_at.is_not(None),
                columns.next_trigger_at <= now,
            )
            .order_by(columns.next_trigger_at, columns.trigger_id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list((await self.session.execute(statement)).scalars().all())
