"""Regression tests for test activation; these cases lock the backend contract used by the changed feature."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables import (
    AgentRun,
    AutomationTrigger,
    CRMCustomFieldDataType,
    CRMCustomFieldDefinition,
)
from src.service.automation.events.contracts import AutomationEventEnvelope
from src.service.automation.triggers.activation import AutomationTriggerActivationService
from src.service.automation.triggers.crm_schema import merge_deal_custom_fields
from src.service.automation.triggers.registry import AUTOMATION_REGISTRY

HASH = "a" * 64


def agent_snapshot(agent_id: UUID, revision: int = 1) -> dict:
    """Build the agent snapshot fixture used by the surrounding regression cases."""
    return {
        "agent_id": str(agent_id),
        "revision": revision,
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


def trigger(
    organisation_id: UUID,
    principal_id: UUID,
    *,
    trigger_key: str = "crm.contact.updated",
    family: str = "event",
    match_event_keys: list[str] | None = None,
    principal_type: str = "user",
    scope_type: str = "owned_or_created_by_principal",
    audience_mode: str = "principal",
    audience_config: dict | None = None,
    watch_config: dict | None = None,
    filter_config: dict | None = None,
    trigger_config: dict | None = None,
    config_hash: str = HASH,
) -> AutomationTrigger:
    """Build the trigger fixture used by the surrounding regression cases."""
    agent_id = uuid4()
    saved_config = (
        trigger_config
        if trigger_config is not None
        else AUTOMATION_REGISTRY.trigger(trigger_key).compile({}).config
    )
    return AutomationTrigger(
        organisation_id=organisation_id,
        created_by_user_id=principal_id,
        name="Automation",
        target_id=agent_id,
        target_revision=1,
        target_snapshot=agent_snapshot(agent_id),
        trigger_key=trigger_key,
        family=family,
        match_event_keys=match_event_keys or [trigger_key],
        trigger_config=saved_config,
        watch_config=watch_config,
        filter_config=filter_config,
        execution_principal_type=principal_type,
        execution_principal_id=(
            organisation_id if principal_type == "organisation_service" else principal_id
        ),
        scope_type=scope_type,
        audience_mode=audience_mode,
        audience_config=audience_config or {},
        config_hash=config_hash,
    )


def contact_event(
    organisation_id: UUID,
    owner_id: UUID,
    *,
    event_key: str = "crm.contact.updated",
    source_mode: str = "interactive",
    caused_by_run_id: UUID | None = None,
    causation_depth: int | None = None,
) -> AutomationEventEnvelope:
    """Build the contact event fixture used by the surrounding regression cases."""
    event_id = uuid4()
    subject_id = uuid4()
    operation = "create" if event_key.endswith(".created") else "update"
    return AutomationEventEnvelope.model_validate(
        {
            "event_id": str(event_id),
            "organisation_id": str(organisation_id),
            "event_key": event_key,
            "route": {"mode": "match"},
            "occurred_at": datetime(2026, 8, 5, tzinfo=UTC),
            "source": {
                "domain": "crm",
                "system": "internal",
                "operation": operation,
                "mode": source_mode,
                "source_event_id": str(event_id),
            },
            "actor": {"type": "user", "id": str(owner_id)},
            "subject": {"type": "contact", "id": str(subject_id)},
            "record": {
                "id": str(subject_id),
                "owner_id": str(owner_id),
                "created_by_user_id": str(owner_id),
                "email": "buyer@example.com",
            },
            "changes": (
                {"email": {"before": None, "after": "buyer@example.com"}}
                if operation == "update"
                else {}
            ),
            "correlation_id": "request-123",
            "caused_by_run_id": str(caused_by_run_id) if caused_by_run_id else None,
            "causation_depth": (
                causation_depth if causation_depth is not None else 1 if caused_by_run_id else 0
            ),
            "idempotency_key": f"{event_key}:{event_id}",
        }
    )


def manual_event(binding: AutomationTrigger) -> AutomationEventEnvelope:
    """Build the manual event fixture used by the surrounding regression cases."""
    event_id = uuid4()
    return AutomationEventEnvelope.model_validate(
        {
            "event_id": str(event_id),
            "organisation_id": str(binding.organisation_id),
            "event_key": "system.manual.invoked",
            "route": {
                "mode": "direct",
                "trigger_id": str(binding.trigger_id),
                "config_hash": binding.config_hash,
            },
            "occurred_at": datetime(2026, 8, 5, tzinfo=UTC),
            "source": {
                "domain": "system",
                "system": "agent_studio",
                "operation": "invoke",
                "mode": "interactive",
                "source_event_id": str(event_id),
            },
            "actor": {"type": "user", "id": str(binding.execution_principal_id)},
            "subject": {"type": None, "id": None},
            "idempotency_key": f"manual:{binding.trigger_id}:{event_id}",
        }
    )


def deal_custom_event(
    organisation_id: UUID,
    owner_id: UUID,
    *,
    field_key: str,
    before: object,
    after: object,
) -> AutomationEventEnvelope:
    """Build a queued deal update containing one historical custom-field fact."""
    event_id, deal_id = uuid4(), uuid4()
    return AutomationEventEnvelope.model_validate(
        {
            "event_id": event_id,
            "organisation_id": organisation_id,
            "event_key": "crm.deal.updated",
            "route": {"mode": "match"},
            "occurred_at": datetime(2026, 8, 5, tzinfo=UTC),
            "source": {
                "domain": "crm",
                "system": "internal",
                "operation": "update",
                "mode": "interactive",
                "source_event_id": str(event_id),
            },
            "actor": {"type": "user", "id": str(owner_id)},
            "subject": {"type": "deal", "id": str(deal_id)},
            "record": {
                "id": str(deal_id),
                "deal_status": "open",
                "owner_id": str(owner_id),
                "created_by_user_id": str(owner_id),
                "custom_properties": {field_key: after},
            },
            "changes": {f"custom_properties.{field_key}": {"before": before, "after": after}},
            "idempotency_key": f"crm.deal.updated:{event_id}",
        }
    )


class FakeAccess:
    """Represent the Fake Access test double used by the surrounding regression cases."""

    def __init__(self, candidates: list[AutomationTrigger]) -> None:
        self.candidates = candidates
        self.active_agent_ids = {item.target_id for item in candidates}
        self.active_user_ids = {
            item.execution_principal_id
            for item in candidates
            if str(item.execution_principal_type) in {"user", "AutomationPrincipalType.USER"}
        }
        self.causation_trigger_ids: set[UUID] = set()
        self.subject_participant_ids: set[UUID] = set()
        self.crm_user_ids_by_principal: dict[UUID, set[UUID]] = {}
        self.invalid_options: set[tuple[str, frozenset[str]]] = set()
        self.validated_options: list[tuple[str, set[str]]] = []
        self.paused: list[tuple[UUID, str, str]] = []
        self.runs: list[AgentRun] = []
        self.existing: AgentRun | None = None
        self.inserted = True

    async def list_event_candidates(self, event_key: str) -> list[AutomationTrigger]:
        """Expose the list event candidates operation expected by the Fake Access test double."""
        return [item for item in self.candidates if event_key in item.match_event_keys]

    async def get_direct_candidate(self, trigger_id: UUID) -> AutomationTrigger | None:
        """Expose the get direct candidate operation expected by the Fake Access test double."""
        return next((item for item in self.candidates if item.trigger_id == trigger_id), None)

    async def list_active_agent_ids(self, agent_ids: set[UUID]) -> set[UUID]:
        """Expose the list active agent ids operation expected by the Fake Access test double."""
        return agent_ids & self.active_agent_ids

    async def list_active_user_ids(self, user_ids: set[UUID]) -> set[UUID]:
        """Expose the list active user ids operation expected by the Fake Access test double."""
        return user_ids & self.active_user_ids

    async def list_crm_user_ids_by_principal(
        self,
        principal_ids: set[UUID],
    ) -> dict[UUID, set[UUID]]:
        return {
            principal_id: set(self.crm_user_ids_by_principal.get(principal_id, set()))
            for principal_id in principal_ids
        }

    async def validate_option_selection(self, source: str, values: set[str]) -> None:
        """Expose the validate option selection operation expected by the Fake Access test double."""
        self.validated_options.append((source, values))
        if (source, frozenset(values)) in self.invalid_options:
            raise ValueError("saved option no longer exists")

    async def list_subject_participant_user_ids(
        self,
        _subject_type: str,
        _subject_id: UUID,
    ) -> set[UUID]:
        """Expose the list subject participant user ids operation expected by the Fake Access test double."""
        return set(self.subject_participant_ids)

    async def pause_invalid_trigger(
        self,
        trigger_id: UUID,
        *,
        error_code: str,
        message: str,
    ) -> bool:
        """Expose the pause invalid trigger operation expected by the Fake Access test double."""
        self.paused.append((trigger_id, error_code, message))
        return True

    async def list_causation_trigger_ids(self, caused_by_run_id: UUID | None) -> set[UUID]:
        """Expose the list causation trigger ids operation expected by the Fake Access test double."""
        return set(self.causation_trigger_ids) if caused_by_run_id else set()

    async def insert_run_if_absent(self, run: AgentRun) -> bool:
        """Expose the insert run if absent operation expected by the Fake Access test double."""
        self.runs.append(run)
        return self.inserted

    async def get_run(self, *, trigger_id: UUID, event_id: UUID) -> AgentRun | None:
        """Expose the get run operation expected by the Fake Access test double."""
        if (
            self.existing is not None
            and self.existing.trigger_id == trigger_id
            and self.existing.event_id == event_id
        ):
            return self.existing
        return None


@pytest.mark.asyncio
async def test_match_route_applies_scope_watch_filter_and_builds_pinned_run() -> None:
    """Verify the contract that match route applies scope watch filter and builds pinned run."""
    organisation_id, principal_id = uuid4(), uuid4()
    binding = trigger(
        organisation_id,
        principal_id,
        watch_config={"mode": "any", "paths": ["email"]},
        filter_config={
            "all": [
                {
                    "path": "changes.email.after",
                    "operator": "eq",
                    "value": "buyer@example.com",
                }
            ]
        },
    )
    event = contact_event(organisation_id, principal_id)
    access = FakeAccess([binding])
    service = AutomationTriggerActivationService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        access=access,
        registry=AUTOMATION_REGISTRY,
    )

    result = await service.activate(event)

    assert result.candidate_count == 1
    assert result.matched_count == 1
    assert result.correlation_id == "request-123"
    assert result.latency_ms >= 0
    assert len(result.created_run_ids) == 1
    assert [(item.status, item.reason) for item in result.outcomes] == [("created", None)]
    run = access.runs[0]
    assert run.execution_principal_id == principal_id
    assert run.event_id == event.event_id
    assert run.event_key == event.event_key
    assert run.correlation_id == "request-123"
    assert run.agent_snapshot == binding.target_snapshot
    assert run.trigger_snapshot["name"] == binding.name
    assert run.input_snapshot == event.model_dump(mode="json")
    assert run.idempotency_key == f"trigger:{binding.trigger_id}:event:{event.event_id}"
    assert (
        result.outcomes[0].as_dict(
            correlation_id=result.correlation_id,
            latency_ms=result.latency_ms,
        )["matched"]
        is True
    )


@pytest.mark.asyncio
async def test_selected_record_matches_the_event_snapshot_without_live_row_revalidation() -> None:
    organisation_id, principal_id = uuid4(), uuid4()
    event = deal_custom_event(
        organisation_id,
        principal_id,
        field_key="renewal_type",
        before="new",
        after="renewal",
    )
    config = {"selection": {"mode": "selected", "ids": [event.subject.id]}}
    compiled = AUTOMATION_REGISTRY.trigger("crm.deal.updated").compile(config)
    binding = trigger(
        organisation_id,
        principal_id,
        trigger_key="crm.deal.updated",
        trigger_config=config,
        filter_config=compiled.filter,
    )
    access = FakeAccess([binding])

    result = await AutomationTriggerActivationService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        access=access,
        registry=AUTOMATION_REGISTRY,
    ).activate(event)

    assert result.matched_count == 1
    assert access.validated_options == []


@pytest.mark.asyncio
async def test_malformed_saved_selection_pauses_instead_of_running() -> None:
    organisation_id, principal_id = uuid4(), uuid4()
    event = deal_custom_event(
        organisation_id,
        principal_id,
        field_key="renewal_type",
        before="new",
        after="renewal",
    )
    binding = trigger(
        organisation_id,
        principal_id,
        trigger_key="crm.deal.updated",
        trigger_config={"selection": {"mode": "all", "ids": [event.subject.id]}},
    )
    access = FakeAccess([binding])

    result = await AutomationTriggerActivationService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        access=access,
        registry=AUTOMATION_REGISTRY,
    ).activate(event)

    assert result.matched_count == 0
    assert [(item.status, item.reason) for item in result.outcomes] == [
        ("paused", "binding_contract_invalid")
    ]


@pytest.mark.asyncio
async def test_personal_scope_fails_closed_while_organisation_scope_runs_once() -> None:
    """Verify the contract that personal scope fails closed while organisation scope runs once."""
    organisation_id = uuid4()
    event_owner, other_user = uuid4(), uuid4()
    personal = trigger(organisation_id, other_user)
    shared = trigger(
        organisation_id,
        uuid4(),
        principal_type="organisation_service",
        scope_type="organisation",
        audience_mode="none",
    )
    access = FakeAccess([personal, shared])

    result = await AutomationTriggerActivationService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        access=access,
        registry=AUTOMATION_REGISTRY,
    ).activate(contact_event(organisation_id, event_owner))

    assert result.candidate_count == 2
    assert result.matched_count == 1
    assert [item.reason for item in result.outcomes] == ["scope_mismatch", None]
    assert access.runs[0].trigger_id == shared.trigger_id


@pytest.mark.asyncio
async def test_personal_scope_maps_workspace_principal_to_provider_crm_owner() -> None:
    organisation_id, principal_id, crm_owner_id = uuid4(), uuid4(), uuid4()
    binding = trigger(organisation_id, principal_id)
    access = FakeAccess([binding])
    access.crm_user_ids_by_principal[principal_id] = {crm_owner_id}

    result = await AutomationTriggerActivationService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        access=access,
        registry=AUTOMATION_REGISTRY,
    ).activate(contact_event(organisation_id, crm_owner_id))

    assert result.matched_count == 1
    assert [(item.status, item.reason) for item in result.outcomes] == [("created", None)]


@pytest.mark.asyncio
async def test_direct_route_requires_the_exact_active_config_hash() -> None:
    """Verify the contract that direct route requires the exact active config hash."""
    organisation_id, principal_id = uuid4(), uuid4()
    binding = trigger(
        organisation_id,
        principal_id,
        trigger_key="system.manual.invoked",
        family="manual",
    )
    access = FakeAccess([binding])
    service = AutomationTriggerActivationService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        access=access,
        registry=AUTOMATION_REGISTRY,
    )

    success = await service.activate(manual_event(binding))
    stale = await service.activate(
        manual_event(binding).model_copy(
            update={
                "route": manual_event(binding).route.model_copy(
                    update={"trigger_id": binding.trigger_id, "config_hash": "b" * 64}
                )
            }
        )
    )

    assert success.matched_count == 1
    assert stale.matched_count == 0
    assert stale.outcomes[0].reason == "stale_config"


@pytest.mark.asyncio
async def test_direct_route_rejects_an_event_not_declared_by_the_selected_trigger() -> None:
    """Bind a direct envelope to both the selected trigger and its declared event key."""
    organisation_id, principal_id = uuid4(), uuid4()
    binding = trigger(
        organisation_id,
        principal_id,
        trigger_key="system.manual.invoked",
        family="manual",
    )
    event_data = manual_event(binding).model_dump(mode="json")
    event_data.update(
        {
            "event_key": "time.schedule.due",
            "source": {
                "domain": "time",
                "system": "automation_scheduler",
                "operation": "schedule",
                "mode": "system",
                "source_event_id": event_data["source"]["source_event_id"],
            },
            "actor": {"type": "system", "id": None},
            "attributes": {
                "scheduled_for": "2026-08-05T00:00:00+00:00",
                "timezone": "UTC",
            },
        }
    )
    access = FakeAccess([binding])

    result = await AutomationTriggerActivationService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        access=access,
        registry=AUTOMATION_REGISTRY,
    ).activate(AutomationEventEnvelope.model_validate(event_data))

    assert result.matched_count == 0
    assert result.outcomes[0].reason == "direct_event_mismatch"
    assert not access.runs


@pytest.mark.asyncio
async def test_imports_inactive_authority_and_repeat_causation_skip_independently() -> None:
    """Verify the contract that imports inactive authority and repeat causation skip independently."""
    organisation_id, principal_id = uuid4(), uuid4()
    import_binding = trigger(organisation_id, principal_id)
    import_access = FakeAccess([import_binding])
    import_result = await AutomationTriggerActivationService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        access=import_access,
        registry=AUTOMATION_REGISTRY,
    ).activate(contact_event(organisation_id, principal_id, source_mode="initial_sync"))
    assert import_result.outcomes[0].reason == "source_mode_denied"

    inactive_access = FakeAccess([import_binding])
    inactive_access.active_user_ids.clear()
    inactive_result = await AutomationTriggerActivationService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        access=inactive_access,
        registry=AUTOMATION_REGISTRY,
    ).activate(contact_event(organisation_id, principal_id))
    assert inactive_result.outcomes[0].reason == "principal_inactive"

    caused_by = uuid4()
    repeated_access = FakeAccess([import_binding])
    repeated_access.causation_trigger_ids.add(import_binding.trigger_id)
    repeated_result = await AutomationTriggerActivationService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        access=repeated_access,
        registry=AUTOMATION_REGISTRY,
    ).activate(contact_event(organisation_id, principal_id, caused_by_run_id=caused_by))
    assert repeated_result.outcomes[0].reason == "causation_repeat"

    depth_access = FakeAccess([import_binding])
    depth_result = await AutomationTriggerActivationService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        access=depth_access,
        registry=AUTOMATION_REGISTRY,
    ).activate(
        contact_event(
            organisation_id,
            principal_id,
            caused_by_run_id=caused_by,
            causation_depth=3,
        )
    )
    assert depth_result.outcomes[0].reason == "causation_depth_exceeded"


@pytest.mark.asyncio
async def test_invalid_saved_matcher_pauses_only_that_binding_and_replay_resolves_run() -> None:
    """Verify the contract that invalid saved matcher pauses only that binding and replay resolves run."""
    organisation_id, principal_id = uuid4(), uuid4()
    invalid = trigger(
        organisation_id,
        principal_id,
        filter_config={"path": "record.unknown", "operator": "eq", "value": "x"},
    )
    valid = trigger(organisation_id, principal_id)
    event = contact_event(organisation_id, principal_id)
    access = FakeAccess([invalid, valid])
    service = AutomationTriggerActivationService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        access=access,
        registry=AUTOMATION_REGISTRY,
    )

    first = await service.activate(event)

    assert first.matched_count == 1
    assert [item.status for item in first.outcomes] == ["paused", "created"]
    assert [item.reason for item in first.outcomes] == ["binding_contract_invalid", None]
    assert access.paused[0][:2] == (invalid.trigger_id, "binding_contract_invalid")

    existing = access.runs[-1]
    access.inserted = False
    access.existing = existing
    replay = await service.activate(event)
    assert replay.created_run_ids == ()
    assert replay.replayed_run_ids == (existing.run_id,)
    assert replay.outcomes[-1].status == "replayed"


@pytest.mark.asyncio
async def test_deleted_custom_field_does_not_poison_unrelated_queued_candidates() -> None:
    """Validate historical facts independently from the current candidate schema."""
    organisation_id, principal_id = uuid4(), uuid4()
    stale = trigger(
        organisation_id,
        principal_id,
        trigger_key="crm.deal.updated",
        watch_config={"mode": "any", "paths": ["custom_properties.score"]},
    )
    valid = trigger(
        organisation_id,
        principal_id,
        trigger_key="crm.deal.updated",
    )
    access = FakeAccess([stale, valid])

    result = await AutomationTriggerActivationService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        access=access,
        registry=merge_deal_custom_fields([]).registry,
    ).activate(
        deal_custom_event(
            organisation_id,
            principal_id,
            field_key="score",
            before="8.0",
            after="9.0",
        )
    )

    assert [(item.status, item.reason) for item in result.outcomes] == [
        ("paused", "binding_contract_invalid"),
        ("created", None),
    ]
    assert [run.trigger_id for run in access.runs] == [valid.trigger_id]


@pytest.mark.asyncio
async def test_changed_custom_field_type_isolated_to_the_affected_candidate() -> None:
    """Treat a historical value mismatch as one invalid binding, not a dispatch failure."""
    organisation_id, principal_id = uuid4(), uuid4()
    current_field = CRMCustomFieldDefinition(
        organisation_id=organisation_id,
        key="score",
        label="Score",
        data_type=CRMCustomFieldDataType.BOOLEAN,
        options={},
    )
    affected = trigger(
        organisation_id,
        principal_id,
        trigger_key="crm.deal.updated",
        filter_config={
            "path": "record.custom_properties.score",
            "operator": "eq",
            "value": True,
        },
    )
    valid = trigger(
        organisation_id,
        principal_id,
        trigger_key="crm.deal.updated",
    )
    access = FakeAccess([affected, valid])

    result = await AutomationTriggerActivationService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        access=access,
        registry=merge_deal_custom_fields([current_field]).registry,
    ).activate(
        deal_custom_event(
            organisation_id,
            principal_id,
            field_key="score",
            before="8.0",
            after="9.0",
        )
    )

    assert [(item.status, item.reason) for item in result.outcomes] == [
        ("paused", "binding_contract_invalid"),
        ("created", None),
    ]
    assert [run.trigger_id for run in access.runs] == [valid.trigger_id]


@pytest.mark.asyncio
async def test_activation_resolves_and_persists_subject_participant_audience() -> None:
    """Verify the contract that activation resolves and persists subject participant audience."""
    organisation_id = uuid4()
    audience_user_id = uuid4()
    binding = trigger(
        organisation_id,
        uuid4(),
        trigger_key="email.message.received",
        match_event_keys=["email.message.received"],
        principal_type="organisation_service",
        scope_type="organisation",
        audience_mode="subject_participants",
    )
    access = FakeAccess([binding])
    access.active_user_ids.add(audience_user_id)

    event_data = contact_event(organisation_id, uuid4()).model_dump(mode="json")
    event_data.update(
        {
            "event_key": "email.message.received",
            "source": {
                "domain": "email",
                "system": "gmail",
                "operation": "signal",
                "mode": "incremental_sync",
                "source_event_id": event_data["subject"]["id"],
            },
            "subject": {"type": "email", "id": event_data["subject"]["id"]},
            "record": {
                "id": event_data["subject"]["id"],
                "direction": "inbound",
                "sender_email": "buyer@example.com",
                "sender_domain": "example.com",
                "recipient_emails": ["owner@example.com"],
                "has_attachments": False,
                "thread_id": None,
                "owner_id": str(uuid4()),
                "deal_id": None,
            },
            "changes": {},
            "attributes": {"direction": "inbound"},
            "references": {"email_id": event_data["subject"]["id"]},
            "audience_user_ids": [str(audience_user_id)],
        }
    )
    event = AutomationEventEnvelope.model_validate(event_data)

    result = await AutomationTriggerActivationService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        access=access,
        registry=AUTOMATION_REGISTRY,
    ).activate(event)

    assert result.matched_count == 1
    assert access.runs[0].audience_config == {"user_ids": [str(audience_user_id)]}


@pytest.mark.asyncio
async def test_inactive_explicit_audience_skips_without_creating_a_run() -> None:
    """Verify the contract that inactive explicit audience skips without creating a run."""
    organisation_id = uuid4()
    audience_user_id = uuid4()
    binding = trigger(
        organisation_id,
        uuid4(),
        principal_type="organisation_service",
        scope_type="organisation",
        audience_mode="explicit_users",
        audience_config={"user_ids": [str(audience_user_id)]},
    )
    access = FakeAccess([binding])

    result = await AutomationTriggerActivationService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        access=access,
        registry=AUTOMATION_REGISTRY,
    ).activate(contact_event(organisation_id, uuid4()))

    assert result.matched_count == 0
    assert result.outcomes[0].reason == "audience_inactive"
    assert not access.runs


@pytest.mark.asyncio
async def test_deleted_saved_option_pauses_binding_before_match() -> None:
    """Verify the contract that deleted saved option pauses binding before match."""
    organisation_id, principal_id = uuid4(), uuid4()
    binding = trigger(
        organisation_id,
        principal_id,
        filter_config={
            "all": [
                {
                    "path": "record.owner_id",
                    "operator": "eq",
                    "value": str(principal_id),
                }
            ]
        },
    )
    access = FakeAccess([binding])
    access.invalid_options.add(("crm_users", frozenset({str(principal_id)})))

    result = await AutomationTriggerActivationService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        access=access,
        registry=AUTOMATION_REGISTRY,
    ).activate(contact_event(organisation_id, principal_id))

    assert result.outcomes[0].status == "paused"
    assert result.outcomes[0].reason == "binding_contract_invalid"
    assert access.paused[0][0] == binding.trigger_id
    assert not access.runs
