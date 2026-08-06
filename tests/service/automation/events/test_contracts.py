"""Regression tests for test; these cases lock the backend contract used by the changed feature."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from src.service.automation.events.contracts import AutomationEventEnvelope


@pytest.fixture
def valid_envelope_data() -> dict[str, object]:
    """Build the valid envelope data fixture used by the surrounding regression cases."""
    organisation_id = uuid4()
    return {
        "schema_version": 1,
        "event_id": str(uuid4()),
        "organisation_id": str(organisation_id),
        "event_key": "crm.deal.updated",
        "route": {"mode": "match", "trigger_id": None, "config_hash": None},
        "occurred_at": datetime(2026, 8, 5, 8, 30, tzinfo=UTC).isoformat(),
        "source": {
            "domain": "crm",
            "system": "copilotgtm",
            "operation": "update",
            "mode": "interactive",
            "source_event_id": "mutation-123",
        },
        "actor": {"type": "user", "id": str(uuid4())},
        "subject": {"type": "deal", "id": str(uuid4())},
        "record": {
            "id": str(uuid4()),
            "amount": "125000.00",
            "active": True,
            "labels": ["enterprise"],
            "optional": None,
        },
        "changes": {
            "amount": {"before": "90000.00", "after": "125000.00"},
            "owner_id": {"before": None, "after": str(uuid4())},
        },
        "attributes": {"origin": "api"},
        "references": {"mutation_id": "mutation-123"},
        "correlation_id": "request-123",
        "caused_by_run_id": None,
        "causation_depth": 0,
        "idempotency_key": f"{organisation_id}:crm.deal.updated:mutation-123",
    }


def test_valid_match_envelope_is_frozen_and_json_serializable(valid_envelope_data):
    """Verify the contract that valid match envelope is frozen and json serializable."""
    envelope = AutomationEventEnvelope.model_validate(valid_envelope_data)

    assert envelope.schema_version == 1
    assert envelope.occurred_at.tzinfo is UTC
    assert envelope.changes["owner_id"].before is None
    assert envelope.model_dump(mode="json")["record"]["amount"] == "125000.00"

    with pytest.raises(ValidationError):
        envelope.event_key = "crm.deal.created"


@pytest.mark.parametrize("trigger_id", [None, "missing"])
def test_direct_route_requires_uuid_trigger_and_sha256_hash(valid_envelope_data, trigger_id):
    """Verify the contract that direct route requires uuid trigger and sha256 hash."""
    valid_envelope_data["route"] = {
        "mode": "direct",
        "trigger_id": trigger_id,
        "config_hash": None,
    }

    with pytest.raises(ValidationError):
        AutomationEventEnvelope.model_validate(valid_envelope_data)


def test_valid_direct_route_requires_both_direct_projections(valid_envelope_data):
    """Verify the contract that valid direct route requires both direct projections."""
    trigger_id = uuid4()
    valid_envelope_data["route"] = {
        "mode": "direct",
        "trigger_id": str(trigger_id),
        "config_hash": "a" * 64,
    }

    envelope = AutomationEventEnvelope.model_validate(valid_envelope_data)

    assert envelope.route.trigger_id == trigger_id
    assert envelope.route.config_hash == "a" * 64


def test_match_route_forbids_direct_identity(valid_envelope_data):
    """Verify the contract that match route forbids direct identity."""
    valid_envelope_data["route"] = {
        "mode": "match",
        "trigger_id": str(uuid4()),
        "config_hash": "a" * 64,
    }

    with pytest.raises(ValidationError, match="match route"):
        AutomationEventEnvelope.model_validate(valid_envelope_data)


@pytest.mark.parametrize(
    "subject",
    [
        {"type": "deal", "id": None},
        {"type": None, "id": str(uuid4())},
        {"type": "", "id": "deal-1"},
    ],
)
def test_subject_type_and_id_are_both_present_or_both_null(valid_envelope_data, subject):
    """Verify the contract that subject type and id are both present or both null."""
    valid_envelope_data["subject"] = subject

    with pytest.raises(ValidationError, match="subject"):
        AutomationEventEnvelope.model_validate(valid_envelope_data)


def test_global_subject_is_valid(valid_envelope_data):
    """Verify the contract that global subject is valid."""
    valid_envelope_data["subject"] = {"type": None, "id": None}

    envelope = AutomationEventEnvelope.model_validate(valid_envelope_data)

    assert envelope.subject.type is None
    assert envelope.subject.id is None


@pytest.mark.parametrize(
    "section,value",
    [
        ("record", {"bad": Decimal("1.00")}),
        ("attributes", {"bad": float("nan")}),
        ("references", {"bad": ("tuple",)}),
        ("record", {"bad": {1: "non-string-key"}}),
    ],
)
def test_envelope_rejects_values_outside_strict_json(
    valid_envelope_data,
    section,
    value,
):
    """Verify the contract that envelope rejects values outside strict json."""
    valid_envelope_data[section] = value

    with pytest.raises(ValidationError, match="strict JSON"):
        AutomationEventEnvelope.model_validate(valid_envelope_data)


def test_change_requires_explicit_before_and_after(valid_envelope_data):
    """Verify the contract that change requires explicit before and after."""
    valid_envelope_data["changes"] = {"amount": {"after": "1.00"}}

    with pytest.raises(ValidationError):
        AutomationEventEnvelope.model_validate(valid_envelope_data)


def test_envelope_rejects_unknown_fields_at_every_level(valid_envelope_data):
    """Verify the contract that envelope rejects unknown fields at every level."""
    valid_envelope_data["source"]["provider_token"] = "secret"

    with pytest.raises(ValidationError, match="provider_token"):
        AutomationEventEnvelope.model_validate(valid_envelope_data)


def test_occurred_at_must_be_an_aware_utc_datetime(valid_envelope_data):
    """Verify the contract that occurred at must be an aware utc datetime."""
    valid_envelope_data["occurred_at"] = datetime(2026, 8, 5, 8, 30)
    with pytest.raises(ValidationError, match="UTC"):
        AutomationEventEnvelope.model_validate(valid_envelope_data)

    valid_envelope_data["occurred_at"] = datetime(
        2026,
        8,
        5,
        8,
        30,
        tzinfo=timezone(timedelta(hours=5, minutes=30)),
    )
    with pytest.raises(ValidationError, match="UTC"):
        AutomationEventEnvelope.model_validate(valid_envelope_data)


@pytest.mark.parametrize("depth", [-1, 4])
def test_envelope_rejects_out_of_range_causation_depth(valid_envelope_data, depth):
    """Verify the contract that envelope rejects out of range causation depth."""
    valid_envelope_data["causation_depth"] = depth

    with pytest.raises(ValidationError):
        AutomationEventEnvelope.model_validate(valid_envelope_data)


@pytest.mark.parametrize(
    ("has_parent", "causation_depth"),
    [(False, 1), (True, 0)],
)
def test_envelope_requires_complete_causation_lineage(
    valid_envelope_data,
    has_parent,
    causation_depth,
):
    """Verify the contract that envelope requires complete causation lineage."""
    valid_envelope_data["caused_by_run_id"] = str(uuid4()) if has_parent else None
    valid_envelope_data["causation_depth"] = causation_depth

    with pytest.raises(ValidationError, match="causation"):
        AutomationEventEnvelope.model_validate(valid_envelope_data)


def test_envelope_enforces_64_kib_serialized_limit(valid_envelope_data):
    """Verify the contract that envelope enforces 64 kib serialized limit."""
    valid_envelope_data["record"] = {"large": "x" * 65_536}

    with pytest.raises(ValidationError, match="65536 bytes"):
        AutomationEventEnvelope.model_validate(valid_envelope_data)


def test_envelope_snapshots_unique_audience_user_ids(valid_envelope_data):
    """Verify the contract that envelope snapshots unique audience user ids."""
    user_id = uuid4()
    valid_envelope_data["audience_user_ids"] = [str(user_id)]

    envelope = AutomationEventEnvelope.model_validate(valid_envelope_data)

    assert envelope.audience_user_ids == (user_id,)

    valid_envelope_data["audience_user_ids"] = [str(user_id), str(user_id)]
    with pytest.raises(ValidationError, match="audience"):
        AutomationEventEnvelope.model_validate(valid_envelope_data)
