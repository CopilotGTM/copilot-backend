"""HTTP transaction and response tests for agent-definition endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import Response as FastAPIResponse
from src.agents.tools.registry import ToolName
from src.api.dependencies.organisation_dependencies import UserIDs
from src.api.routers.agents.capabilities_handler import get_agent_capability_catalog
from src.api.routers.agents.definitions_handler import (
    archive_agent_definition,
    create_agent_definition,
    get_agent_definition_service,
    list_agent_definitions,
    replace_agent_capabilities,
    replace_agent_definition,
)
from src.api.schemas.agent_models import (
    AgentCapabilitiesUpdateRequest,
    AgentCreateRequest,
    AgentDetail,
    AgentListResponse,
    AgentUpdateRequest,
)
from src.domain.models.organisation_tables import (
    AgentOutputMode,
    AgentSourceType,
    AgentStatus,
    AgentVisibility,
)
from src.service.agents.errors import AgentServiceError


class _Session:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def close(self) -> None:
        self.closes += 1


class _Service:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.session = _Session()
        self.failure = failure
        self.detail = _detail()

    def _raise_if_needed(self) -> None:
        if self.failure is not None:
            raise self.failure

    async def create_agent(self, _body):
        self._raise_if_needed()
        return self.detail

    async def list_agents(self, **_kwargs):
        self._raise_if_needed()
        return AgentListResponse(items=[], total_count=0, limit=20, offset=0)

    async def replace_agent(self, _agent_id, _body):
        self._raise_if_needed()
        return self.detail

    async def replace_capabilities(self, _agent_id, _body):
        self._raise_if_needed()
        return self.detail

    async def archive_agent(self, _agent_id, *, expected_revision):
        self._raise_if_needed()
        return None


def _detail() -> AgentDetail:
    now = datetime(2026, 7, 13, tzinfo=UTC)
    return AgentDetail(
        agent_id=uuid4(),
        revision=1,
        source_type=AgentSourceType.USER,
        status=AgentStatus.ACTIVE,
        name="Pipeline Coach",
        description="Reviews open deals.",
        agent_type="general",
        model_name=None,
        allowed_tools=[ToolName.SEARCH_DEALS.value],
        allowed_skills=["pipeline-review"],
        output_mode=AgentOutputMode.TEXT,
        created_at=now,
        updated_at=now,
        owner_user_id=uuid4(),
        source_agent_id=None,
        source_version=None,
        visibility=AgentVisibility.PRIVATE,
        prompt="Rank open deals.",
        agent_model_config={},
        max_turns=None,
        input_schema=None,
        output_schema=None,
    )


def _create_body() -> AgentCreateRequest:
    return AgentCreateRequest(name="Pipeline Coach", prompt="Rank open deals.")


def _update_body() -> AgentUpdateRequest:
    return AgentUpdateRequest(
        **_create_body().model_dump(),
        expected_revision=1,
        status=AgentStatus.PAUSED,
    )


@pytest.mark.asyncio
async def test_service_dependency_binds_authenticated_org_and_owner() -> None:
    session = _Session()
    user_ids = UserIDs(user_id=uuid4(), organisation_id=uuid4())
    dependency = get_agent_definition_service(
        user_ids=user_ids,
        org_session=session,  # type: ignore[arg-type]
    )

    service = await anext(dependency)

    assert service.organisation_id == user_ids.organisation_id
    assert service.owner_user_id == user_ids.user_id
    await dependency.aclose()
    assert session.closes == 1


@pytest.mark.asyncio
async def test_create_agent_returns_201_location_and_commits() -> None:
    service = _Service()
    response = FastAPIResponse()

    result = await create_agent_definition(
        body=_create_body(),
        response=response,
        service=service,  # type: ignore[arg-type]
        _rl_write=None,
    )

    assert result.status == "SUCCESS"
    assert response.status_code == 201
    assert response.headers["Location"] == f"/users/v1/agents/{service.detail.agent_id}"
    assert service.session.commits == 1
    assert service.session.rollbacks == 0


@pytest.mark.asyncio
async def test_list_agents_is_read_only() -> None:
    service = _Service()

    result = await list_agent_definitions(
        response=FastAPIResponse(),
        status=None,
        limit=20,
        offset=0,
        service=service,  # type: ignore[arg-type]
        _rl_read=None,
    )

    assert result.status == "SUCCESS"
    assert service.session.commits == 0
    assert service.session.rollbacks == 0


@pytest.mark.asyncio
async def test_replace_agent_rolls_back_and_returns_service_conflict() -> None:
    service = _Service(
        failure=AgentServiceError(
            "Agent changed since it was loaded; refresh and retry",
            status_code=409,
        )
    )
    response = FastAPIResponse()

    result = await replace_agent_definition(
        agent_id=uuid4(),
        body=_update_body(),
        response=response,
        service=service,  # type: ignore[arg-type]
        _rl_write=None,
    )

    assert result.status == "ERROR"
    assert response.status_code == 409
    assert service.session.commits == 0
    assert service.session.rollbacks == 1


@pytest.mark.asyncio
async def test_replace_capabilities_commits_atomic_set_update() -> None:
    service = _Service()

    result = await replace_agent_capabilities(
        agent_id=uuid4(),
        body=AgentCapabilitiesUpdateRequest(
            expected_revision=1,
            allowed_tools=[ToolName.SEARCH_DEALS.value],
            allowed_skills=[],
        ),
        response=FastAPIResponse(),
        service=service,  # type: ignore[arg-type]
        _rl_write=None,
    )

    assert result.status == "SUCCESS"
    assert service.session.commits == 1
    assert service.session.rollbacks == 0


@pytest.mark.asyncio
async def test_capability_catalog_is_read_only() -> None:
    result = await get_agent_capability_catalog(
        _user_ids=UserIDs(user_id=uuid4(), organisation_id=uuid4()),
        _rl_read=None,
    )

    assert result.status == "SUCCESS"
    assert any(item.key == ToolName.SEARCH_DEALS.value for item in result.data.tools)


@pytest.mark.asyncio
async def test_archive_agent_returns_204_and_commits() -> None:
    service = _Service()

    result = await archive_agent_definition(
        agent_id=uuid4(),
        expected_revision=1,
        response=FastAPIResponse(),
        service=service,  # type: ignore[arg-type]
        _rl_write=None,
    )

    assert result.status_code == 204
    assert not result.body
    assert service.session.commits == 1
    assert service.session.rollbacks == 0


@pytest.mark.asyncio
async def test_unexpected_create_failure_rolls_back_and_is_sanitized() -> None:
    service = _Service(failure=RuntimeError("database details must stay private"))
    response = FastAPIResponse()

    result = await create_agent_definition(
        body=_create_body(),
        response=response,
        service=service,  # type: ignore[arg-type]
        _rl_write=None,
    )

    assert result.status == "ERROR"
    assert response.status_code == 500
    assert "database details" not in result.message
    assert service.session.rollbacks == 1
