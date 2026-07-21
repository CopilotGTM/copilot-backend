"""Domain-validation tests for persisted agent definitions."""

from __future__ import annotations

import pytest
from src.agents.tools.registry import ToolName
from src.api.schemas.agent_models import AgentCreateRequest
from src.domain.models.organisation_tables import AgentOutputMode
from src.service.agents.errors import AgentServiceError
from src.service.agents.validator import AgentDefinitionValidator


def _valid_definition(**updates) -> AgentCreateRequest:
    values = {
        "name": "Pipeline Coach",
        "description": "Reviews the owner's open pipeline.",
        "prompt": "Rank open deals and explain the largest risks.",
        "agent_model_config": {"temperature": 0.2},
        "allowed_tools": [ToolName.SEARCH_DEALS.value],
        "allowed_skills": ["pipeline-review"],
        "max_turns": 6,
        "input_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
        },
        "output_mode": AgentOutputMode.JSON,
        "output_schema": {"type": "object", "additionalProperties": False},
    }
    values.update(updates)
    return AgentCreateRequest(**values)


def test_validator_accepts_code_owned_tool_skill_and_valid_json_schemas() -> None:
    AgentDefinitionValidator().validate_definition(_valid_definition())


def test_validator_rejects_unknown_tool_key() -> None:
    body = _valid_definition(allowed_tools=["delete_everything"])

    with pytest.raises(AgentServiceError, match="Unknown agent tools: delete_everything") as exc:
        AgentDefinitionValidator().validate_definition(body)

    assert exc.value.status_code == 400


def test_validator_rejects_known_but_unregistered_general_agent_tool() -> None:
    body = _valid_definition(allowed_tools=[ToolName.GET_BRIEFING.value])

    with pytest.raises(AgentServiceError, match="Unknown agent tools: get_briefing"):
        AgentDefinitionValidator().validate_definition(body)


def test_validator_rejects_unknown_skill_key() -> None:
    body = _valid_definition(allowed_skills=["made-up-sales-skill"])

    with pytest.raises(AgentServiceError, match="Unknown agent skills: made-up-sales-skill"):
        AgentDefinitionValidator().validate_definition(body)


def test_validator_rejects_invalid_json_schema() -> None:
    body = _valid_definition(input_schema={"type": 42})

    with pytest.raises(AgentServiceError, match="input_schema is not valid JSON Schema"):
        AgentDefinitionValidator().validate_definition(body)


@pytest.mark.parametrize("credential_key", ["api_key", "openai_api_key"])
def test_validator_rejects_credentials_nested_in_model_settings(
    credential_key: str,
) -> None:
    body = _valid_definition(agent_model_config={"provider": {credential_key: "not-allowed"}})

    with pytest.raises(AgentServiceError, match="must not contain credentials"):
        AgentDefinitionValidator().validate_definition(body)


def test_validator_rejects_oversized_model_settings() -> None:
    body = _valid_definition(agent_model_config={"notes": "x" * 17_000})

    with pytest.raises(AgentServiceError, match="agent_model_config exceeds 16384 bytes"):
        AgentDefinitionValidator().validate_definition(body)


def test_validator_rechecks_json_output_rule_for_non_api_callers() -> None:
    # model_construct simulates a future worker/service caller bypassing request validation.
    values = _valid_definition().model_dump()
    values.update(output_mode=AgentOutputMode.JSON, output_schema=None)
    body = AgentCreateRequest.model_construct(**values)

    with pytest.raises(AgentServiceError, match="output_schema is required"):
        AgentDefinitionValidator().validate_definition(body)
