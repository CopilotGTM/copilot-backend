"""Lifecycle tests for target pinning and trigger ownership orchestration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from src.api.schemas.automation_trigger_models import (
    AutomationTriggerCreateRequest,
    AutomationTriggerUpdateRequest,
)
from src.domain.models.organisation_tables import (
    AgentStatus,
    AutomationTrigger,
    AutomationTriggerStatus,
    UserAgent,
)
from src.service.automation.errors import AutomationServiceError
from src.service.automation.triggers.service import AutomationTriggerService


class FakeOptions:
    async def validate_crm_selection(self, pipeline_ids: set[UUID], stage_ids: set[UUID]) -> None:
        return None

    async def list_pipelines(self) -> list[object]:
        return []


class FakeAccess:
    def __init__(self, agent: UserAgent) -> None:
        self.agent = agent
        self.trigger: AutomationTrigger | None = None
        self.replace_values: dict[str, object] | None = None

    async def get_agent(self, agent_id: UUID) -> UserAgent | None:
        return self.agent if agent_id == self.agent.agent_id else None

    async def find_duplicate(self, *args: object, **kwargs: object) -> None:
        return None

    async def create_trigger(self, trigger: AutomationTrigger) -> None:
        self.trigger = trigger

    async def get_trigger(
        self, agent_id: UUID, trigger_id: UUID, *, include_archived: bool = False
    ) -> AutomationTrigger | None:
        if self.trigger and self.trigger.trigger_id == trigger_id:
            return self.trigger
        return None

    async def replace_trigger(
        self,
        agent_id: UUID,
        trigger_id: UUID,
        *,
        expected_updated_at: datetime,
        values: dict[str, object],
    ) -> AutomationTrigger | None:
        assert self.trigger is not None
        self.replace_values = values
        for key, value in values.items():
            setattr(self.trigger, key, value)
        self.trigger.updated_at = expected_updated_at + timedelta(seconds=1)
        return self.trigger


def make_agent(*, revision: int = 3, status: AgentStatus = AgentStatus.ACTIVE) -> UserAgent:
    return UserAgent(
        organisation_id=uuid4(),
        owner_user_id=uuid4(),
        revision=revision,
        status=status,
        name="Deal coach",
        prompt="Review the deal",
    )


def schedule_create(*, status: str = "active") -> AutomationTriggerCreateRequest:
    return AutomationTriggerCreateRequest.model_validate(
        {
            "name": "Daily review",
            "status": status,
            "trigger_type": "schedule",
            "trigger_config": {
                "timezone": "UTC",
                "starts_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                "recurrence": "daily",
            },
            "scope_config": {"entity": "deal", "statuses": ["open"]},
        }
    )


def make_service(agent: UserAgent) -> tuple[AutomationTriggerService, FakeAccess, AsyncMock]:
    session = AsyncMock()
    service = AutomationTriggerService(session, agent.organisation_id, agent.owner_user_id)
    access = FakeAccess(agent)
    service.access = access  # type: ignore[assignment]
    service.options = FakeOptions()  # type: ignore[assignment]
    service.validator.options = service.options
    return service, access, session


@pytest.mark.asyncio
async def test_create_pins_current_agent_snapshot_and_never_commits() -> None:
    agent = make_agent()
    service, access, session = make_service(agent)

    result = await service.create_trigger(agent.agent_id, schedule_create())

    assert result.target_revision == 3
    assert result.target_snapshot["prompt"] == "Review the deal"
    assert result.target_snapshot["revision"] == 3
    assert len(result.config_hash) == 64
    session.commit.assert_not_awaited()
    assert access.trigger is not None


@pytest.mark.asyncio
async def test_active_trigger_rejects_paused_agent_but_paused_draft_is_allowed() -> None:
    agent = make_agent(status=AgentStatus.PAUSED)
    service, _, _ = make_service(agent)

    with pytest.raises(AutomationServiceError, match="active agent"):
        await service.create_trigger(agent.agent_id, schedule_create())

    result = await service.create_trigger(agent.agent_id, schedule_create(status="paused"))
    assert result.status == AutomationTriggerStatus.PAUSED


@pytest.mark.asyncio
async def test_replace_preserves_pin_and_refresh_explicitly_repins() -> None:
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
