"""Tenant-scoped claim and terminal writes for post-run delivery.

The delivery worker claims one generic notification row, performs provider
work outside that claim transaction, and then writes the result with the claim
token as a fence. Every query repeats the organization and delivery identity so
queue payloads never become an authorization boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables import (
    AgentRun,
    AgentRunStatus,
)
from src.domain.models.organisation_tables.sync import (
    NotificationDelivery,
    NotificationDeliveryChannel,
    NotificationDeliveryStatus,
)

_NOTIFICATION_TYPE = "agent_output_ready"
_SOURCE_TYPE = "agent_run"


@dataclass(frozen=True, slots=True)
class AgentRunDeliveryDispatchCandidate:
    """One due delivery plus the timestamps needed for recovery health metrics."""

    delivery_id: UUID
    run_id: UUID
    status: str
    created_at: datetime
    claimed_at: datetime | None


class AgentRunDeliveryAccess:
    """Fence one email delivery independently from its completed model run.

    This access object only mutates the durable ledger. The worker owns retry
    policy and provider calls, while the claim token prevents stale workers from
    overwriting a newer attempt.
    """

    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None:
        self.session = session
        self.organisation_id = organisation_id

    async def claim(
        self,
        delivery_id: UUID,
        *,
        worker_id: str,
        claimed_at: datetime,
        stale_before: datetime,
    ) -> tuple[NotificationDelivery, AgentRun] | None:
        """Atomically claim one due delivery and load its successful source run."""

        columns = cast(Any, NotificationDelivery).__table__.c
        claim_token = uuid4()
        claimable = or_(
            and_(
                columns.status == NotificationDeliveryStatus.PENDING.value,
                or_(
                    columns.next_attempt_at.is_(None),
                    columns.next_attempt_at <= claimed_at,
                ),
            ),
            and_(
                columns.status == NotificationDeliveryStatus.SENDING.value,
                or_(columns.claimed_at.is_(None), columns.claimed_at < stale_before),
            ),
        )
        # The organization predicate is the trust boundary; delivery_id from a
        # queue message is only an identifier and is never sufficient by itself.
        statement = (
            update(NotificationDelivery)
            .where(
                columns.organisation_id == self.organisation_id,
                columns.delivery_id == delivery_id,
                columns.notification_type == _NOTIFICATION_TYPE,
                columns.source_type == _SOURCE_TYPE,
                columns.channel == NotificationDeliveryChannel.EMAIL.value,
                claimable,
            )
            .values(
                status=NotificationDeliveryStatus.SENDING.value,
                attempt_count=columns.attempt_count + 1,
                claimed_at=claimed_at,
                claimed_by=worker_id,
                claim_token=claim_token,
                updated_at=claimed_at,
            )
            .returning(NotificationDelivery)
            .execution_options(synchronize_session=False)
        )
        delivery = (await self.session.execute(statement)).scalar_one_or_none()
        if delivery is None:
            return None
        run_columns = cast(Any, AgentRun).__table__.c
        # The source row is rechecked in the same tenant so a delivery cannot
        # authorize access to a foreign or non-successful run.
        run = (
            (
                await self.session.execute(
                    select(AgentRun).where(
                        run_columns.organisation_id == self.organisation_id,
                        run_columns.run_id == delivery.source_id,
                        run_columns.status == AgentRunStatus.SUCCEEDED.value,
                    )
                )
            )
            .scalars()
            .first()
        )
        if run is None:
            raise ValueError("delivery run is not a successful tenant run")
        return delivery, run

    async def mark_sent(
        self,
        delivery_id: UUID,
        *,
        claim_token: UUID,
        provider_resource_id: str | None,
        sent_at: datetime,
    ) -> bool:
        """Mark the fenced attempt sent and clear claim/retry metadata."""

        return await self._finish(
            delivery_id,
            expected_claim_token=claim_token,
            status=NotificationDeliveryStatus.SENT.value,
            updated_at=sent_at,
            sent_at=sent_at,
            provider_resource_id=provider_resource_id,
            next_attempt_at=None,
            claimed_at=None,
            claimed_by=None,
            claim_token=None,
            error_code=None,
            last_error=None,
        )

    async def mark_terminal(
        self,
        delivery_id: UUID,
        *,
        claim_token: UUID,
        status: str,
        error_code: str,
        error_message: str,
    ) -> bool:
        """Record a deliberate terminal outcome without reopening the source run."""

        normalized_status = status.upper()
        if normalized_status not in {
            NotificationDeliveryStatus.FAILED.value,
            NotificationDeliveryStatus.NEEDS_ATTENTION.value,
            NotificationDeliveryStatus.SKIPPED.value,
        }:
            raise ValueError("delivery terminal status is invalid")
        return await self._finish(
            delivery_id,
            expected_claim_token=claim_token,
            status=normalized_status,
            updated_at=datetime.now(UTC),
            next_attempt_at=None,
            claimed_at=None,
            claimed_by=None,
            claim_token=None,
            error_code=error_code,
            last_error=error_message,
        )

    async def release_for_retry(
        self,
        delivery_id: UUID,
        *,
        claim_token: UUID,
        next_attempt_at: datetime,
        error_code: str,
        error_message: str,
    ) -> bool:
        """Return the fenced attempt to pending with its next retry time."""

        return await self._finish(
            delivery_id,
            expected_claim_token=claim_token,
            status=NotificationDeliveryStatus.PENDING.value,
            updated_at=datetime.now(UTC),
            next_attempt_at=next_attempt_at,
            claimed_at=None,
            claimed_by=None,
            claim_token=None,
            error_code=error_code,
            last_error=error_message,
        )

    async def requeue_repairable_delivery(
        self,
        run_id: UUID,
        delivery_id: UUID,
        *,
        expected_error_code: str,
    ) -> UUID | None:
        """Requeue only the exact repairable terminal state the user observed.

        Matching the stored error code prevents a manual repair action from
        turning an unknown provider outcome into an automatic resend.
        """

        now = datetime.now(UTC)
        columns = cast(Any, NotificationDelivery).__table__.c
        statement = (
            update(NotificationDelivery)
            .where(
                columns.organisation_id == self.organisation_id,
                columns.notification_type == _NOTIFICATION_TYPE,
                columns.source_type == _SOURCE_TYPE,
                columns.source_id == run_id,
                columns.delivery_id == delivery_id,
                columns.channel == NotificationDeliveryChannel.EMAIL.value,
                columns.status == NotificationDeliveryStatus.NEEDS_ATTENTION.value,
                columns.error_code == expected_error_code,
            )
            .values(
                status=NotificationDeliveryStatus.PENDING.value,
                next_attempt_at=now,
                claimed_at=None,
                claimed_by=None,
                claim_token=None,
                error_code=None,
                last_error=None,
                updated_at=now,
            )
            .returning(columns.delivery_id)
            .execution_options(synchronize_session=False)
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def _finish(
        self,
        delivery_id: UUID,
        *,
        expected_claim_token: UUID,
        status: str,
        **values: Any,
    ) -> bool:
        """Apply a terminal or retry transition only for the current claim fence."""

        columns = cast(Any, NotificationDelivery).__table__.c
        # A stale worker may still finish after lease recovery; the claim token
        # makes that write a harmless no-op instead of a duplicate-send race.
        statement = (
            update(NotificationDelivery)
            .where(
                columns.organisation_id == self.organisation_id,
                columns.delivery_id == delivery_id,
                columns.notification_type == _NOTIFICATION_TYPE,
                columns.source_type == _SOURCE_TYPE,
                columns.channel == NotificationDeliveryChannel.EMAIL.value,
                columns.status == NotificationDeliveryStatus.SENDING.value,
                columns.claim_token == expected_claim_token,
            )
            .values(status=status, **values)
            .returning(columns.delivery_id)
            .execution_options(synchronize_session=False)
        )
        return (await self.session.execute(statement)).scalar_one_or_none() is not None

    async def list_dispatchable(
        self,
        *,
        stale_before: datetime,
        now: datetime,
        limit: int,
    ) -> list[AgentRunDeliveryDispatchCandidate]:
        """Return due pending rows and stale sending leases for recovery."""

        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        # Recovery uses a small bounded page and deterministic ordering so a
        # busy tenant is revisited on the next sweep instead of starving others.
        columns = cast(Any, NotificationDelivery).__table__.c
        statement = (
            select(
                columns.delivery_id,
                columns.source_id,
                columns.status,
                columns.created_at,
                columns.claimed_at,
            )
            .where(
                columns.organisation_id == self.organisation_id,
                columns.notification_type == _NOTIFICATION_TYPE,
                columns.source_type == _SOURCE_TYPE,
                columns.channel == NotificationDeliveryChannel.EMAIL.value,
                or_(
                    and_(
                        columns.status == NotificationDeliveryStatus.PENDING.value,
                        or_(
                            columns.next_attempt_at.is_(None),
                            columns.next_attempt_at <= now,
                        ),
                    ),
                    and_(
                        columns.status == NotificationDeliveryStatus.SENDING.value,
                        or_(
                            columns.claimed_at.is_(None),
                            columns.claimed_at < stale_before,
                        ),
                    ),
                ),
            )
            .order_by(columns.created_at, columns.delivery_id)
            .limit(limit)
        )
        rows = (await self.session.execute(statement)).all()
        return [
            AgentRunDeliveryDispatchCandidate(
                delivery_id=row.delivery_id,
                run_id=row.source_id,
                status=row.status,
                created_at=row.created_at,
                claimed_at=row.claimed_at,
            )
            for row in rows
        ]


__all__ = ["AgentRunDeliveryAccess", "AgentRunDeliveryDispatchCandidate"]
