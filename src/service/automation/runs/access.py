"""Organization-scoped persistence for atomically claiming agent runs.

Future workers call this layer immediately before execution. It owns only the
database compare-and-set; transaction commit and external agent work remain
outside so the caller can release its database session before the slow LLM call.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables import AgentRun, AgentRunStatus


class AgentRunClaimAccess:
    """Claim durable runs inside one immutable organization boundary."""

    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None:
        self.session = session
        self.organisation_id = organisation_id

    async def claim_run(
        self,
        run_id: UUID,
        *,
        worker_id: str,
        claimed_at: datetime,
        stale_before: datetime,
    ) -> AgentRun | None:
        """Claim a pending or expired running row and return the concurrent winner.

        The caller must commit before starting external work. Returning None
        means another worker owns the row or the run already reached a terminal
        state.
        """

        columns = cast(Any, AgentRun).__table__.c
        claimable = or_(
            columns.status == AgentRunStatus.PENDING.value,
            and_(
                columns.status == AgentRunStatus.RUNNING.value,
                or_(
                    columns.claimed_at.is_(None),
                    columns.claimed_at < stale_before,
                ),
            ),
        )

        # This compare-and-set is deliberately one SQL statement. A preceding
        # SELECT would let two workers observe the same pending row before either
        # writes, while UPDATE RETURNING gives PostgreSQL one durable winner.
        statement = (
            update(AgentRun)
            .where(
                columns.organisation_id == self.organisation_id,
                columns.run_id == run_id,
                claimable,
            )
            .values(
                status=AgentRunStatus.RUNNING.value,
                claimed_at=claimed_at,
                claimed_by=worker_id,
                attempt_count=columns.attempt_count + 1,
                started_at=func.coalesce(columns.started_at, claimed_at),
            )
            .returning(AgentRun)
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def complete_run(
        self,
        run_id: UUID,
        *,
        attempt_number: int,
        worker_id: str,
        status: str,
        completed_at: datetime,
        output_text: str | None,
        output_json: Any | None,
        error_code: str | None,
        error_message: str | None,
        model_name: str | None,
        token_usage: dict[str, Any] | None,
        cost: Decimal | None,
        latency_ms: int,
    ) -> bool:
        """Write one terminal result only while the caller still owns the claim."""

        columns = cast(Any, AgentRun).__table__.c
        # attempt_count and claimed_by form the fencing token. A worker whose
        # lease was reclaimed cannot overwrite the newer attempt's result.
        statement = (
            update(AgentRun)
            .where(
                columns.organisation_id == self.organisation_id,
                columns.run_id == run_id,
                columns.status == AgentRunStatus.RUNNING.value,
                columns.attempt_count == attempt_number,
                columns.claimed_by == worker_id,
            )
            .values(
                status=status,
                completed_at=completed_at,
                output_text=output_text,
                output_json=output_json,
                error_code=error_code,
                error_message=error_message,
                model_name=model_name,
                token_usage=token_usage,
                cost=cost,
                latency_ms=latency_ms,
            )
            .returning(columns.run_id)
            .execution_options(synchronize_session=False)
        )
        return (await self.session.execute(statement)).scalar_one_or_none() is not None

    async def release_for_retry(
        self,
        run_id: UUID,
        *,
        attempt_number: int,
        worker_id: str,
        error_code: str,
        error_message: str,
    ) -> bool:
        """Return an owned running attempt to pending for a later Celery delivery."""

        columns = cast(Any, AgentRun).__table__.c
        statement = (
            update(AgentRun)
            .where(
                columns.organisation_id == self.organisation_id,
                columns.run_id == run_id,
                columns.status == AgentRunStatus.RUNNING.value,
                columns.attempt_count == attempt_number,
                columns.claimed_by == worker_id,
            )
            .values(
                status=AgentRunStatus.PENDING.value,
                claimed_at=None,
                claimed_by=None,
                error_code=error_code,
                error_message=error_message,
            )
            .returning(columns.run_id)
            .execution_options(synchronize_session=False)
        )
        return (await self.session.execute(statement)).scalar_one_or_none() is not None

    async def list_dispatchable_run_ids(
        self,
        *,
        stale_before: datetime,
        limit: int,
    ) -> list[UUID]:
        """Return pending or lease-expired runs for idempotent queue publication."""

        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        columns = cast(Any, AgentRun).__table__.c
        statement = (
            select(columns.run_id)
            .where(
                columns.organisation_id == self.organisation_id,
                or_(
                    columns.status == AgentRunStatus.PENDING.value,
                    and_(
                        columns.status == AgentRunStatus.RUNNING.value,
                        or_(
                            columns.claimed_at.is_(None),
                            columns.claimed_at < stale_before,
                        ),
                    ),
                ),
            )
            .order_by(columns.created_at, columns.run_id)
            .limit(limit)
        )
        return list((await self.session.execute(statement)).scalars().all())
