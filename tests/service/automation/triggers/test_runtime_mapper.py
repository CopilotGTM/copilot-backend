"""Pinned agent-run snapshot mapping tests for the execution worker."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables import AgentRun
from src.service.automation.events.contracts import AutomationEventEnvelope
from src.service.automation.triggers.runtime import map_agent_run_runtime


def event(
    organisation_id: UUID,
    *,
    event_id: UUID | None = None,
    subject_type: str = "deal",
) -> AutomationEventEnvelope:
    """Build the event fixture used by the surrounding regression cases."""
    event_id = event_id or uuid4()
    subject_id = uuid4()
    return AutomationEventEnvelope.model_validate(
        {
            "event_id": str(event_id),
            "organisation_id": str(organisation_id),
            "event_key": f"crm.{subject_type}.created",
            "route": {"mode": "match"},
            "occurred_at": datetime(2026, 8, 5, tzinfo=UTC),
            "source": {
                "domain": "crm",
                "system": "internal",
                "operation": "create",
                "mode": "interactive",
                "source_event_id": str(event_id),
            },
            "actor": {"type": "user", "id": str(uuid4())},
            "subject": {"type": subject_type, "id": str(subject_id)},
            "record": {"id": str(subject_id), "name": "Acme"},
            "correlation_id": "request-123",
            "idempotency_key": f"crm.{subject_type}.created:{event_id}",
        }
    )


def run(*, subject_type: str = "deal") -> AgentRun:
    """Build the run fixture used by the surrounding regression cases."""
    organisation_id, principal_id, agent_id, trigger_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    source_event = event(organisation_id, subject_type=subject_type)
    return AgentRun(
        organisation_id=organisation_id,
        created_by_user_id=principal_id,
        execution_principal_type="user",
        execution_principal_id=principal_id,
        audience_mode="principal",
        audience_config={},
        agent_id=agent_id,
        trigger_id=trigger_id,
        agent_revision=4,
        idempotency_key=f"trigger:{trigger_id}:event:{source_event.event_id}",
        event_id=source_event.event_id,
        event_key=source_event.event_key,
        correlation_id="request-123",
        caused_by_run_id=None,
        causation_depth=0,
        trigger_snapshot={
            "trigger_id": str(trigger_id),
            "task_config": {"goal": "Prepare next steps"},
        },
        agent_snapshot={
            "agent_id": str(agent_id),
            "revision": 4,
            "agent_type": "general",
            "prompt": "Act as a deal coach.",
            "model_name": "gpt-5-mini",
            "model_config": {"temperature": 0.2},
            "allowed_tools": ["search_deals"],
            "allowed_skills": ["crm"],
            "max_turns": 8,
            "input_schema": None,
            "output_mode": "text",
            "output_schema": None,
        },
        input_snapshot=source_event.model_dump(mode="json"),
        attempt_count=1,
        claimed_by="agent-worker-1",
    )


def test_runtime_mapper_uses_only_pinned_run_snapshots() -> None:
    """Verify the contract that runtime mapper uses only pinned run snapshots."""
    agent_run = run()
    agent_run.attempt_count = 2
    agent_run.claimed_by = "agent-worker-2"

    envelope = map_agent_run_runtime(agent_run)

    assert envelope.run_id == agent_run.run_id
    assert envelope.attempt_number == 2
    assert envelope.worker_id == "agent-worker-2"
    assert envelope.execution_principal_type == "user"
    assert envelope.principal_user_id == agent_run.execution_principal_id
    assert envelope.audience_mode == "principal"
    assert envelope.event_id == agent_run.event_id
    assert envelope.event_key == "crm.deal.created"
    assert envelope.correlation_id == "request-123"
    assert envelope.subject_type == "deal"
    assert envelope.subject_id == agent_run.input_snapshot["subject"]["id"]
    assert str(envelope.deal_id) == envelope.subject_id
    assert envelope.prompt == "Act as a deal coach."
    assert envelope.allowed_tools == ("search_deals",)
    assert envelope.task_goal == "Prepare next steps"
    assert envelope.trigger_snapshot == agent_run.trigger_snapshot
    assert envelope.input_snapshot == agent_run.input_snapshot


@pytest.mark.parametrize(
    ("snapshot_change", "message"),
    [
        ({"revision": 3}, "revision"),
        ({"agent_id": str(uuid4())}, "agent_id"),
        ({"prompt": ""}, "prompt"),
    ],
)
def test_runtime_mapper_fails_closed_on_inconsistent_agent_snapshot(
    snapshot_change: dict[str, object], message: str
) -> None:
    """Verify the contract that runtime mapper fails closed on inconsistent agent snapshot."""
    agent_run = run()
    agent_run.agent_snapshot = {**agent_run.agent_snapshot, **snapshot_change}

    with pytest.raises(ValueError, match=message):
        map_agent_run_runtime(agent_run)


def test_runtime_mapper_accepts_contact_subject_without_deal() -> None:
    """Verify the contract that runtime mapper accepts contact subject without deal."""
    agent_run = run(subject_type="contact")

    envelope = map_agent_run_runtime(agent_run)

    assert envelope.subject_type == "contact"
    assert envelope.subject_id == agent_run.input_snapshot["subject"]["id"]
    assert envelope.deal_id is None


def test_runtime_mapper_rejects_event_row_mismatch() -> None:
    """Verify the contract that runtime mapper rejects event row mismatch."""
    agent_run = run()
    agent_run.event_key = "crm.contact.created"

    with pytest.raises(ValueError, match="event_key"):
        map_agent_run_runtime(agent_run)


def test_runtime_mapper_rejects_tools_for_organisation_service_principal() -> None:
    """Verify the contract that runtime mapper rejects tools for organisation service principal."""
    agent_run = run()
    agent_run.execution_principal_type = "organisation_service"
    agent_run.execution_principal_id = agent_run.organisation_id
    agent_run.audience_mode = "none"

    with pytest.raises(ValueError, match="organisation service agent cannot use"):
        map_agent_run_runtime(agent_run)


def test_runtime_mapper_rejects_skills_for_organisation_service_principal() -> None:
    """Verify the contract that runtime mapper rejects skills for organisation service principal."""
    agent_run = run()
    agent_run.execution_principal_type = "organisation_service"
    agent_run.execution_principal_id = agent_run.organisation_id
    agent_run.audience_mode = "none"
    agent_run.agent_snapshot = {
        **agent_run.agent_snapshot,
        "allowed_tools": [],
        "allowed_skills": ["crm"],
    }

    with pytest.raises(ValueError, match="cannot use skills"):
        map_agent_run_runtime(agent_run)


def test_runtime_mapper_rejects_retry_unsafe_personal_mutations() -> None:
    """Verify the contract that runtime mapper rejects retry unsafe personal mutations."""
    agent_run = run()
    agent_run.agent_snapshot = {
        **agent_run.agent_snapshot,
        "allowed_tools": ["update_deal_fields"],
    }

    with pytest.raises(ValueError, match="retry-safe"):
        map_agent_run_runtime(agent_run)
