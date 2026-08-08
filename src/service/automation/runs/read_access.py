"""Audience-scoped SQL for agent run history and execution metadata."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables import AgentRun, AgentRunStatus
from src.domain.models.organisation_tables.sync import NotificationDelivery
from src.service.automation.runs.delivery_access import AgentRunDeliveryAccess


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
        # The organization predicate is the isolation boundary; the supplied run
        # or agent IDs below only narrow rows already inside this tenant.
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

    async def list_deliveries(self, run_ids: list[UUID]) -> dict[UUID, list[NotificationDelivery]]:
        """Return delivery rows only for the already-authorized run page."""

        if not run_ids:
            return {}
        columns = cast(Any, NotificationDelivery).__table__.c
        # These IDs came from the visibility-filtered run page, but the tenant and
        # source predicates remain on this query so the ledger is safe in isolation.
        statement = select(NotificationDelivery).where(
            columns.organisation_id == self.organisation_id,
            columns.notification_type == "agent_output_ready",
            columns.source_type == "agent_run",
            columns.source_id.in_(run_ids),
            columns.channel == "email",
        )
        rows = (await self.session.execute(statement)).scalars().all()
        result: dict[UUID, list[NotificationDelivery]] = {}
        for row in rows:
            result.setdefault(row.source_id, []).append(row)
        return result

    async def get_deliveries(self, run_id: UUID) -> list[NotificationDelivery]:
        """Return tenant-scoped notification rows after run visibility was checked."""

        columns = cast(Any, NotificationDelivery).__table__.c
        # Reapply the tenant boundary because run_id is an externally supplied API
        # identifier and cannot authorize a cross-tenant delivery lookup.
        statement = select(NotificationDelivery).where(
            columns.organisation_id == self.organisation_id,
            columns.notification_type == "agent_output_ready",
            columns.source_type == "agent_run",
            columns.source_id == run_id,
            columns.channel == "email",
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def requeue_repairable_delivery(
        self,
        run_id: UUID,
        delivery_id: UUID,
        *,
        expected_error_code: str,
    ) -> UUID | None:
        """Requeue one visible repairable delivery through the fenced ledger access."""

        return await AgentRunDeliveryAccess(
            self.session,
            self.organisation_id,
        ).requeue_repairable_delivery(
            run_id,
            delivery_id,
            expected_error_code=expected_error_code,
        )
