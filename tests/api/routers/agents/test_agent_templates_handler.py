"""HTTP contracts for code-owned agent templates and materialisation."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import Response as FastAPIResponse
from src.api.dependencies.organisation_dependencies import UserIDs
from src.api.schemas.agent_models import AgentDetail, UseAgentTemplateRequest
from src.domain.models.organisation_tables import (
    AgentOutputMode,
    AgentSourceType,
    AgentStatus,
    AgentVisibility,
)
from src.service.agents.errors import AgentServiceError
from src.service.agents.template_metadata import DEAL_STAGE_HEALTH_REVIEW_TEMPLATE_ID


class _Session:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        """Record the handler's successful transaction boundary."""

        self.commits += 1

    async def rollback(self) -> None:
        """Record the handler's failed transaction boundary."""

        self.rollbacks += 1


class _Service:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.session = _Session()
        self.failure = failure
        self.detail = _detail()

    async def use_template(self, _template_id, *, version):
        """Return one complete user-agent shape or the configured domain failure."""

        if self.failure is not None:
            raise self.failure
        assert version == 1
        return self.detail


def _detail() -> AgentDetail:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    return AgentDetail(
        agent_id=uuid4(),
        revision=1,
        source_type=AgentSourceType.SYSTEM,
        status=AgentStatus.ACTIVE,
        name="Deal Stage Health Review",
        description="Reviews deal health after a stage change.",
        agent_type="general",
        model_name=None,
        allowed_tools=["get_deal_details"],
        allowed_skills=["deal-health-check"],
        output_mode=AgentOutputMode.JSON,
        created_at=now,
        updated_at=now,
        owner_user_id=uuid4(),
        source_agent_id=DEAL_STAGE_HEALTH_REVIEW_TEMPLATE_ID,
        source_version=1,
        visibility=AgentVisibility.PRIVATE,
        prompt="Review the deal.",
        agent_model_config={"temperature": 0.1},
        max_turns=12,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )


def _handler_module():
    return importlib.import_module("src.api.routers.agents.templates_handler")


@pytest.mark.asyncio
async def test_template_catalog_is_read_only_code_metadata() -> None:
    """Listing base agents must not create user rows as a side effect."""

    result = await _handler_module().list_agent_templates(
        _user_ids=UserIDs(user_id=uuid4(), organisation_id=uuid4()),
        _rl_read=None,
    )

    assert result.status == "SUCCESS"
    assert result.data.total_count == 1
    assert result.data.items[0].template_id == DEAL_STAGE_HEALTH_REVIEW_TEMPLATE_ID
    assert result.data.items[0].version == 1


@pytest.mark.asyncio
async def test_template_detail_hides_an_unknown_metadata_id() -> None:
    """An arbitrary UUID must not be confused with a platform-shipped template."""

    response = FastAPIResponse()
    result = await _handler_module().get_agent_template_detail(
        template_id=uuid4(),
        response=response,
        _user_ids=UserIDs(user_id=uuid4(), organisation_id=uuid4()),
        _rl_read=None,
    )

    assert result.status == "ERROR"
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_use_template_returns_created_agent_location_and_commits() -> None:
    """The use action commits only after the owned agent has been materialised."""

    service = _Service()
    response = FastAPIResponse()

    result = await _handler_module().use_agent_template(
        template_id=DEAL_STAGE_HEALTH_REVIEW_TEMPLATE_ID,
        body=UseAgentTemplateRequest(version=1),
        response=response,
        service=service,
        _rl_write=None,
    )

    assert result.status == "SUCCESS"
    assert response.status_code == 201
    assert response.headers["Location"] == f"/users/v1/agents/{service.detail.agent_id}"
    assert service.session.commits == 1
    assert service.session.rollbacks == 0


@pytest.mark.asyncio
async def test_use_template_rolls_back_a_known_materialisation_failure() -> None:
    """A rejected template selection must leave no partial user-agent transaction."""

    service = _Service(failure=AgentServiceError("Agent template not found", status_code=404))
    response = FastAPIResponse()

    result = await _handler_module().use_agent_template(
        template_id=DEAL_STAGE_HEALTH_REVIEW_TEMPLATE_ID,
        body=UseAgentTemplateRequest(version=2),
        response=response,
        service=service,
        _rl_write=None,
    )

    assert result.status == "ERROR"
    assert response.status_code == 404
    assert service.session.commits == 0
    assert service.session.rollbacks == 1
