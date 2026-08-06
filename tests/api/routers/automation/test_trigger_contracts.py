"""HTTP-contract tests for user-owned automation triggers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from src.api.schemas.automation_trigger_models import (
    AutomationTriggerCreateRequest,
    AutomationTriggerUpdateRequest,
)
from src.domain.models.organisation_tables import AutomationTriggerStatus


def _schedule_payload() -> dict:
    return {
        "name": "  Friday pipeline review  ",
        "status": AutomationTriggerStatus.ACTIVE,
        "trigger_key": "time.schedule.due",
        "config": {
            "timezone": "Asia/Kolkata",
            "starts_at": "2026-07-17T09:00:00+05:30",
            "recurrence": "weekly",
        },
        "principal_mode": "personal",
        "scope": {"type": "owned_or_created_by_principal"},
        "audience": {"mode": "principal"},
        "task": {"goal": "  Rank my open deals by risk.  "},
    }


def test_create_contract_normalizes_user_editable_trigger_fields() -> None:
    """Verify the contract that create contract normalizes user editable trigger fields."""
    body = AutomationTriggerCreateRequest(**_schedule_payload())

    assert body.name == "Friday pipeline review"
    assert body.task.goal == "Rank my open deals by risk."


@pytest.mark.parametrize(
    "field_name",
    [
        "organisation_id",
        "created_by_user_id",
        "target_type",
        "target_id",
        "target_revision",
        "target_snapshot",
        "family",
        "match_event_keys",
        "execution_principal_type",
        "execution_principal_id",
        "config_schema_version",
        "config_hash",
        "next_trigger_at",
        "last_triggered_at",
        "trigger_count",
        "consecutive_failures",
        "last_error",
    ],
)
def test_create_contract_rejects_server_owned_fields(field_name: str) -> None:
    """Verify the contract that create contract rejects server owned fields."""
    payload = {**_schedule_payload(), field_name: "client-controlled"}

    with pytest.raises(ValidationError):
        AutomationTriggerCreateRequest(**payload)


def test_trigger_write_contract_rejects_archived_status() -> None:
    """Verify the contract that trigger write contract rejects archived status."""
    payload = {**_schedule_payload(), "status": AutomationTriggerStatus.ARCHIVED}

    with pytest.raises(ValidationError, match="archived status is only available through DELETE"):
        AutomationTriggerCreateRequest(**payload)


def test_update_contract_requires_complete_body_and_aware_concurrency_token() -> None:
    """Verify the contract that update contract requires complete body and aware concurrency token."""
    with pytest.raises(ValidationError):
        AutomationTriggerUpdateRequest(expected_updated_at=datetime.now(UTC))

    with pytest.raises(ValidationError, match="expected_updated_at must include timezone"):
        AutomationTriggerUpdateRequest(
            **_schedule_payload(),
            expected_updated_at=datetime(2026, 7, 13, 9, 0),
        )

    body = AutomationTriggerUpdateRequest(
        **_schedule_payload(),
        expected_updated_at=datetime(2026, 7, 13, 9, 0, tzinfo=UTC),
    )
    assert body.expected_updated_at.tzinfo is not None
