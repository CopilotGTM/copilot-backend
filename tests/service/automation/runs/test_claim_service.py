"""Behavior tests for the generic agent-run claim service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables import AgentRun
from src.service.automation.events.contracts import AutomationEventEnvelope
from src.service.automation.runs.claim_service import (
    AgentRunClaimService,
    InvalidClaimedAgentRunError,
)


def agent_run() -> AgentRun:
    """Build the agent run fixture used by the surrounding regression cases."""
    organisation_id, principal_id = uuid4(), uuid4()
    agent_id, deal_id, trigger_id, event_id = uuid4(), uuid4(), uuid4(), uuid4()
    event = AutomationEventEnvelope.model_validate(
        {
            "event_id": str(event_id),
            "organisation_id": str(organisation_id),
            "event_key": "crm.deal.updated",
            "route": {"mode": "match"},
            "occurred_at": datetime(2026, 7, 14, 8, 0, tzinfo=UTC),
            "source": {
                "domain": "crm",
                "system": "internal",
                "operation": "update",
                "mode": "interactive",
                "source_event_id": str(event_id),
            },
            "actor": {"type": "user", "id": str(principal_id)},
            "subject": {"type": "deal", "id": str(deal_id)},
            "record": {"id": str(deal_id), "owner_id": str(principal_id)},
            "changes": {"name": {"before": "Old", "after": "New"}},
            "correlation_id": "request-123",
            "idempotency_key": f"crm.deal.updated:{event_id}",
        }
    )
    return AgentRun(
        organisation_id=organisation_id,
        created_by_user_id=principal_id,
        execution_principal_type="user",
        execution_principal_id=principal_id,
        audience_mode="principal",
        audience_config={},
        agent_id=agent_id,
        trigger_id=trigger_id,
        agent_revision=3,
        idempotency_key=f"trigger:{trigger_id}:event:{event_id}",
        event_id=event_id,
        event_key=event.event_key,
        correlation_id="request-123",
        trigger_snapshot={
            "trigger_id": str(trigger_id),
            "task_config": {"goal": "Prepare next steps"},
        },
        agent_snapshot={
            "agent_id": str(agent_id),
            "revision": 3,
            "agent_type": "general",
            "prompt": "Act as a deal coach.",
            "model_name": "gpt-5-mini",
            "model_config": {},
            "allowed_tools": ["search_deals"],
            "allowed_skills": ["crm"],
            "max_turns": 8,
            "input_schema": None,
            "output_mode": "text",
            "output_schema": None,
        },
        input_snapshot=event.model_dump(mode="json"),
        attempt_count=1,
        claimed_by="agent-worker-3",
    )


class FakeAccess:
    """Represent the Fake Access test double used by the surrounding regression cases."""

    def __init__(self, returned: AgentRun | None) -> None:
        self.returned = returned
        self.calls: list[dict[str, Any]] = []

    async def claim_run(
        self,
        run_id: UUID,
        *,
        worker_id: str,
        claimed_at: datetime,
        stale_before: datetime,
    ) -> AgentRun | None:
        """Expose the claim run operation expected by the Fake Access test double."""
        self.calls.append(
            {
                "run_id": run_id,
                "worker_id": worker_id,
                "claimed_at": claimed_at,
                "stale_before": stale_before,
            }
        )
        return self.returned


@pytest.mark.asyncio
async def test_claim_returns_pinned_runtime_envelope_and_exact_stale_cutoff() -> None:
    # Claiming must not re-read the mutable agent definition; execution receives
    # only the snapshot that activation pinned into this durable run.
    """Verify the contract that claim returns pinned runtime envelope and exact stale cutoff."""
    run = agent_run()
    access = FakeAccess(run)
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    lease_timeout = timedelta(minutes=45)
    service = AgentRunClaimService(
        object(),  # type: ignore[arg-type]
        run.organisation_id,
        access=access,
        lease_timeout=lease_timeout,
    )

    envelope = await service.claim_run(
        run.run_id,
        worker_id="  agent-worker-3  ",
        claimed_at=now,
    )

    assert envelope is not None
    assert envelope.run_id == run.run_id
    assert envelope.prompt == "Act as a deal coach."
    assert envelope.subject_type == "deal"
    assert envelope.subject_id == str(run.input_snapshot["subject"]["id"])
    assert access.calls == [
        {
            "run_id": run.run_id,
            "worker_id": "agent-worker-3",
            "claimed_at": now,
            "stale_before": now - lease_timeout,
        }
    ]


@pytest.mark.asyncio
async def test_claim_returns_none_when_another_worker_won() -> None:
    # Duplicate queue deliveries are expected; a lost compare-and-set is a clean
    # no-op, not an execution error or a second agent run.
    """Verify the contract that claim returns none when another worker won."""
    access = FakeAccess(None)
    service = AgentRunClaimService(object(), uuid4(), access=access)  # type: ignore[arg-type]

    assert (
        await service.claim_run(
            uuid4(),
            worker_id="agent-worker-4",
            claimed_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
        )
        is None
    )


@pytest.mark.asyncio
async def test_claim_rejects_blank_worker_identity_before_database_access() -> None:
    # A worker identity is required for lease ownership and production incident
    # diagnosis; blank ownership would make stale-run recovery ambiguous.
    """Verify the contract that claim rejects blank worker identity before database access."""
    access = FakeAccess(None)
    service = AgentRunClaimService(object(), uuid4(), access=access)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="worker_id"):
        await service.claim_run(
            uuid4(),
            worker_id="  ",
            claimed_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
        )

    assert access.calls == []


@pytest.mark.asyncio
async def test_claim_rejects_naive_timestamp_before_database_access() -> None:
    # Claim timestamps are compared with timezone-aware PostgreSQL values; a
    # naive timestamp could reclaim a healthy run at the wrong wall-clock time.
    """Verify the contract that claim rejects naive timestamp before database access."""
    access = FakeAccess(None)
    service = AgentRunClaimService(object(), uuid4(), access=access)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="timezone"):
        await service.claim_run(
            uuid4(),
            worker_id="agent-worker-4",
            claimed_at=datetime(2026, 7, 14, 9, 0),
        )

    assert access.calls == []


def test_claim_service_rejects_non_positive_lease() -> None:
    # A non-positive lease would make every running row immediately claimable
    # and allow concurrent workers to duplicate side effects.
    """Verify the contract that claim service rejects non positive lease."""
    with pytest.raises(ValueError, match="lease_timeout"):
        AgentRunClaimService(
            object(),  # type: ignore[arg-type]
            uuid4(),
            access=FakeAccess(None),
            lease_timeout=timedelta(0),
        )


@pytest.mark.asyncio
async def test_invalid_claimed_snapshot_exposes_fence_for_terminal_failure() -> None:
    """Verify the contract that invalid claimed snapshot exposes fence for terminal failure."""
    run = agent_run()
    run.agent_snapshot = {**run.agent_snapshot, "prompt": ""}
    service = AgentRunClaimService(
        object(),  # type: ignore[arg-type]
        run.organisation_id,
        access=FakeAccess(run),
    )

    with pytest.raises(InvalidClaimedAgentRunError) as exc:
        await service.claim_run(
            run.run_id,
            worker_id=run.claimed_by or "",
            claimed_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
        )

    assert exc.value.run_id == run.run_id
    assert exc.value.attempt_number == run.attempt_count
    assert exc.value.worker_id == run.claimed_by
