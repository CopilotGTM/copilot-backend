"""Lifecycle tests for compiled bindings and explicit target revision pins."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from src.api.schemas.automation_trigger_models import (
    AutomationTriggerCreateRequest,
    AutomationTriggerOptionResolveRequest,
    AutomationTriggerOptionSearchRequest,
    AutomationTriggerUpdateRequest,
    AutomationWebhookRotateRequest,
)
from src.domain.models.organisation_tables import (
    AgentStatus,
    AutomationTrigger,
    AutomationTriggerStatus,
    UserAgent,
)
from src.service.automation.errors import AutomationServiceError
from src.service.automation.triggers.crm_schema import AutomationRegistrySnapshot
from src.service.automation.triggers.options import TriggerOptionRecord
from src.service.automation.triggers.registry import AUTOMATION_REGISTRY
from src.service.automation.triggers.service import AutomationTriggerService


class FakeOptions:
    """Represent the Fake Options test double used by the surrounding regression cases."""

    def __init__(self) -> None:
        self.search_calls: list[dict[str, object]] = []
        self.resolve_calls: list[tuple[str, set[str]]] = []
        self.search_records: list[TriggerOptionRecord] = []
        self.resolve_records: dict[str, TriggerOptionRecord] = {}

    async def validate_option_selection(self, source: str, values: set[str]) -> None:
        """Expose the validate option selection operation expected by the Fake Options test double."""
        return None

    async def validate_audience_user_ids(self, user_ids: set[UUID]) -> None:
        """Expose the validate audience user ids operation expected by the Fake Options test double."""
        return None

    async def list_pipelines(self) -> list[object]:
        """Expose the list pipelines operation expected by the Fake Options test double."""
        return []

    async def validate_schedule_subjects(self, *args: object, **kwargs: object) -> None:
        """Expose the validate schedule subjects operation expected by the Fake Options test double."""
        return None

    async def validate_source_selection(self, *args: object, **kwargs: object) -> None:
        return None

    async def search_options(self, source: str, **kwargs: object):
        self.search_calls.append({"source": source, **kwargs})
        return self.search_records, None

    async def resolve_options(self, source: str, values: set[str], **kwargs: object):
        self.resolve_calls.append((source, values))
        return {
            value: self.resolve_records[value] for value in values if value in self.resolve_records
        }


class FakeAccess:
    """Represent the Fake Access test double used by the surrounding regression cases."""

    def __init__(self, agent: UserAgent) -> None:
        self.agent = agent
        self.trigger: AutomationTrigger | None = None
        self.replace_values: dict[str, object] | None = None

    async def get_agent(self, agent_id: UUID) -> UserAgent | None:
        """Expose the get agent operation expected by the Fake Access test double."""
        return self.agent if agent_id == self.agent.agent_id else None

    async def find_duplicate(self, *args: object, **kwargs: object) -> None:
        """Expose the find duplicate operation expected by the Fake Access test double."""
        return None

    async def create_trigger(self, trigger: AutomationTrigger) -> None:
        """Expose the create trigger operation expected by the Fake Access test double."""
        self.trigger = trigger

    async def get_trigger(
        self, agent_id: UUID, trigger_id: UUID, *, include_archived: bool = False
    ) -> AutomationTrigger | None:
        """Expose the get trigger operation expected by the Fake Access test double."""
        if self.trigger and self.trigger.trigger_id == trigger_id:
            return self.trigger
        return None

    async def get_trigger_by_id(
        self, trigger_id: UUID, *, include_archived: bool = False
    ) -> AutomationTrigger | None:
        if self.trigger and self.trigger.trigger_id == trigger_id:
            return self.trigger
        return None

    async def list_triggers(self, *args: object, **kwargs: object):
        rows = [self.trigger] if self.trigger is not None else []
        return rows, len(rows)

    async def replace_trigger(
        self,
        agent_id: UUID,
        trigger_id: UUID,
        *,
        expected_updated_at: datetime,
        values: dict[str, object],
    ) -> AutomationTrigger | None:
        """Expose the replace trigger operation expected by the Fake Access test double."""
        assert self.trigger is not None
        self.replace_values = values
        for key, value in values.items():
            setattr(self.trigger, key, value)
        self.trigger.updated_at = expected_updated_at + timedelta(seconds=1)
        return self.trigger


def make_agent(*, revision: int = 3, status: AgentStatus = AgentStatus.ACTIVE) -> UserAgent:
    """Build the make agent fixture used by the surrounding regression cases."""
    return UserAgent(
        organisation_id=uuid4(),
        owner_user_id=uuid4(),
        revision=revision,
        status=status,
        name="Deal coach",
        prompt="Review the deal",
    )


def schedule_create(*, status: str = "active") -> AutomationTriggerCreateRequest:
    """Build the schedule create fixture used by the surrounding regression cases."""
    return AutomationTriggerCreateRequest.model_validate(
        {
            "name": "Daily review",
            "trigger_key": "time.schedule.due",
            "status": status,
            "config": {
                "timezone": "UTC",
                "starts_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                "recurrence": "daily",
            },
            "principal_mode": "personal",
            "scope": {"type": "owned_or_created_by_principal"},
            "audience": {"mode": "principal"},
        }
    )


def webhook_create() -> AutomationTriggerCreateRequest:
    """Build the webhook create fixture used by the surrounding regression cases."""
    return AutomationTriggerCreateRequest.model_validate(
        {
            "name": "External signal",
            "trigger_key": "integration.webhook.received",
            "config": {
                "payload_schema": {
                    "type": "object",
                    "properties": {"risk": {"type": "string"}},
                    "required": ["risk"],
                    "additionalProperties": False,
                }
            },
            "principal_mode": "personal",
            "scope": {"type": "owned_or_created_by_principal"},
            "audience": {"mode": "principal"},
        }
    )


def entered_stage_create(stage_id: UUID) -> AutomationTriggerCreateRequest:
    """Build one semantic Deal trigger using a server-owned fixed matcher."""

    return AutomationTriggerCreateRequest.model_validate(
        {
            "name": "Entered discovery",
            "trigger_key": "crm.deal.entered_stage",
            "config": {"stage_id": str(stage_id)},
            "principal_mode": "personal",
            "scope": {"type": "owned_or_created_by_principal"},
            "audience": {"mode": "principal"},
        }
    )


def make_service(agent: UserAgent) -> tuple[AutomationTriggerService, FakeAccess, AsyncMock]:
    """Build the make service fixture used by the surrounding regression cases."""
    session = AsyncMock()
    service = AutomationTriggerService(
        session,
        agent.organisation_id,
        agent.owner_user_id,
        is_admin=False,
    )
    access = FakeAccess(agent)
    service.access = access  # type: ignore[assignment]
    service.options = FakeOptions()  # type: ignore[assignment]
    service.validator.options = service.options
    service.registry_service.snapshot = AsyncMock(
        return_value=AutomationRegistrySnapshot(AUTOMATION_REGISTRY, {})
    )
    return service, access, session


@pytest.mark.asyncio
async def test_option_search_derives_source_and_authority_from_catalog() -> None:
    service, _, _ = make_service(make_agent())
    options = service.options
    assert isinstance(options, FakeOptions)
    mailbox_id = uuid4()
    options.search_records = [
        TriggerOptionRecord(str(mailbox_id), "sales@example.com · Google", "active", True)
    ]

    result = await service.search_trigger_options(
        AutomationTriggerOptionSearchRequest.model_validate(
            {
                "trigger_key": "email.message.received",
                "control": "source_selector",
                "principal_mode": "personal",
                "query": "sales",
            }
        )
    )

    assert result.items[0].label == "sales@example.com · Google"
    assert options.search_calls[0]["source"] == "email_mailboxes"
    assert options.search_calls[0]["authority_scoped"] is True
    assert options.search_calls[0]["principal_id"] == service.created_by_user_id


@pytest.mark.asyncio
async def test_non_admin_cannot_search_organisation_options() -> None:
    service, _, _ = make_service(make_agent())

    with pytest.raises(AutomationServiceError, match="organisation admin"):
        await service.search_trigger_options(
            AutomationTriggerOptionSearchRequest.model_validate(
                {
                    "trigger_key": "crm.deal.updated",
                    "control": "source_selector",
                    "principal_mode": "organisation",
                }
            )
        )


@pytest.mark.asyncio
async def test_filter_relation_search_remains_tenant_scoped_not_record_owned() -> None:
    service, _, _ = make_service(make_agent())
    options = service.options
    assert isinstance(options, FakeOptions)

    await service.search_trigger_options(
        AutomationTriggerOptionSearchRequest.model_validate(
            {
                "trigger_key": "crm.deal.updated",
                "control": "filter",
                "field_path": "record.owner_id",
                "principal_mode": "personal",
            }
        )
    )

    assert options.search_calls[0]["source"] == "crm_users"
    assert options.search_calls[0]["authority_scoped"] is False


@pytest.mark.asyncio
async def test_resolved_ai_status_search_uses_terminal_options() -> None:
    service, _, _ = make_service(make_agent())
    service.is_admin = True
    options = service.options
    assert isinstance(options, FakeOptions)

    await service.search_trigger_options(
        AutomationTriggerOptionSearchRequest.model_validate(
            {
                "trigger_key": "ai.deal_risk.resolved",
                "control": "filter",
                "field_path": "record.status",
                "principal_mode": "organisation",
            }
        )
    )

    assert options.search_calls[0]["source"] == "risk_resolution_statuses"


@pytest.mark.asyncio
async def test_audience_and_direct_subject_options_are_backend_derived() -> None:
    service, _, _ = make_service(make_agent())
    service.is_admin = True
    options = service.options
    assert isinstance(options, FakeOptions)

    await service.search_trigger_options(
        AutomationTriggerOptionSearchRequest.model_validate(
            {
                "trigger_key": "ai.deal_risk.detected",
                "control": "audience",
                "principal_mode": "organisation",
            }
        )
    )
    await service.search_trigger_options(
        AutomationTriggerOptionSearchRequest.model_validate(
            {
                "trigger_key": "system.manual.invoked",
                "control": "direct_subject",
                "field_path": "deal",
                "principal_mode": "personal",
            }
        )
    )

    assert options.search_calls[0]["source"] == "organisation_users"
    assert options.search_calls[0]["authority_scoped"] is False
    assert options.search_calls[1]["source"] == "deals"
    assert options.search_calls[1]["authority_scoped"] is True


@pytest.mark.asyncio
async def test_option_resolution_batches_and_masks_labels_without_saved_disclosure() -> None:
    service, _, _ = make_service(make_agent())
    options = service.options
    assert isinstance(options, FakeOptions)
    active_id, foreign_id = str(uuid4()), str(uuid4())
    options.resolve_records = {
        active_id: TriggerOptionRecord(active_id, "Northwind", "active", True)
    }
    body = AutomationTriggerOptionResolveRequest.model_validate(
        {
            "requests": [
                {
                    "trigger_key": "crm.deal.updated",
                    "control": "source_selector",
                    "principal_mode": "personal",
                    "values": [active_id],
                },
                {
                    "trigger_key": "crm.deal.updated",
                    "control": "source_selector",
                    "principal_mode": "personal",
                    "values": [foreign_id],
                },
            ]
        }
    )

    result = await service.resolve_trigger_options(body)

    assert options.resolve_calls == [("deals", {active_id, foreign_id})]
    assert result.results[0].items[0].label == "Northwind"
    unavailable = result.results[1].items[0]
    assert unavailable.status == "unavailable"
    assert unavailable.label is None


@pytest.mark.asyncio
async def test_saved_inactive_option_is_labelled_only_through_accessible_binding() -> None:
    agent = make_agent()
    service, access, _ = make_service(agent)
    options = service.options
    assert isinstance(options, FakeOptions)
    deal_id = str(uuid4())
    options.resolve_records = {
        deal_id: TriggerOptionRecord(deal_id, "Renamed deal", "deleted", False)
    }
    created = await service.create_trigger(
        agent.agent_id,
        AutomationTriggerCreateRequest.model_validate(
            {
                "name": "Specific deal",
                "trigger_key": "crm.deal.updated",
                "config": {"selection": {"mode": "selected", "ids": [deal_id]}},
                "principal_mode": "personal",
                "scope": {"type": "owned_or_created_by_principal"},
                "audience": {"mode": "principal"},
            }
        ),
    )
    assert access.trigger is not None
    body = AutomationTriggerOptionResolveRequest.model_validate(
        {
            "requests": [
                {
                    "trigger_key": "crm.deal.updated",
                    "trigger_id": created.trigger_id,
                    "control": "source_selector",
                    "principal_mode": "personal",
                    "values": [deal_id],
                }
            ]
        }
    )

    item = (await service.resolve_trigger_options(body)).results[0].items[0]

    assert item.label == "Renamed deal"
    assert item.status == "deleted"
    assert item.selectable is False


@pytest.mark.asyncio
async def test_trigger_list_batch_resolves_saved_card_labels() -> None:
    agent = make_agent()
    service, _, _ = make_service(agent)
    options = service.options
    assert isinstance(options, FakeOptions)
    deal_id = str(uuid4())
    options.resolve_records = {
        deal_id: TriggerOptionRecord(deal_id, "Acme renewal", "active", True)
    }
    await service.create_trigger(
        agent.agent_id,
        AutomationTriggerCreateRequest.model_validate(
            {
                "name": "Specific deal",
                "trigger_key": "crm.deal.updated",
                "config": {"selection": {"mode": "selected", "ids": [deal_id]}},
                "principal_mode": "personal",
                "scope": {"type": "owned_or_created_by_principal"},
                "audience": {"mode": "principal"},
            }
        ),
    )
    options.resolve_calls.clear()

    result = await service.list_triggers(
        agent.agent_id,
        status=None,
        limit=25,
        offset=0,
    )

    assert result.items[0].presentation.source_summary == "Specific deals: Acme renewal"
    assert options.resolve_calls == [("deals", {deal_id})]


@pytest.mark.asyncio
async def test_trigger_detail_needs_no_delivery_option_lookup() -> None:
    agent = make_agent()
    service, _, _ = make_service(agent)
    options = service.options
    assert isinstance(options, FakeOptions)
    created = await service.create_trigger(
        agent.agent_id,
        AutomationTriggerCreateRequest.model_validate(
            {
                "name": "Send pipeline review",
                "trigger_key": "time.schedule.due",
                "config": {
                    "timezone": "UTC",
                    "starts_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                    "recurrence": "daily",
                },
                "principal_mode": "personal",
                "scope": {"type": "owned_or_created_by_principal"},
                "audience": {"mode": "principal"},
                "delivery": {"mode": "email_notification"},
            }
        ),
    )

    detail = await service.get_trigger(agent.agent_id, created.trigger_id)

    assert detail.presentation.delivery_summary == "Email you from CopilotGTM"
    assert detail.presentation.delivery_status == "active"
    assert detail.presentation.delivery_reason is None
    assert options.resolve_calls == []


@pytest.mark.asyncio
async def test_create_pins_current_agent_and_persists_registry_projections() -> None:
    """Verify the contract that create pins current agent and persists registry projections."""
    agent = make_agent()
    service, access, session = make_service(agent)

    result = await service.create_trigger(agent.agent_id, schedule_create())

    assert result.target_revision == 3
    assert result.target_snapshot["prompt"] == "Review the deal"
    assert result.trigger_key == "time.schedule.due"
    assert result.family == "schedule"
    assert result.match_event_keys == ["time.schedule.due"]
    assert result.principal_mode == "personal"
    assert result.delivery.mode == "none"
    assert len(result.config_hash) == 64
    session.commit.assert_not_awaited()
    assert access.trigger is not None


@pytest.mark.asyncio
async def test_active_trigger_rejects_paused_agent_but_paused_draft_is_allowed() -> None:
    """Verify the contract that active trigger rejects paused agent but paused draft is allowed."""
    agent = make_agent(status=AgentStatus.PAUSED)
    service, _, _ = make_service(agent)

    with pytest.raises(AutomationServiceError, match="active agent"):
        await service.create_trigger(agent.agent_id, schedule_create())

    result = await service.create_trigger(agent.agent_id, schedule_create(status="paused"))
    assert result.status == AutomationTriggerStatus.PAUSED


@pytest.mark.asyncio
async def test_trigger_response_exposes_stable_pause_error_code() -> None:
    """Verify the contract that trigger response exposes stable pause error code."""
    agent = make_agent()
    service, access, _ = make_service(agent)
    created = await service.create_trigger(agent.agent_id, schedule_create())
    assert access.trigger is not None
    access.trigger.status = AutomationTriggerStatus.PAUSED
    access.trigger.last_error_code = "schedule_subject_unavailable"
    access.trigger.last_error = "A scheduled subject is unavailable."

    detail = await service.get_trigger(agent.agent_id, created.trigger_id)

    assert detail.last_error_code == "schedule_subject_unavailable"
    assert detail.last_error == "A scheduled subject is unavailable."


@pytest.mark.asyncio
async def test_replace_preserves_pin_and_refresh_explicitly_repins() -> None:
    """Verify the contract that replace preserves pin and refresh explicitly repins."""
    agent = make_agent(revision=3)
    service, access, _ = make_service(agent)
    created = await service.create_trigger(agent.agent_id, schedule_create())
    assert access.trigger is not None
    old_updated_at = access.trigger.updated_at
    agent.revision = 4
    update = AutomationTriggerUpdateRequest.model_validate(
        {**schedule_create().model_dump(), "name": "Updated", "expected_updated_at": old_updated_at}
    )

    replaced = await service.replace_trigger(agent.agent_id, created.trigger_id, update)
    assert replaced.target_revision == 3
    assert replaced.target_update_available

    refreshed = await service.refresh_target(
        agent.agent_id,
        created.trigger_id,
        expected_updated_at=access.trigger.updated_at,
    )
    assert refreshed.target_revision == 4
    assert not refreshed.target_update_available
    assert access.replace_values is not None
    assert access.replace_values["target_snapshot"]["revision"] == 4  # type: ignore[index]


@pytest.mark.asyncio
async def test_semantic_trigger_detail_round_trip_does_not_duplicate_fixed_matcher() -> None:
    agent = make_agent()
    service, access, _ = make_service(agent)
    stage_id = uuid4()

    created = await service.create_trigger(agent.agent_id, entered_stage_create(stage_id))
    loaded = await service.get_trigger(agent.agent_id, created.trigger_id)

    assert loaded.watch is None
    assert loaded.filter is None
    assert access.trigger is not None
    expected_fixed = AUTOMATION_REGISTRY.trigger("crm.deal.entered_stage").compile(
        {"stage_id": str(stage_id)}
    )
    update = AutomationTriggerUpdateRequest.model_validate(
        {
            "name": loaded.name,
            "trigger_key": loaded.trigger_key,
            "status": loaded.status,
            "config": loaded.config,
            "watch": loaded.watch,
            "filter": loaded.filter,
            "principal_mode": loaded.principal_mode,
            "scope": loaded.scope.model_dump(),
            "audience": loaded.audience.model_dump(),
            "task": loaded.task.model_dump(),
            "delivery": loaded.delivery.model_dump(),
            "expected_updated_at": access.trigger.updated_at,
        }
    )

    replaced = await service.replace_trigger(agent.agent_id, created.trigger_id, update)

    assert replaced.watch is None
    assert replaced.filter is None
    assert access.trigger.watch_config == {
        "mode": expected_fixed.watch.mode,
        "paths": list(expected_fixed.watch.paths),
    }
    assert access.trigger.filter_config == expected_fixed.filter


@pytest.mark.asyncio
async def test_refresh_rejects_new_retry_unsafe_capabilities() -> None:
    """Verify the contract that refresh rejects new retry unsafe capabilities."""
    agent = make_agent()
    service, access, _ = make_service(agent)
    created = await service.create_trigger(agent.agent_id, schedule_create())
    assert access.trigger is not None
    agent.allowed_tools = ["create_task"]
    agent.revision += 1

    with pytest.raises(AutomationServiceError, match="retry-safe"):
        await service.refresh_target(
            agent.agent_id,
            created.trigger_id,
            expected_updated_at=access.trigger.updated_at,
        )


def test_config_hash_is_order_stable_for_commutative_filter_groups() -> None:
    """Verify the contract that config hash is order stable for commutative filter groups."""
    first = {
        "all": [
            {"path": "record.currency", "operator": "eq", "value": "USD"},
            {"path": "record.amount", "operator": "gte", "value": "10"},
        ]
    }
    second = {"all": list(reversed(first["all"]))}

    assert AutomationTriggerService._canonical_filter(first) == (
        AutomationTriggerService._canonical_filter(second)
    )


@pytest.mark.asyncio
async def test_webhook_create_redacts_persisted_hashes_and_rotation_is_conditional() -> None:
    """Verify the contract that webhook create redacts persisted hashes and rotation is conditional."""
    agent = make_agent()
    service, access, _session = make_service(agent)
    service.webhook_root_secret_provider = AsyncMock(return_value="root-secret")
    service.webhook_clock = lambda: datetime(2026, 8, 5, 10, tzinfo=UTC)

    created = await service.create_trigger(agent.agent_id, webhook_create())

    assert created.webhook_credentials is not None
    assert created.webhook_credentials.endpoint_token
    assert created.webhook_credentials.secret.startswith("whsec_")
    assert access.trigger is not None
    assert access.trigger.trigger_config["_webhook_endpoint_token_hash"]
    assert access.trigger.trigger_config["_webhook_secret_hash"]
    assert "_webhook_endpoint_token_hash" not in created.config
    assert created.webhook_credentials.secret not in str(access.trigger.trigger_config)

    fetched = await service.get_trigger(agent.agent_id, created.trigger_id)
    assert fetched.webhook_credentials is None
    old_hash = access.trigger.config_hash
    old_updated_at = access.trigger.updated_at
    rotated = await service.rotate_webhook_secret(
        agent.agent_id,
        created.trigger_id,
        AutomationWebhookRotateRequest(expected_updated_at=old_updated_at),
    )

    assert rotated.endpoint_token is None
    assert rotated.secret != created.webhook_credentials.secret
    assert access.trigger.config_hash == old_hash
    assert access.trigger.trigger_config["_webhook_previous_secret_hash"]
