"""Organization-scoped persistence for atomically claiming agent runs.

Future workers call this layer immediately before execution. It owns only the
database compare-and-set; transaction commit and external agent work remain
outside so the caller can release its database session before the slow LLM call.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables import (
    AgentRun,
    AgentRunStatus,
    AutomationTrigger,
)
from src.domain.models.organisation_tables.sync import (
    NotificationDelivery,
    NotificationDeliveryChannel,
    NotificationDeliveryStatus,
)


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
    ) -> AgentRun | None:
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
            .returning(AgentRun)
            .execution_options(synchronize_session=False)
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

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

    async def record_trigger_outcome(
        self,
        trigger_id: UUID,
        *,
        succeeded: bool,
        completed_at: datetime,
        error_code: str | None,
        error_message: str | None,
    ) -> None:
        """Record trigger outcome as part of the durable automation lifecycle."""
        columns = cast(Any, AutomationTrigger).__table__.c
        values: dict[str, Any] = {
            "last_triggered_at": completed_at,
            "trigger_count": columns.trigger_count + 1,
            "last_error_code": None if succeeded else error_code,
            "last_error": None if succeeded else error_message,
        }
        values["consecutive_failures"] = 0 if succeeded else columns.consecutive_failures + 1
        await self.session.execute(
            update(AutomationTrigger)
            .where(
                columns.organisation_id == self.organisation_id,
                columns.trigger_id == trigger_id,
            )
            .values(values)
        )

    async def ensure_deliveries(self, run: AgentRun) -> tuple[UUID, ...]:
        """Insert one platform-email intent for every immutable run recipient."""

        # Recipients come from the pinned run audience, not a live trigger or user
        # lookup; replaying completion must preserve the same delivery identities.
        recipient_ids = _delivery_recipient_ids(run)
        targets: list[tuple[str, str | None]] = [
            (str(recipient_id), None) for recipient_id in recipient_ids
        ]
        if not targets:
            targets = [("policy:no_authorized_recipient", "no_authorized_recipient")]
        columns = cast(Any, NotificationDelivery).__table__.c
        delivery_ids: list[UUID] = []
        for target_key, policy_error in targets:
            delivery_id = uuid4()
            identity = {
                "organisation_id": self.organisation_id,
                "notification_type": "agent_output_ready",
                "source_type": "agent_run",
                "source_id": run.run_id,
                "channel": NotificationDeliveryChannel.EMAIL.value,
                "target_key": target_key,
            }
            delivery_values: dict[str, Any] = {
                "delivery_id": delivery_id,
                **identity,
                "delivery_metadata": {"template_version": 1},
            }
            if policy_error is not None:
                delivery_values.update(
                    status=NotificationDeliveryStatus.SKIPPED.value,
                    error_code=policy_error,
                    last_error="No authorized recipient was pinned to this run",
                )
            created = (
                await self.session.execute(
                    insert(NotificationDelivery)
                    .values(**delivery_values)
                    .on_conflict_do_nothing(
                        # The unique source/channel/target key makes completion
                        # idempotent when a worker finalization is replayed.
                        index_elements=[
                            columns.organisation_id,
                            columns.notification_type,
                            columns.source_type,
                            columns.source_id,
                            columns.channel,
                            columns.target_key,
                        ]
                    )
                    .returning(columns.delivery_id)
                )
            ).scalar_one_or_none()
            if created is None:
                created = (
                    await self.session.execute(
                        select(columns.delivery_id).where(
                            *(columns[key] == value for key, value in identity.items())
                        )
                    )
                ).scalar_one()
            if policy_error is None:
                delivery_ids.append(cast(UUID, created))
        return tuple(delivery_ids)

    async def list_dispatchable_run_ids(
        self,
        *,
        stale_before: datetime,
        limit: int,
    ) -> list[UUID]:
        """Return pending or lease-expired runs for idempotent queue publication."""

        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        # Recovery is intentionally bounded so one organization cannot monopolize
        # the sweep; the next tick continues from the ordered keyset.
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


def _delivery_recipient_ids(run: AgentRun) -> tuple[UUID, ...]:
    """Resolve recipients from the immutable execution principal or run audience."""

    if str(getattr(run.execution_principal_type, "value", run.execution_principal_type)) == "user":
        return (run.execution_principal_id,)
    values = run.audience_config.get("user_ids", [])
    recipients = tuple(sorted({UUID(str(value)) for value in values}, key=str))
    return recipients
