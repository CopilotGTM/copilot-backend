"""Behavior tests for the generic agent-run claim service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables import AgentRun
from src.service.automation.runs.claim_service import (
    AgentRunClaimService,
    InvalidClaimedAgentRunError,
)


def agent_run() -> AgentRun:
    agent_id, deal_id = uuid4(), uuid4()
    return AgentRun(
        organisation_id=uuid4(),
        owner_user_id=uuid4(),
        agent_id=agent_id,
        trigger_id=uuid4(),
        agent_revision=3,
        idempotency_key="trigger:event",
        source_domain="crm",
        event_type="crm.deal.stage_changed",
        source_event_id=str(uuid4()),
        trigger_snapshot={"task_config": {"goal": "Prepare next steps"}},
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
        input_snapshot={
            "schema_key": "crm.deal.stage_changed.v1",
            "subject": {"type": "deal", "id": str(deal_id)},
            "deal": {"deal_id": str(deal_id)},
            "event": {"event_id": str(uuid4())},
        },
        attempt_count=1,
        claimed_by="agent-worker-3",
    )


class FakeAccess:
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
    assert envelope.subject_id == str(run.input_snapshot["deal"]["deal_id"])
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
    with pytest.raises(ValueError, match="lease_timeout"):
        AgentRunClaimService(
            object(),  # type: ignore[arg-type]
            uuid4(),
            access=FakeAccess(None),
            lease_timeout=timedelta(0),
        )


@pytest.mark.asyncio
async def test_invalid_claimed_snapshot_exposes_fence_for_terminal_failure() -> None:
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
