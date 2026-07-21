"""Validated claim orchestration for durable agent runs.

The future execution worker calls this service before leaving the database
transaction. A successful claim is mapped from its pinned snapshots, allowing
the worker to commit, release the session, and then execute without consulting
mutable trigger or agent definitions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables import AgentRun
from src.infra.utils.logger import get_logger
from src.service.automation.runs.access import AgentRunClaimAccess
from src.service.automation.triggers.runtime import (
    AgentRunRuntimeEnvelope,
    map_agent_run_runtime,
)

logger = get_logger("agent_run_claim")

# copilotgtm: V1 has no lease heartbeat, so use a conservative initial window.
# Add heartbeat renewal before supporting agent executions longer than 30 minutes.
DEFAULT_AGENT_RUN_LEASE = timedelta(minutes=30)


class InvalidClaimedAgentRunError(ValueError):
    """Carry the winning claim fence when its pinned snapshot is invalid."""

    def __init__(
        self,
        *,
        run_id: UUID,
        attempt_number: int,
        worker_id: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.attempt_number = attempt_number
        self.worker_id = worker_id


class ClaimAccess(Protocol):
    """Persistence operation required by the claim service."""

    async def claim_run(
        self,
        run_id: UUID,
        *,
        worker_id: str,
        claimed_at: datetime,
        stale_before: datetime,
    ) -> AgentRun | None:
        """Return the row won by this worker's atomic claim, if any."""

        ...


class AgentRunClaimService:
    """Validate and claim one durable run for a future execution worker."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        *,
        access: ClaimAccess | None = None,
        lease_timeout: timedelta = DEFAULT_AGENT_RUN_LEASE,
    ) -> None:
        if lease_timeout <= timedelta(0):
            raise ValueError("lease_timeout must be positive")

        self.session = session
        self.organisation_id = organisation_id
        self.access = access or AgentRunClaimAccess(session, organisation_id)
        self.lease_timeout = lease_timeout

    async def claim_run(
        self,
        run_id: UUID,
        *,
        worker_id: str,
        claimed_at: datetime | None = None,
    ) -> AgentRunRuntimeEnvelope | None:
        """Claim one run and return its immutable execution contract.

        Returning None is an expected duplicate-delivery outcome. The caller
        owns the transaction and must commit the successful claim before releasing
        the database session and beginning external agent work.
        """

        normalized_worker_id = worker_id.strip()
        if not normalized_worker_id:
            raise ValueError("worker_id must be a non-empty string")

        claim_time = claimed_at or datetime.now(UTC)
        if claim_time.tzinfo is None or claim_time.utcoffset() is None:
            raise ValueError("claimed_at must include timezone information")

        run = await self.access.claim_run(
            run_id,
            worker_id=normalized_worker_id,
            claimed_at=claim_time,
            stale_before=claim_time - self.lease_timeout,
        )
        if run is None:
            logger.debug(
                "Agent run claim skipped org=%s run=%s worker=%s",
                self.organisation_id,
                run_id,
                normalized_worker_id,
            )
            return None

        try:
            envelope = map_agent_run_runtime(run)
        except ValueError as exc:
            # The UPDATE already won this attempt. Preserve its fence so the
            # worker can mark the corrupt snapshot terminal in the same transaction.
            raise InvalidClaimedAgentRunError(
                run_id=run.run_id,
                attempt_number=run.attempt_count,
                worker_id=run.claimed_by or normalized_worker_id,
                message=str(exc),
            ) from exc
        logger.info(
            "Agent run claimed org=%s run=%s worker=%s attempt=%d",
            self.organisation_id,
            run_id,
            normalized_worker_id,
            run.attempt_count,
        )
        return envelope
