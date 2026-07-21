"""Orchestration tests for CRM event activation into pending agent runs."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables import (
    AgentRun,
    AgentRunStatus,
    AutomationTrigger,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealMutationEvent
from src.service.automation.triggers.activation import AutomationTriggerActivationService


class FakeAccess:
    def __init__(
        self,
        event: CRMDealMutationEvent,
        deal: CRMDeal,
        triggers: list[AutomationTrigger],
        *,
        insert_created: bool = True,
    ) -> None:
        self.event = event
        self.deal = deal
        self.triggers = triggers
        self.insert_created = insert_created
        self.runs: list[AgentRun] = []

    async def get_crm_event(self, event_id: UUID) -> CRMDealMutationEvent | None:
        return self.event if event_id == self.event.event_id else None

    async def get_deal(self, deal_id: UUID) -> CRMDeal | None:
        return self.deal if deal_id == self.deal.deal_id else None

    async def get_crm_entity(self, entity_type: str, entity_id: UUID) -> object | None:
        if entity_type == self.event.entity_type and entity_id == self.event.entity_id:
            return self.deal
        return None

    async def list_crm_event_candidates(
        self, *, owner_user_id: UUID, event_type: str
    ) -> list[AutomationTrigger]:
        assert owner_user_id == self.deal.owner_id
        assert event_type == self.event.semantic_event_types[0]
        return self.triggers

    async def insert_run_if_absent(self, run: AgentRun) -> bool:
        self.runs.append(run)
        return self.insert_created

    async def get_run_by_idempotency_key(self, idempotency_key: str) -> AgentRun | None:
        return self.runs[-1] if self.runs[-1].idempotency_key == idempotency_key else None


def fixtures() -> tuple[CRMDealMutationEvent, CRMDeal, AutomationTrigger]:
    organisation_id, owner_id, deal_id = uuid4(), uuid4(), uuid4()
    pipeline_id, from_stage_id, to_stage_id = uuid4(), uuid4(), uuid4()
    agent_id = uuid4()
    event = CRMDealMutationEvent(
        event_id=uuid4(),
        organisation_id=organisation_id,
        event_type="crm.deal.updated",
        semantic_event_types=["crm.deal.stage_changed"],
        entity_type="deal",
        entity_id=deal_id,
        deal_id=deal_id,
        operation="move_stage",
        occurred_at=datetime(2026, 7, 13, tzinfo=UTC),
        changed_fields={"stage_id": {"old": str(from_stage_id), "new": str(to_stage_id)}},
        source="api",
    )
    deal = CRMDeal(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        deal_id=deal_id,
        owner_id=owner_id,
        pipeline_id=pipeline_id,
        stage_id=to_stage_id,
        deal_status="open",
    )
    trigger = AutomationTrigger(
        organisation_id=organisation_id,
        owner_user_id=owner_id,
        name="Negotiation coach",
        target_id=agent_id,
        target_revision=5,
        target_snapshot={
            "agent_id": str(agent_id),
            "revision": 5,
            "agent_type": "general",
            "prompt": "Coach this deal.",
            "model_name": None,
            "model_config": {},
            "allowed_tools": [],
            "allowed_skills": [],
            "max_turns": None,
            "input_schema": None,
            "output_mode": "text",
            "output_schema": None,
        },
        trigger_type="event",
        source_domain="crm",
        event_type="crm.deal.stage_changed",
        filter_config={
            "schema_key": "crm.deal.stage_changed.v1",
            "pipeline_ids": [str(pipeline_id)],
            "stage_change_mode": "specific_stage",
            "from_stage_ids": [str(from_stage_id)],
            "to_stage_ids": [str(to_stage_id)],
            "outcome": "any",
        },
        task_config={"goal": "Recommend the next action"},
        config_hash="hash",
    )
    return event, deal, trigger


@pytest.mark.asyncio
async def test_activation_creates_complete_pending_run_without_commit_or_publish() -> None:
    event, deal, trigger = fixtures()
    session = AsyncMock()
    access = FakeAccess(event, deal, [trigger])
    service = AutomationTriggerActivationService(
        session,
        event.organisation_id,
        access=access,  # type: ignore[arg-type]
    )

    result = await service.activate_crm_event(event.event_id)

    assert result.matched_count == 1
    assert len(result.created_run_ids) == 1
    assert result.replayed_run_ids == ()
    run = access.runs[0]
    assert run.status == AgentRunStatus.PENDING
    assert run.owner_user_id == deal.owner_id
    assert run.agent_snapshot == trigger.target_snapshot
    assert run.trigger_snapshot["filter_config"] == trigger.filter_config
    assert run.input_snapshot["deal"] == {
        "deal_id": str(deal.deal_id),
        "pipeline_id": str(deal.pipeline_id),
        "stage_id": str(deal.stage_id),
        "outcome": "open",
    }
    assert run.idempotency_key == f"trigger:{trigger.trigger_id}:crm-event:{event.event_id}"
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_activation_replay_returns_existing_run_and_unsupported_event_is_noop() -> None:
    event, deal, trigger = fixtures()
    access = FakeAccess(event, deal, [trigger], insert_created=False)
    service = AutomationTriggerActivationService(
        AsyncMock(),
        event.organisation_id,
        access=access,  # type: ignore[arg-type]
    )

    replay = await service.activate_crm_event(event.event_id)
    assert replay.created_run_ids == ()
    assert replay.replayed_run_ids == (access.runs[0].run_id,)

    event.semantic_event_types = ["crm.deal.amount_changed"]
    skipped = await service.activate_crm_event(event.event_id)
    assert skipped.matched_count == 0
    assert skipped.skipped_reason == "unsupported_event"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "entity_type", "identity_field"),
    [
        ("crm.deal.created", "deal", "deal_id"),
        ("crm.contact.created", "contact", "contact_id"),
        ("crm.contact.deleted", "contact", "contact_id"),
        ("crm.account.created", "account", "account_id"),
        ("crm.account.deleted", "account", "account_id"),
    ],
)
async def test_activation_creates_run_for_supported_crm_lifecycle_event(
    event_type: str, entity_type: str, identity_field: str
) -> None:
    organisation_id, owner_id, entity_id, agent_id = uuid4(), uuid4(), uuid4(), uuid4()
    event = CRMDealMutationEvent(
        event_id=uuid4(),
        organisation_id=organisation_id,
        event_type=event_type,
        semantic_event_types=[event_type],
        entity_type=entity_type,
        entity_id=entity_id,
        deal_id=entity_id if entity_type == "deal" else None,
        operation=event_type.rsplit(".", 1)[-1],
        occurred_at=datetime(2026, 7, 21, tzinfo=UTC),
        changed_fields={"id": {"old": None, "new": str(entity_id)}},
        source="api",
    )
    entity = SimpleNamespace(owner_id=owner_id)
    if entity_type == "deal":
        entity.deal_id = entity_id
        entity.pipeline_id = None
        entity.stage_id = None
        entity.deal_status = "open"
    elif entity_type == "contact":
        entity.contact_id = entity_id
        entity.company_id = None
    else:
        entity.company_id = entity_id

    trigger = AutomationTrigger(
        organisation_id=organisation_id,
        owner_user_id=owner_id,
        name="Lifecycle agent",
        target_id=agent_id,
        target_revision=1,
        target_snapshot={"agent_id": str(agent_id), "revision": 1},
        trigger_type="event",
        source_domain="crm",
        event_type=event_type,
        filter_config={"schema_key": f"{event_type}.v1"},
        config_hash="hash",
    )
    access = FakeAccess(event, entity, [trigger])  # type: ignore[arg-type]
    service = AutomationTriggerActivationService(
        AsyncMock(),
        organisation_id,
        access=access,  # type: ignore[arg-type]
    )

    result = await service.activate_crm_event(event.event_id)

    assert result.matched_count == 1
    run = access.runs[0]
    assert run.event_type == event_type
    assert run.input_snapshot["schema_key"] == f"{event_type}.v1"
    assert run.input_snapshot[entity_type][identity_field] == str(entity_id)
