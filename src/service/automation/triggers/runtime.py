"""Fail-closed mapping from a claimed run to immutable runtime input."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from src.domain.models.organisation_tables import AgentRun
from src.service.automation.events.contracts import AutomationEventEnvelope
from src.service.automation.triggers.capabilities import RETRY_UNSAFE_AUTOMATION_TOOLS


@dataclass(frozen=True)
class AgentRunRuntimeEnvelope:
    """Pinned execution contract passed to the agent runtime adapter."""

    run_id: UUID
    attempt_number: int
    worker_id: str
    organisation_id: UUID
    execution_principal_type: str
    execution_principal_id: UUID
    principal_user_id: UUID | None
    audience_mode: str
    audience_config: dict[str, Any]
    agent_id: UUID
    agent_revision: int
    trigger_id: UUID
    event_id: UUID
    event_key: str
    correlation_id: str
    caused_by_run_id: UUID | None
    causation_depth: int
    subject_type: str | None
    subject_id: str | None
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
    """Validate one claimed row without consulting mutable trigger definitions."""

    snapshot = run.agent_snapshot
    snapshot_agent_id = _required_uuid(snapshot, "agent_id")
    if snapshot_agent_id != run.agent_id:
        raise ValueError("agent_snapshot agent_id does not match agent run")
    snapshot_revision = snapshot.get("revision")
    if not isinstance(snapshot_revision, int) or isinstance(snapshot_revision, bool):
        raise ValueError("agent_snapshot revision must be an integer")
    if snapshot_revision != run.agent_revision:
        raise ValueError("agent_snapshot revision does not match agent run")
    if run.attempt_count < 1:
        raise ValueError("agent run attempt_count must be positive after claim")
    if not run.claimed_by or not run.claimed_by.strip():
        raise ValueError("agent run claimed_by must identify the worker")

    try:
        event = AutomationEventEnvelope.model_validate(run.input_snapshot)
    except ValidationError as exc:
        raise ValueError("input_snapshot must contain a valid automation envelope") from exc
    if event.event_id != run.event_id:
        raise ValueError("input_snapshot event_id does not match agent run")
    if event.organisation_id != run.organisation_id:
        raise ValueError("input_snapshot organisation_id does not match agent run")
    if event.event_key != run.event_key:
        raise ValueError("input_snapshot event_key does not match agent run")
    if event.caused_by_run_id != run.caused_by_run_id:
        raise ValueError("input_snapshot caused_by_run_id does not match agent run")
    if event.causation_depth != run.causation_depth:
        raise ValueError("input_snapshot causation_depth does not match agent run")

    trigger_snapshot_id = _required_uuid(run.trigger_snapshot, "trigger_id")
    if trigger_snapshot_id != run.trigger_id:
        raise ValueError("trigger_snapshot trigger_id does not match agent run")
    prompt = snapshot.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("agent_snapshot prompt must be a non-empty string")

    principal_type = _value(run.execution_principal_type)
    if principal_type not in {"user", "organisation_service"}:
        raise ValueError("agent run execution principal type is invalid")
    principal_user_id = run.execution_principal_id if principal_type == "user" else None
    allowed_tools = _string_tuple(snapshot, "allowed_tools")
    allowed_skills = _string_tuple(snapshot, "allowed_skills")
    if set(allowed_tools) & RETRY_UNSAFE_AUTOMATION_TOOLS:
        raise ValueError("automation mutating tools are disabled until actions are retry-safe")
    if principal_type == "organisation_service" and allowed_tools:
        # Organization service bindings are analysis-only until each tool has a
        # code-owned service-principal capability policy.
        raise ValueError("organisation service agent cannot use user-scoped tools")
    if principal_type == "organisation_service" and allowed_skills:
        raise ValueError("organisation service agent cannot use skills")

    subject_type = event.subject.type
    subject_id = event.subject.id
    deal_id = _event_deal_id(event)
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
        execution_principal_type=principal_type,
        execution_principal_id=run.execution_principal_id,
        principal_user_id=principal_user_id,
        audience_mode=_value(run.audience_mode),
        audience_config=dict(run.audience_config),
        agent_id=run.agent_id,
        agent_revision=run.agent_revision,
        trigger_id=run.trigger_id,
        event_id=run.event_id,
        event_key=run.event_key,
        correlation_id=run.correlation_id,
        caused_by_run_id=run.caused_by_run_id,
        causation_depth=run.causation_depth,
        subject_type=subject_type,
        subject_id=subject_id,
        deal_id=deal_id,
        agent_type=_required_string(snapshot, "agent_type"),
        prompt=prompt.strip(),
        model_name=_optional_string(snapshot, "model_name"),
        model_config=_required_dict(snapshot, "model_config"),
        allowed_tools=allowed_tools,
        allowed_skills=allowed_skills,
        max_turns=_optional_positive_int(snapshot, "max_turns"),
        input_schema=_optional_dict(snapshot, "input_schema"),
        output_mode=_required_string(snapshot, "output_mode"),
        output_schema=_optional_dict(snapshot, "output_schema"),
        task_goal=task_goal.strip() if task_goal else None,
        trigger_snapshot=dict(run.trigger_snapshot),
        input_snapshot=dict(run.input_snapshot),
    )


def _event_deal_id(event: AutomationEventEnvelope) -> UUID | None:
    value = event.subject.id if event.subject.type == "deal" else event.record.get("deal_id")
    if value is None:
        return None
    try:
        return UUID(str(value))
    except ValueError as exc:
        raise ValueError("automation event deal identity must be a UUID") from exc


def _required_uuid(value: dict[str, Any], key: str) -> UUID:
    try:
        return UUID(str(value[key]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a UUID") from exc


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


def _value(value: object) -> str:
    return str(value.value) if isinstance(value, Enum) else str(value)
