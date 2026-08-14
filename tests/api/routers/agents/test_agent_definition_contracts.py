"""Request-contract tests for user-owned agent definitions."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from src.contracts.agent_models import (
    AgentCapabilitiesUpdateRequest,
    AgentCreateRequest,
    AgentOutputDeliveryUpdateRequest,
    AgentUpdateRequest,
)
from src.domain.models.organisation_tables import AgentOutputMode, AgentStatus


def _definition_payload() -> dict:
    return {
        "name": "  Pipeline Coach  ",
        "description": "  Helps a rep review open deals.  ",
        "prompt": "  Rank the current user's open deals.  ",
        "model_name": "  gpt-5-mini  ",
        "agent_model_config": {"temperature": 0.2},
        "allowed_tools": ["  search_deals  "],
        "allowed_skills": ["  pipeline-review  "],
        "max_turns": 6,
        "input_schema": {"type": "object"},
        "output_mode": AgentOutputMode.JSON,
        "output_schema": {"type": "object"},
    }


def test_create_contract_normalizes_user_editable_definition_fields() -> None:
    body = AgentCreateRequest(**_definition_payload())

    assert body.name == "Pipeline Coach"
    assert body.description == "Helps a rep review open deals."
    assert body.prompt == "Rank the current user's open deals."
    assert body.model_name == "gpt-5-mini"
    assert body.allowed_tools == ["search_deals"]
    assert body.allowed_skills == ["pipeline-review"]


@pytest.mark.parametrize(
    "server_owned_field",
    [
        "organisation_id",
        "owner_user_id",
        "source_type",
        "source_agent_id",
        "source_version",
        "visibility",
        "agent_type",
        "revision",
    ],
)
def test_create_contract_rejects_server_owned_fields(server_owned_field: str) -> None:
    payload = _definition_payload()
    payload[server_owned_field] = uuid4() if server_owned_field.endswith("_id") else "user"

    with pytest.raises(ValidationError):
        AgentCreateRequest(**payload)


def test_definition_contract_rejects_duplicate_capability_keys_after_normalization() -> None:
    payload = _definition_payload()
    payload["allowed_tools"] = ["search_deals", " search_deals "]

    with pytest.raises(ValidationError, match="allowed_tools values must be unique"):
        AgentCreateRequest(**payload)


def test_definition_contract_returns_validation_error_for_non_string_capability_keys() -> None:
    payload = _definition_payload()
    payload["allowed_tools"] = [{"name": "search_deals"}]

    with pytest.raises(ValidationError):
        AgentCreateRequest(**payload)


def test_capability_update_contract_normalizes_complete_desired_sets() -> None:
    # The focused editor submits one idempotent desired state rather than racing add/remove calls.
    body = AgentCapabilitiesUpdateRequest(
        expected_revision=3,
        allowed_tools=[" search_deals "],
        allowed_skills=[" pipeline-review "],
    )

    assert body.allowed_tools == ["search_deals"]
    assert body.allowed_skills == ["pipeline-review"]


def test_json_output_requires_an_output_schema() -> None:
    payload = _definition_payload()
    payload["output_schema"] = None

    with pytest.raises(ValidationError, match="output_schema is required"):
        AgentCreateRequest(**payload)


def test_replace_contract_requires_a_positive_revision_and_disallows_archive() -> None:
    payload = _definition_payload()
    payload.update(expected_revision=0, status=AgentStatus.ARCHIVED)

    with pytest.raises(ValidationError) as exc_info:
        AgentUpdateRequest(**payload)

    errors = exc_info.value.errors()
    assert {error["loc"] for error in errors} >= {("expected_revision",), ("status",)}


def test_replace_contract_requires_status_as_part_of_the_full_representation() -> None:
    payload = _definition_payload()
    payload["expected_revision"] = 1

    with pytest.raises(ValidationError) as exc_info:
        AgentUpdateRequest(**payload)

    assert ("status",) in {error["loc"] for error in exc_info.value.errors()}


def test_output_delivery_update_accepts_only_the_agent_delivery_decision() -> None:
    expected_updated_at = datetime(2026, 8, 9, 10, 30, tzinfo=UTC)

    body = AgentOutputDeliveryUpdateRequest(
        channels=["email_notification"],
        expected_updated_at=expected_updated_at,
    )

    assert body.channels == ["email_notification"]
    assert body.expected_updated_at == expected_updated_at


@pytest.mark.parametrize(
    "payload",
    [
        {"channels": ["email_notification", "email_notification"]},
        {"channels": ["webhook"]},
        {"channels": [], "recipient": "user@example.com"},
    ],
)
def test_output_delivery_update_rejects_noncanonical_or_server_owned_values(
    payload: dict,
) -> None:
    payload["expected_updated_at"] = datetime(2026, 8, 9, 10, 30, tzinfo=UTC)

    with pytest.raises(ValidationError):
        AgentOutputDeliveryUpdateRequest(**payload)
