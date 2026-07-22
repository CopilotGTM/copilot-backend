"""Fail-closed mapping from durable agent runs to future worker runtime input.

This module does not execute an agent. It proves the persisted run contains a self-consistent
pinned definition so a later worker never falls back to mutable current-agent configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from src.domain.models.organisation_tables import AgentRun


@dataclass(frozen=True)
class AgentRunRuntimeEnvelope:
    """Pinned execution contract passed from a claimed run to the agent runtime adapter."""

    run_id: UUID
    attempt_number: int
    worker_id: str
    organisation_id: UUID
    owner_user_id: UUID
    agent_id: UUID
    agent_revision: int
    trigger_id: UUID
    source_domain: str
    event_type: str
    source_event_id: str
    subject_type: str
    subject_id: str
    deal_id: UUID | None
    agent_type: str
    prompt: str
    model_name: str | None
    model_config: dict[str, Any]
    allowed_tools: tuple[str, ...]
    allowed_skills: tuple[str, ...]
    max_turns: int | None
    input_schema: dict[str, Any] | None
    output_mode: str
    output_schema: dict[str, Any] | None
    task_goal: str | None
    trigger_snapshot: dict[str, Any]
    input_snapshot: dict[str, Any]


def map_agent_run_runtime(run: AgentRun) -> AgentRunRuntimeEnvelope:
    """Validate and map one run without consulting the mutable agent definition table."""

    snapshot = run.agent_snapshot
    snapshot_agent_id = _required_uuid(snapshot, "agent_id")
    if snapshot_agent_id != run.agent_id:
        raise ValueError("agent_snapshot agent_id does not match agent run")

    snapshot_revision = snapshot.get("revision")
    if not isinstance(snapshot_revision, int) or isinstance(snapshot_revision, bool):
        raise ValueError("agent_snapshot revision must be an integer")
    if snapshot_revision != run.agent_revision:
        raise ValueError("agent_snapshot revision does not match agent run")
    if run.trigger_id is None:
        raise ValueError("triggered agent run must include trigger_id")
    if run.attempt_count < 1:
        raise ValueError("agent run attempt_count must be positive after claim")
    if not run.claimed_by or not run.claimed_by.strip():
        raise ValueError("agent run claimed_by must identify the worker")

    prompt = snapshot.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("agent_snapshot prompt must be a non-empty string")

    source_domain = _required_run_string(run.source_domain, "source_domain")
    subject = run.input_snapshot.get("subject")
    if not isinstance(subject, dict):
        raise ValueError("input_snapshot subject must be an object")
    subject_type = _required_string(subject, "type")
    subject_id = _required_string(subject, "id")

    deal = run.input_snapshot.get("deal")
    if deal is None:
        deal_id = None
    elif isinstance(deal, dict):
        deal_id = _required_uuid(deal, "deal_id")
    else:
        raise ValueError("input_snapshot deal must be null or an object")
    if subject_type == "deal" and str(deal_id) != subject_id:
        raise ValueError("input_snapshot subject id does not match deal_id")

    if source_domain == "crm":
        identity_key = {
            "deal": "deal_id",
            "contact": "contact_id",
            "account": "account_id",
        }.get(subject_type)
        entity = run.input_snapshot.get(subject_type)
        if identity_key is None or not isinstance(entity, dict):
            raise ValueError("CRM input_snapshot subject type is not supported")
        if _required_string(entity, identity_key) != subject_id:
            raise ValueError("CRM input_snapshot subject identity does not match entity snapshot")

    task_config = run.trigger_snapshot.get("task_config", {})
    if not isinstance(task_config, dict):
        raise ValueError("trigger_snapshot task_config must be an object")
    task_goal = task_config.get("goal")
    if task_goal is not None and not isinstance(task_goal, str):
        raise ValueError("trigger_snapshot task goal must be a string")

    return AgentRunRuntimeEnvelope(
        run_id=run.run_id,
        attempt_number=run.attempt_count,
        worker_id=run.claimed_by.strip(),
        organisation_id=run.organisation_id,
        owner_user_id=run.owner_user_id,
        agent_id=run.agent_id,
        agent_revision=run.agent_revision,
        trigger_id=run.trigger_id,
        source_domain=source_domain,
        event_type=_required_run_string(run.event_type, "event_type"),
        source_event_id=_required_run_string(run.source_event_id, "source_event_id"),
        subject_type=subject_type,
        subject_id=subject_id,
        deal_id=deal_id,
        agent_type=_required_string(snapshot, "agent_type"),
        prompt=prompt.strip(),
        model_name=_optional_string(snapshot, "model_name"),
        model_config=_required_dict(snapshot, "model_config"),
        allowed_tools=_string_tuple(snapshot, "allowed_tools"),
        allowed_skills=_string_tuple(snapshot, "allowed_skills"),
        max_turns=_optional_positive_int(snapshot, "max_turns"),
        input_schema=_optional_dict(snapshot, "input_schema"),
        output_mode=_required_string(snapshot, "output_mode"),
        output_schema=_optional_dict(snapshot, "output_schema"),
        task_goal=task_goal.strip() if task_goal else None,
        trigger_snapshot=dict(run.trigger_snapshot),
        input_snapshot=dict(run.input_snapshot),
    )


def _required_uuid(value: dict[str, Any], key: str) -> UUID:
    try:
        return UUID(str(value[key]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a UUID") from exc


def _required_run_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"agent run {field_name} must be a non-empty string")
    return value.strip()


def _required_string(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"agent_snapshot {key} must be a non-empty string")
    return item.strip()


def _optional_string(value: dict[str, Any], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"agent_snapshot {key} must be null or a non-empty string")
    return item.strip()


def _required_dict(value: dict[str, Any], key: str) -> dict[str, Any]:
    item = value.get(key)
    if not isinstance(item, dict):
        raise ValueError(f"agent_snapshot {key} must be an object")
    return dict(item)


def _optional_dict(value: dict[str, Any], key: str) -> dict[str, Any] | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, dict):
        raise ValueError(f"agent_snapshot {key} must be null or an object")
    return dict(item)


def _string_tuple(value: dict[str, Any], key: str) -> tuple[str, ...]:
    item = value.get(key)
    if not isinstance(item, list) or any(not isinstance(entry, str) for entry in item):
        raise ValueError(f"agent_snapshot {key} must be an array of strings")
    return tuple(item)


def _optional_positive_int(value: dict[str, Any], key: str) -> int | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, int) or isinstance(item, bool) or item < 1:
        raise ValueError(f"agent_snapshot {key} must be null or a positive integer")
    return item
