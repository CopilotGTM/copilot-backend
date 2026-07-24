"""Orchestration tests for private agent-definition CRUD."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from src.agents.tools.registry import ToolName
from src.api.schemas.agent_models import (
    AgentCapabilitiesUpdateRequest,
    AgentCreateRequest,
    AgentUpdateRequest,
)
from src.domain.models.organisation_tables import (
    AgentOutputMode,
    AgentSourceType,
    AgentStatus,
    AgentVisibility,
    UserAgent,
)
from src.service.agents.definition_service import AgentDefinitionService
from src.service.agents.errors import AgentServiceError
from src.service.agents.template_metadata import DEAL_STAGE_HEALTH_REVIEW_TEMPLATE_ID


class _Session:
    def __init__(self) -> None:
        self.closes = 0

    async def close(self) -> None:
        self.closes += 1


class _Validator:
    def __init__(self) -> None:
        self.validated: list[object] = []

    def validate_definition(self, definition: object) -> None:
        self.validated.append(definition)

    def validate_capabilities(self, allowed_tools: list[str], allowed_skills: list[str]) -> None:
        self.validated.append((allowed_tools, allowed_skills))


class _Access:
    def __init__(self) -> None:
        self.created: UserAgent | None = None
        self.rows: list[UserAgent] = []
        self.total_count = 0
        self.current: UserAgent | None = None
        self.replace_result: UserAgent | None = None
        self.archive_result: UserAgent | None = None
        self.replace_call: dict[str, Any] | None = None
        self.archive_call: tuple[UUID, int] | None = None

    async def create_agent(self, agent: UserAgent) -> None:
        self.created = agent

    async def list_agents(self, **_kwargs) -> tuple[list[UserAgent], int]:
        return self.rows, self.total_count

    async def get_agent(self, _agent_id: UUID, *, include_archived: bool = False):
        return self.current

    async def replace_agent(self, agent_id: UUID, **kwargs):
        self.replace_call = {"agent_id": agent_id, **kwargs}
        return self.replace_result

    async def archive_agent(self, agent_id: UUID, *, expected_revision: int):
        self.archive_call = (agent_id, expected_revision)
        return self.archive_result


def _create_body() -> AgentCreateRequest:
    return AgentCreateRequest(
        name="Pipeline Coach",
        description="Reviews open deals.",
        prompt="Rank the owner's open deals.",
        agent_model_config={"temperature": 0.2},
        allowed_tools=[ToolName.SEARCH_DEALS.value],
        allowed_skills=["pipeline-review"],
        max_turns=6,
        input_schema={"type": "object"},
        output_mode=AgentOutputMode.JSON,
        output_schema={"type": "object"},
    )


def _update_body(*, expected_revision: int = 1) -> AgentUpdateRequest:
    return AgentUpdateRequest(
        **_create_body().model_dump(),
        expected_revision=expected_revision,
        status=AgentStatus.PAUSED,
    )


def _agent(
    *,
    organisation_id: UUID,
    owner_user_id: UUID,
    revision: int = 1,
    status: AgentStatus = AgentStatus.ACTIVE,
) -> UserAgent:
    return UserAgent(
        organisation_id=organisation_id,
        owner_user_id=owner_user_id,
        source_type=AgentSourceType.USER,
        visibility=AgentVisibility.PRIVATE,
        status=status,
        agent_type="general",
        name="Pipeline Coach",
        description="Reviews open deals.",
        prompt="Rank the owner's open deals.",
        model_settings={"temperature": 0.2},
        allowed_tools=[ToolName.SEARCH_DEALS.value],
        allowed_skills=["pipeline-review"],
        max_turns=6,
        input_schema={"type": "object"},
        output_mode=AgentOutputMode.JSON,
        output_schema={"type": "object"},
        revision=revision,
    )


def _service() -> tuple[AgentDefinitionService, _Access, _Validator, UUID, UUID]:
    organisation_id = uuid4()
    owner_user_id = uuid4()
    service = AgentDefinitionService(
        _Session(),  # type: ignore[arg-type]
        organisation_id,
        owner_user_id,
    )
    access = _Access()
    validator = _Validator()
    service.access = access  # type: ignore[assignment]
    service.validator = validator  # type: ignore[assignment]
    return service, access, validator, organisation_id, owner_user_id


@pytest.mark.asyncio
async def test_create_agent_injects_authenticated_identity_and_server_owned_defaults() -> None:
    service, access, validator, organisation_id, owner_user_id = _service()

    result = await service.create_agent(_create_body())

    assert validator.validated == [_create_body()]
    assert access.created is not None
    assert access.created.organisation_id == organisation_id
    assert access.created.owner_user_id == owner_user_id
    assert access.created.source_type == AgentSourceType.USER
    assert access.created.visibility == AgentVisibility.PRIVATE
    assert access.created.agent_type == "general"
    assert access.created.revision == 1
    assert result.agent_id == access.created.agent_id
    assert result.agent_model_config == {"temperature": 0.2}


@pytest.mark.asyncio
async def test_use_template_materialises_a_private_system_sourced_agent() -> None:
    """A catalog selection becomes an editable agent under the authenticated owner."""

    service, access, validator, organisation_id, owner_user_id = _service()

    result = await service.use_template(
        DEAL_STAGE_HEALTH_REVIEW_TEMPLATE_ID,
        version=1,
    )

    assert access.created is not None
    assert access.created.organisation_id == organisation_id
    assert access.created.owner_user_id == owner_user_id
    assert access.created.source_type == AgentSourceType.SYSTEM
    assert access.created.source_agent_id == DEAL_STAGE_HEALTH_REVIEW_TEMPLATE_ID
    assert access.created.source_version == 1
    assert access.created.visibility == AgentVisibility.PRIVATE
    assert access.created.status == AgentStatus.ACTIVE
    assert access.created.revision == 1
    assert access.created.name == "Deal Stage Health Review"
    assert access.created.allowed_skills == ["deal-health-check"]
    assert len(validator.validated) == 1
    validated_definition = validator.validated[0]
    assert isinstance(validated_definition, AgentCreateRequest)
    assert validated_definition.allowed_tools == access.created.allowed_tools
    assert validated_definition.allowed_skills == access.created.allowed_skills
    assert result.source_agent_id == DEAL_STAGE_HEALTH_REVIEW_TEMPLATE_ID
    assert result.source_version == 1


@pytest.mark.asyncio
async def test_use_template_rejects_an_unknown_version_before_creating_an_agent() -> None:
    """The backend must not silently substitute a newer template than the user selected."""

    service, access, _validator, _organisation_id, _owner_user_id = _service()

    with pytest.raises(AgentServiceError, match="Agent template not found") as exc:
        await service.use_template(DEAL_STAGE_HEALTH_REVIEW_TEMPLATE_ID, version=2)

    assert exc.value.status_code == 404
    assert access.created is None


@pytest.mark.asyncio
async def test_list_agents_returns_only_access_rows_in_repo_pagination_shape() -> None:
    service, access, _validator, organisation_id, owner_user_id = _service()
    access.rows = [_agent(organisation_id=organisation_id, owner_user_id=owner_user_id)]
    access.total_count = 1

    result = await service.list_agents(status=None, limit=20, offset=0)

    assert result.total_count == 1
    assert result.limit == 20
    assert result.offset == 0
    assert [item.agent_id for item in result.items] == [access.rows[0].agent_id]


@pytest.mark.asyncio
async def test_get_agent_maps_absent_cross_owner_or_archived_rows_to_not_found() -> None:
    service, _access, _validator, _organisation_id, _owner_user_id = _service()

    with pytest.raises(AgentServiceError, match="Agent not found") as exc:
        await service.get_agent(uuid4())

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_replace_agent_validates_full_definition_and_maps_database_columns() -> None:
    service, access, validator, organisation_id, owner_user_id = _service()
    access.replace_result = _agent(
        organisation_id=organisation_id,
        owner_user_id=owner_user_id,
        revision=2,
        status=AgentStatus.PAUSED,
    )
    agent_id = access.replace_result.agent_id
    body = _update_body(expected_revision=1)

    result = await service.replace_agent(agent_id, body)

    assert validator.validated == [body]
    assert access.replace_call is not None
    assert access.replace_call["expected_revision"] == 1
    assert access.replace_call["values"]["model_config"] == {"temperature": 0.2}
    assert access.replace_call["values"]["status"] == AgentStatus.PAUSED.value
    assert result.revision == 2


@pytest.mark.asyncio
async def test_replace_agent_reports_stale_revision_as_conflict() -> None:
    service, access, _validator, organisation_id, owner_user_id = _service()
    access.current = _agent(
        organisation_id=organisation_id,
        owner_user_id=owner_user_id,
        revision=8,
    )

    with pytest.raises(AgentServiceError, match="changed since it was loaded") as exc:
        await service.replace_agent(access.current.agent_id, _update_body(expected_revision=7))

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_replace_capabilities_validates_and_updates_only_capability_columns() -> None:
    service, access, validator, organisation_id, owner_user_id = _service()
    access.replace_result = _agent(
        organisation_id=organisation_id,
        owner_user_id=owner_user_id,
        revision=2,
    )
    agent_id = access.replace_result.agent_id
    body = AgentCapabilitiesUpdateRequest(
        expected_revision=1,
        allowed_tools=[ToolName.GET_DEAL_DETAILS.value],
        allowed_skills=[],
    )

    result = await service.replace_capabilities(agent_id, body)

    assert validator.validated == [([ToolName.GET_DEAL_DETAILS.value], [])]
    assert access.replace_call == {
        "agent_id": agent_id,
        "expected_revision": 1,
        "values": {
            "allowed_tools": [ToolName.GET_DEAL_DETAILS.value],
            "allowed_skills": [],
        },
    }
    assert result.revision == 2


@pytest.mark.asyncio
async def test_archive_agent_hides_already_archived_rows_as_not_found() -> None:
    service, access, _validator, organisation_id, owner_user_id = _service()
    access.current = _agent(
        organisation_id=organisation_id,
        owner_user_id=owner_user_id,
        revision=4,
        status=AgentStatus.ARCHIVED,
    )

    with pytest.raises(AgentServiceError, match="Agent not found") as exc:
        await service.archive_agent(access.current.agent_id, expected_revision=4)

    assert exc.value.status_code == 404
    assert access.archive_call == (access.current.agent_id, 4)


def test_agent_definition_services_do_not_own_transaction_commits() -> None:
    service_root = Path("src/service/agents")
    source = "\n".join(path.read_text(encoding="utf-8") for path in service_root.glob("*.py"))

    assert ".commit(" not in source
