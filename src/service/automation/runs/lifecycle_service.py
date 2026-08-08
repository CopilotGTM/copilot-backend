"""Fenced terminal and retry transitions for claimed agent runs.

Celery tasks call this after external agent work. The attempt and worker
identity are always repeated so a stale process cannot mutate a newer claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables import AgentRun, AgentRunStatus
from src.service.automation.events.contracts import AutomationEventEnvelope
from src.service.automation.events.service import AutomationEventDispatchService
from src.service.automation.producers.agent import AgentRunEventAdapter
from src.service.automation.runs.access import AgentRunClaimAccess


class LifecycleAccess(Protocol):
    """Persistence operations required by the lifecycle service."""

    async def complete_run(self, run_id: UUID, **kwargs: Any) -> AgentRun | None:
        """Write a terminal state while the supplied claim fence still owns the run."""

        ...

    async def release_for_retry(self, run_id: UUID, **kwargs: Any) -> bool:
        """Return an owned running attempt to pending."""

        ...

    async def record_trigger_outcome(self, trigger_id: UUID, **kwargs: Any) -> None:
        """Record trigger outcome as part of the durable automation lifecycle."""
        ...

    async def ensure_deliveries(self, run: AgentRun) -> tuple[UUID, ...]:
        """Create or reuse one post-run notification intent per resolved recipient."""
        ...


class AutomationEventEnqueuer(Protocol):
    """Transaction-local event append required after a fenced terminal write."""

    async def enqueue(self, envelope: AutomationEventEnvelope) -> object:
        """Queue the current Automation Event Enqueuer work while preserving its tenant and idempotency boundaries."""
        ...


@dataclass(frozen=True, slots=True)
class AgentRunCompletion:
    """Fenced completion result plus its transaction-local delivery identity."""

    updated: bool
    delivery_ids: tuple[UUID, ...] = ()


class AgentRunLifecycleService:
    """Persist execution outcomes under the claim's fencing token."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        *,
        access: LifecycleAccess | None = None,
        automation_events: AutomationEventEnqueuer | None = None,
    ) -> None:
        self.access = access or AgentRunClaimAccess(session, organisation_id)
        self.organisation_id = organisation_id
        self._automation_events = automation_events or AutomationEventDispatchService(
            session,
            organisation_id,
        )

    async def mark_succeeded(
        self,
        run_id: UUID,
        *,
        attempt_number: int,
        worker_id: str,
        output_text: str | None,
        output_json: Any | None,
        model_name: str | None,
        token_usage: dict[str, Any] | None,
        cost: Decimal | None,
        latency_ms: int,
        completed_at: datetime | None = None,
    ) -> AgentRunCompletion:
        """Persist a successful result if this worker still owns the attempt."""

        completed = _validated_transition(
            attempt_number=attempt_number,
            worker_id=worker_id,
            completed_at=completed_at,
            latency_ms=latency_ms,
        )
        return await self._complete_and_enqueue(
            run_id,
            attempt_number=attempt_number,
            worker_id=worker_id.strip(),
            status=AgentRunStatus.SUCCEEDED.value,
            completed_at=completed,
            output_text=output_text,
            output_json=output_json,
            error_code=None,
            error_message=None,
            model_name=model_name,
            token_usage=token_usage,
            cost=cost,
            latency_ms=latency_ms,
        )

    async def mark_failed(
        self,
        run_id: UUID,
        *,
        attempt_number: int,
        worker_id: str,
        error_code: str,
        error_message: str,
        validation_failure: bool,
        latency_ms: int,
        completed_at: datetime | None = None,
    ) -> bool:
        """Persist a terminal execution or pinned-contract validation failure."""

        completed = _validated_transition(
            attempt_number=attempt_number,
            worker_id=worker_id,
            completed_at=completed_at,
            latency_ms=latency_ms,
        )
        status = AgentRunStatus.VALIDATION_FAILED if validation_failure else AgentRunStatus.FAILED
        return (
            await self._complete_and_enqueue(
                run_id,
                attempt_number=attempt_number,
                worker_id=worker_id.strip(),
                status=status.value,
                completed_at=completed,
                output_text=None,
                output_json=None,
                error_code=error_code,
                error_message=error_message,
                model_name=None,
                token_usage=None,
                cost=None,
                latency_ms=latency_ms,
            )
        ).updated

    async def _complete_and_enqueue(self, run_id: UUID, **kwargs: Any) -> AgentRunCompletion:
        run = await self.access.complete_run(run_id, **kwargs)
        if run is None:
            return AgentRunCompletion(updated=False)
        delivery_ids: tuple[UUID, ...] = ()
        delivery_config = run.trigger_snapshot.get("delivery_config")
        # Delivery intent is created only for a fenced successful run and uses
        # the immutable snapshot, so later trigger edits cannot change its target.
        if (
            kwargs["status"] == AgentRunStatus.SUCCEEDED.value
            and isinstance(delivery_config, dict)
            and delivery_config.get("mode") == "email_notification"
        ):
            delivery_ids = await self.access.ensure_deliveries(run)
        envelope = AgentRunEventAdapter(self.organisation_id).from_run(run)
        # Keep the terminal run, delivery intents, event append, and trigger
        # counters in the caller's transaction; publication happens after commit.
        if envelope is not None:
            await self._automation_events.enqueue(envelope)
        await self.access.record_trigger_outcome(
            run.trigger_id,
            succeeded=kwargs["status"] == AgentRunStatus.SUCCEEDED.value,
            completed_at=kwargs["completed_at"],
            error_code=kwargs["error_code"],
            error_message=kwargs["error_message"],
        )
        return AgentRunCompletion(updated=True, delivery_ids=delivery_ids)

    async def release_for_retry(
        self,
        run_id: UUID,
        *,
        attempt_number: int,
        worker_id: str,
        error_code: str,
        error_message: str,
    ) -> bool:
        """Release an owned claim so a retry can atomically claim it again."""

        _validated_claim(attempt_number, worker_id)
        return await self.access.release_for_retry(
            run_id,
            attempt_number=attempt_number,
            worker_id=worker_id.strip(),
            error_code=error_code,
            error_message=error_message,
        )


def _validated_transition(
    *,
    attempt_number: int,
    worker_id: str,
    completed_at: datetime | None,
    latency_ms: int,
) -> datetime:
    _validated_claim(attempt_number, worker_id)
    if latency_ms < 0:
        raise ValueError("latency_ms must be non-negative")
    value = completed_at or datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("completed_at must include timezone information")
    return value


def _validated_claim(attempt_number: int, worker_id: str) -> None:
    if attempt_number < 1:
        raise ValueError("attempt_number must be positive")
    if not worker_id.strip():
        raise ValueError("worker_id must be a non-empty string")
