"""Pinned agent-run snapshot mapping tests for the future execution worker."""

from __future__ import annotations

from uuid import uuid4

import pytest
from src.domain.models.organisation_tables import AgentRun
from src.service.automation.triggers.runtime import map_agent_run_runtime


def run() -> AgentRun:
    agent_id, deal_id = uuid4(), uuid4()
    return AgentRun(
        organisation_id=uuid4(),
        owner_user_id=uuid4(),
        agent_id=agent_id,
        trigger_id=uuid4(),
        agent_revision=4,
        idempotency_key="trigger:event",
        source_domain="crm",
        event_type="crm.deal.stage_changed",
        source_event_id=str(uuid4()),
        trigger_snapshot={"task_config": {"goal": "Prepare next steps"}},
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
        input_snapshot={
            "schema_key": "crm.deal.stage_changed.v1",
            "subject": {"type": "deal", "id": str(deal_id)},
            "deal": {"deal_id": str(deal_id)},
            "event": {"event_id": str(uuid4())},
        },
        attempt_count=1,
        claimed_by="agent-worker-1",
    )


def test_runtime_mapper_uses_only_pinned_run_snapshots() -> None:
    agent_run = run()
    agent_run.attempt_count = 2
    agent_run.claimed_by = "agent-worker-2"

    envelope = map_agent_run_runtime(agent_run)

    assert envelope.run_id == agent_run.run_id
    assert envelope.attempt_number == 2
    assert envelope.worker_id == "agent-worker-2"
    assert envelope.agent_id == agent_run.agent_id
    assert envelope.agent_revision == 4
    assert envelope.trigger_id == agent_run.trigger_id
    assert envelope.source_domain == "crm"
    assert envelope.event_type == "crm.deal.stage_changed"
    assert envelope.source_event_id == agent_run.source_event_id
    assert envelope.subject_type == "deal"
    assert envelope.subject_id == agent_run.input_snapshot["subject"]["id"]
    assert str(envelope.deal_id) == agent_run.input_snapshot["deal"]["deal_id"]
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
def test_runtime_mapper_fails_closed_on_inconsistent_snapshot(
    snapshot_change: dict[str, object], message: str
) -> None:
    agent_run = run()
    agent_run.agent_snapshot = {**agent_run.agent_snapshot, **snapshot_change}

    with pytest.raises(ValueError, match=message):
        map_agent_run_runtime(agent_run)


def test_runtime_mapper_accepts_contact_subject_without_deal() -> None:
    """A contact lifecycle trigger must reach execution without inventing deal scope."""

    agent_run = run()
    contact_id = uuid4()
    agent_run.event_type = "crm.contact.created"
    agent_run.input_snapshot = {
        "schema_key": "crm.contact.created.v1",
        "subject": {"type": "contact", "id": str(contact_id)},
        "contact": {"contact_id": str(contact_id), "company_id": None},
        "event": {"semantic_event_type": "crm.contact.created"},
    }

    envelope = map_agent_run_runtime(agent_run)

    assert envelope.subject_type == "contact"
    assert envelope.subject_id == str(contact_id)
    assert envelope.deal_id is None


def test_runtime_mapper_rejects_deal_subject_identity_mismatch() -> None:
    """Pinned subject and deal boundaries cannot point to different CRM records."""

    agent_run = run()
    agent_run.input_snapshot["subject"]["id"] = str(uuid4())

    with pytest.raises(ValueError, match="subject id does not match deal_id"):
        map_agent_run_runtime(agent_run)
