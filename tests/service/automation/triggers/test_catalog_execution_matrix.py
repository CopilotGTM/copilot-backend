"""Execution proof for every trigger definition returned by the live catalog."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables import AgentRun, AutomationTrigger
from src.service.automation.events.contracts import AutomationEventEnvelope
from src.service.automation.triggers.activation import AutomationTriggerActivationService
from src.service.automation.triggers.registry import (
    AUTOMATION_REGISTRY,
    EventDefinition,
    FieldType,
    TriggerDefinition,
)

HASH = "a" * 64


def _snapshot(agent_id: UUID) -> dict[str, Any]:
    return {
        "agent_id": str(agent_id),
        "revision": 1,
        "agent_type": "general",
        "prompt": "Handle this event.",
        "model_name": None,
        "model_config": {},
        "allowed_tools": [],
        "allowed_skills": [],
        "max_turns": None,
        "input_schema": None,
        "output_mode": "text",
        "output_schema": None,
    }


def _minimum_config(definition: TriggerDefinition) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for field in definition.config_fields:
        if field.default is not None:
            values[field.key] = field.default
        elif field.key == "starts_at":
            values[field.key] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        elif field.key == "payload_schema":
            values[field.key] = {"type": "object", "additionalProperties": False}
        elif field.key == "stage_id":
            values[field.key] = str(uuid4())
        elif field.key == "direction":
            values[field.key] = "up"
        elif field.key == "threshold":
            values[field.key] = "100.00"
        elif field.required:
            raise AssertionError(
                f"No minimum config fixture for {definition.trigger_key}.{field.key}"
            )
    if definition.source_selector is not None:
        values["selection"] = {"mode": "all", "ids": []}
    return definition.compile(values).config


def _enum_value(key: str, event_key: str) -> str:
    if key == "deal_status":
        return "open"
    if key == "direction":
        return "sent" if event_key.endswith(".sent") else "received"
    if key == "status":
        if event_key == "agent.run.completed":
            return "succeeded"
        if event_key == "agent.run.failed":
            return "failed"
        return "RESOLVED" if event_key.endswith(".resolved") else "OPEN"
    if key == "severity":
        return "HIGH"
    if key == "signal_type":
        return "blocker" if "blocker" in event_key else "risk"
    if key == "event_key":
        return "crm.deal.updated"
    if key == "currency":
        return "USD"
    if key == "provider":
        return "google_meet"
    if key == "language":
        return "en"
    return "active"


def _field_value(
    value_type: FieldType,
    key: str,
    *,
    event_key: str,
    subject_id: UUID,
    principal_id: UUID,
) -> Any:
    if key == "id":
        return str(subject_id)
    if value_type in {FieldType.UUID, FieldType.RELATION}:
        return str(principal_id if key in {"owner_id", "created_by_user_id"} else uuid4())
    if value_type == FieldType.BOOLEAN:
        return True
    if value_type == FieldType.DECIMAL:
        return "100.00"
    if value_type == FieldType.DATETIME:
        return datetime(2026, 8, 7, 10, tzinfo=UTC).isoformat()
    if value_type == FieldType.EMAIL:
        return "buyer@example.com"
    if value_type == FieldType.ENUM:
        return _enum_value(key, event_key)
    if value_type == FieldType.LIST:
        return ["buyer@example.com"] if "email" in key else [str(principal_id)]
    if value_type == FieldType.OBJECT:
        return {}
    return {
        "domain": "example.com",
        "phone": "+15551234567",
        "sender_domain": "example.com",
    }.get(key, f"{key} value")


def _changed_value(value: Any) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, str):
        try:
            UUID(value)
        except ValueError:
            try:
                return str(Decimal(value) + 1)
            except Exception:
                if "T" in value:
                    return datetime(2026, 8, 8, 10, tzinfo=UTC).isoformat()
                return f"previous {value}"
        return str(uuid4())
    return value


def _filter_leaves(tree: Any) -> list[dict[str, Any]]:
    if not isinstance(tree, dict):
        return []
    for mode in ("all", "any"):
        if isinstance(tree.get(mode), list):
            return [leaf for child in tree[mode] for leaf in _filter_leaves(child)]
    return [tree]


def _matching_value(operator: str, expected: Any) -> Any:
    if operator in {"eq", "gte", "lte"}:
        return expected
    number = Decimal(str(expected))
    return str(number - 1 if operator == "lt" else number + 1)


def _envelope(
    definition: TriggerDefinition,
    event: EventDefinition,
    config: dict[str, Any],
    organisation_id: UUID,
    principal_id: UUID,
    trigger_id: UUID,
) -> AutomationEventEnvelope:
    event_id, subject_id = uuid4(), uuid4()
    record = {
        field.key: _field_value(
            field.value_type,
            field.key,
            event_key=event.event_key,
            subject_id=subject_id,
            principal_id=principal_id,
        )
        for field in event.record_fields
    }
    compiled = definition.compile(config)
    changes: dict[str, dict[str, Any]] = {}
    if event.operation.value == "update":
        watch_path = (
            compiled.watch.paths[0]
            if compiled.watch is not None
            else next(iter(event.watch_fields))
        )
        after = record[watch_path]
        changes[watch_path] = {"before": _changed_value(after), "after": after}
    for leaf in _filter_leaves(compiled.filter):
        path = leaf["path"].split(".")
        actual = _matching_value(leaf["operator"], leaf["value"])
        if path[0] == "record":
            record[path[1]] = actual
        elif path[0] == "changes":
            change = changes.setdefault(
                path[1],
                {"before": _changed_value(record[path[1]]), "after": record[path[1]]},
            )
            change[path[2]] = actual
            record[path[1]] = change["after"]
    route = (
        {"mode": "direct", "trigger_id": str(trigger_id), "config_hash": HASH}
        if event.route_mode.value == "direct"
        else {"mode": "match"}
    )
    attributes = {
        field.key: _field_value(
            field.value_type,
            field.key,
            event_key=event.event_key,
            subject_id=subject_id,
            principal_id=principal_id,
        )
        for field in event.attribute_fields
    }
    references = {
        field.key: _field_value(
            field.value_type,
            field.key,
            event_key=event.event_key,
            subject_id=subject_id,
            principal_id=principal_id,
        )
        for field in event.reference_fields
    }
    return AutomationEventEnvelope.model_validate(
        {
            "event_id": event_id,
            "organisation_id": organisation_id,
            "event_key": event.event_key,
            "route": route,
            "occurred_at": datetime(2026, 8, 7, tzinfo=UTC),
            "source": {
                "domain": event.event_key.split(".", 1)[0],
                "system": "catalog_matrix_fixture",
                "operation": event.operation.value,
                "mode": "interactive",
                "source_event_id": str(event_id),
            },
            "actor": {"type": "user", "id": str(principal_id)},
            "subject": {
                "type": event.subject_type if event.subject_required else None,
                "id": str(subject_id) if event.subject_required else None,
            },
            "record": record,
            "changes": changes,
            "attributes": attributes,
            "references": references,
            "idempotency_key": f"catalog-matrix:{definition.trigger_key}:{event_id}",
        }
    )


class MatrixAccess:
    def __init__(self, binding: AutomationTrigger) -> None:
        self.binding = binding
        self.runs: list[AgentRun] = []

    async def list_event_candidates(self, event_key: str) -> list[AutomationTrigger]:
        return [self.binding] if event_key in self.binding.match_event_keys else []

    async def get_direct_candidate(self, trigger_id: UUID) -> AutomationTrigger | None:
        return self.binding if trigger_id == self.binding.trigger_id else None

    async def list_active_agent_delivery_configs(
        self,
        agent_ids: set[UUID],
    ) -> dict[UUID, dict[str, object]]:
        return {agent_id: {"channels": []} for agent_id in agent_ids}

    async def list_active_user_ids(self, user_ids: set[UUID]) -> set[UUID]:
        return user_ids

    async def list_crm_user_ids_by_principal(
        self, principal_ids: set[UUID]
    ) -> dict[UUID, set[UUID]]:
        return {principal_id: {principal_id} for principal_id in principal_ids}

    async def validate_option_selection(self, _source: str, _values: set[str]) -> None:
        return None

    async def list_subject_participant_user_ids(
        self, _subject_type: str, _subject_id: UUID
    ) -> set[UUID]:
        return set()

    async def pause_invalid_trigger(self, *args: Any, **kwargs: Any) -> bool:
        return True

    async def list_causation_trigger_ids(self, _run_id: UUID | None) -> set[UUID]:
        return set()

    async def insert_run_if_absent(self, run: AgentRun) -> bool:
        self.runs.append(run)
        return True

    async def get_run(self, *, trigger_id: UUID, event_id: UUID) -> AgentRun | None:
        return None


CATALOG_DEFINITIONS = tuple(AUTOMATION_REGISTRY.catalog_triggers())


def test_catalog_matrix_contains_exactly_the_producer_proven_definitions() -> None:
    assert len(CATALOG_DEFINITIONS) == 47
    assert len({definition.trigger_key for definition in CATALOG_DEFINITIONS}) == 47
    assert all(definition.producer_available for definition in CATALOG_DEFINITIONS)
    assert "crm.account.tier_changed" not in {
        definition.trigger_key for definition in CATALOG_DEFINITIONS
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "definition",
    CATALOG_DEFINITIONS,
    ids=lambda definition: definition.trigger_key,
)
async def test_every_catalog_definition_can_create_a_claimable_run(
    definition: TriggerDefinition,
) -> None:
    organisation_id, principal_id = uuid4(), uuid4()
    config = _minimum_config(definition)
    compiled = definition.compile(config)
    event = AUTOMATION_REGISTRY.event(definition.match_event_keys[0])
    organisation_scope = definition.allowed_principal_modes == ("organisation",)
    agent_id = uuid4()
    binding = AutomationTrigger(
        organisation_id=organisation_id,
        created_by_user_id=principal_id,
        name="Catalog matrix",
        target_id=agent_id,
        target_revision=1,
        target_snapshot=_snapshot(agent_id),
        trigger_key=definition.trigger_key,
        family=definition.family.value,
        match_event_keys=list(definition.match_event_keys),
        trigger_config=config,
        watch_config=(
            {"mode": compiled.watch.mode, "paths": list(compiled.watch.paths)}
            if compiled.watch
            else None
        ),
        filter_config=compiled.filter,
        execution_principal_type=("organisation_service" if organisation_scope else "user"),
        execution_principal_id=(organisation_id if organisation_scope else principal_id),
        scope_type=("organisation" if organisation_scope else "owned_or_created_by_principal"),
        audience_mode=("none" if organisation_scope else "principal"),
        config_hash=HASH,
    )
    envelope = _envelope(
        definition,
        event,
        config,
        organisation_id,
        principal_id,
        binding.trigger_id,
    )
    event.validate_envelope(envelope)
    access = MatrixAccess(binding)

    result = await AutomationTriggerActivationService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        access=access,
        registry=AUTOMATION_REGISTRY,
    ).activate(envelope)

    assert result.matched_count == 1, result.outcomes
    assert len(result.created_run_ids) == 1
    assert len(access.runs) == 1
    assert access.runs[0].status.value == "pending"
