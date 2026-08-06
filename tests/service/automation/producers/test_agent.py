"""Canonical producer contracts for terminal agent runs."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables import AgentRun, AgentRunStatus
from src.service.automation.producers.agent import AgentRunEventAdapter
from src.service.automation.triggers.registry import AUTOMATION_REGISTRY


def terminal_run(organisation_id, *, status=AgentRunStatus.SUCCEEDED) -> AgentRun:
    """Build the terminal run fixture used by the surrounding regression cases."""
    return AgentRun(
        organisation_id=organisation_id,
        created_by_user_id=uuid4(),
        execution_principal_type="user",
        execution_principal_id=uuid4(),
        audience_mode="principal",
        audience_config={},
        agent_id=uuid4(),
        trigger_id=uuid4(),
        agent_revision=3,
        status=status,
        idempotency_key=f"run:{uuid4()}",
        event_id=uuid4(),
        event_key="crm.deal.updated",
        correlation_id="chain-17",
        causation_depth=1,
        trigger_snapshot={},
        agent_snapshot={},
        input_snapshot={},
        completed_at=datetime(2026, 8, 5, 10, tzinfo=UTC),
        output_text="Private agent output",
        output_json={"private": "result"},
        error_message="Private provider failure",
    )


@pytest.mark.parametrize(
    ("status", "event_key"),
    [
        (AgentRunStatus.SUCCEEDED, "agent.run.completed"),
        (AgentRunStatus.FAILED, "agent.run.failed"),
        (AgentRunStatus.VALIDATION_FAILED, "agent.run.failed"),
        (AgentRunStatus.CANCELLED, "agent.run.cancelled"),
    ],
)
def test_terminal_agent_event_is_private_and_has_child_lineage(status, event_key) -> None:
    """Verify the contract that terminal agent event is private and has child lineage."""
    organisation_id = uuid4()
    run = terminal_run(organisation_id, status=status)

    envelope = AgentRunEventAdapter(organisation_id).from_run(run)

    assert envelope is not None
    assert envelope.event_key == event_key
    if event_key != "agent.run.cancelled":
        AUTOMATION_REGISTRY.event(event_key).validate_envelope(envelope)
    assert envelope.subject.id == str(run.run_id)
    assert envelope.record == {
        "id": str(run.run_id),
        "agent_id": str(run.agent_id),
        "trigger_id": str(run.trigger_id),
        "status": status.value,
        "event_key": run.event_key,
        "created_by_user_id": str(run.created_by_user_id),
    }
    assert envelope.caused_by_run_id == run.run_id
    assert envelope.causation_depth == 2
    serialized = envelope.model_dump_json()
    assert "Private agent output" not in serialized
    assert "Private provider failure" not in serialized
    assert '"private":"result"' not in serialized


def test_terminal_event_is_replay_stable_and_rejects_non_terminal_rows() -> None:
    """Verify the contract that terminal event is replay stable and rejects non terminal rows."""
    organisation_id = uuid4()
    run = terminal_run(organisation_id)
    adapter = AgentRunEventAdapter(organisation_id)

    first = adapter.from_run(run)
    replay = adapter.from_run(run)

    assert first is not None
    assert replay is not None
    assert first.event_id == replay.event_id
    assert first.idempotency_key == replay.idempotency_key

    run.status = AgentRunStatus.RUNNING
    with pytest.raises(ValueError, match="terminal"):
        adapter.from_run(run)


def test_terminal_event_stops_at_the_chain_depth_limit() -> None:
    """Verify the contract that terminal event stops at the chain depth limit."""
    organisation_id = uuid4()
    run = terminal_run(organisation_id)
    run.causation_depth = 3

    assert AgentRunEventAdapter(organisation_id).from_run(run) is None
