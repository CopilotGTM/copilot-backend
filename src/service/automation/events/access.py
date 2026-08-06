"""Organization-scoped persistence for automation event dispatches.

Producers use this boundary inside their source transaction. Recovery workers
use the same rows later, so every query repeats the organization boundary even
though each tenant normally has its own schema.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables import AutomationEventDispatch


class AutomationEventDispatchAccess:
    """Persist and retrieve replay-safe activation dispatches for one tenant."""

    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None:
        self.session = session
        self.organisation_id = organisation_id

    async def insert_if_absent(self, dispatch: AutomationEventDispatch) -> bool:
        """Insert one event identity or report an idempotent replay."""

        table = cast(Any, AutomationEventDispatch).__table__
        columns = table.c
        values = {column.name: getattr(dispatch, column.name) for column in table.columns}
        statement = (
            insert(AutomationEventDispatch)
            .values(values)
            .on_conflict_do_nothing(
                index_elements=[
                    columns.organisation_id,
                    columns.idempotency_key,
                ]
            )
            .returning(columns.event_id)
        )
        return (await self.session.execute(statement)).scalar_one_or_none() is not None

    async def get_by_idempotency_key(self, key: str) -> AutomationEventDispatch | None:
        """Return the tenant-owned event created by an earlier producer delivery."""

        columns = cast(Any, AutomationEventDispatch).__table__.c
        statement = select(AutomationEventDispatch).where(
            columns.organisation_id == self.organisation_id,
            columns.idempotency_key == key,
        )
        return (await self.session.execute(statement)).scalars().first()

    async def get(self, event_id: UUID) -> AutomationEventDispatch | None:
        """Load one event without trusting its opaque ID across tenants."""

        columns = cast(Any, AutomationEventDispatch).__table__.c
        statement = select(AutomationEventDispatch).where(
            columns.organisation_id == self.organisation_id,
            columns.event_id == event_id,
        )
        return (await self.session.execute(statement)).scalars().first()

    async def list_pending_before(self, cutoff: datetime, *, limit: int) -> list[UUID]:
        """Return a bounded batch whose direct post-commit publication may be lost."""

        columns = cast(Any, AutomationEventDispatch).__table__.c
        statement = (
            select(columns.event_id)
            .where(
                columns.organisation_id == self.organisation_id,
                columns.created_at <= cutoff,
                columns.processed_at.is_(None),
                columns.failed_at.is_(None),
            )
            .order_by(columns.created_at, columns.event_id)
            .limit(limit)
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def mark_processed(
        self,
        event_id: UUID,
        *,
        candidate_count: int,
        matched_count: int,
        activation_outcomes: list[dict[str, Any]],
    ) -> bool:
        """Terminally acknowledge activation while duplicate deliveries remain safe."""

        columns = cast(Any, AutomationEventDispatch).__table__.c
        now = datetime.now(UTC)
        statement = (
            update(AutomationEventDispatch)
            .where(
                columns.organisation_id == self.organisation_id,
                columns.event_id == event_id,
                columns.processed_at.is_(None),
                columns.failed_at.is_(None),
            )
            .values(
                processed_at=now,
                last_attempted_at=now,
                attempt_count=columns.attempt_count + 1,
                candidate_count=candidate_count,
                matched_count=matched_count,
                activation_outcomes=activation_outcomes,
                last_error_code=None,
                last_error=None,
            )
            .returning(columns.event_id)
        )
        return (await self.session.execute(statement)).scalar_one_or_none() is not None

    async def record_failure(
        self,
        event_id: UUID,
        *,
        error_code: str,
        error: str,
        terminal: bool,
    ) -> bool:
        """Record a sanitized retry outcome or stop recovery after the final attempt."""

        columns = cast(Any, AutomationEventDispatch).__table__.c
        now = datetime.now(UTC)
        values: dict[str, Any] = {
            "last_attempted_at": now,
            "attempt_count": columns.attempt_count + 1,
            "last_error_code": error_code[:120],
            "last_error": error[:1000],
        }
        if terminal:
            values["failed_at"] = now
        statement = (
            update(AutomationEventDispatch)
            .where(
                columns.organisation_id == self.organisation_id,
                columns.event_id == event_id,
                columns.processed_at.is_(None),
                columns.failed_at.is_(None),
            )
            .values(**values)
            .returning(columns.event_id)
        )
        return (await self.session.execute(statement)).scalar_one_or_none() is not None
