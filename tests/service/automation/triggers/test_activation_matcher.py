"""Regression tests for test activation matcher; these cases lock the backend contract used by the changed feature."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from src.service.automation.events.contracts import AutomationEventEnvelope
from src.service.automation.triggers.matcher import (
    MatcherEvaluationError,
    combine_filter_trees,
    matches_binding,
    validate_filter_tree,
    validate_watch,
)
from src.service.automation.triggers.registry import (
    AUTOMATION_REGISTRY,
    FieldType,
    RecordField,
)


def envelope(
    event_key: str = "crm.deal.updated",
    *,
    operation: str = "update",
    record: dict | None = None,
    changes: dict | None = None,
) -> AutomationEventEnvelope:
    """Build the envelope fixture used by the surrounding regression cases."""
    return AutomationEventEnvelope.model_validate(
        {
            "schema_version": 1,
            "event_id": str(uuid4()),
            "organisation_id": str(uuid4()),
            "event_key": event_key,
            "route": {"mode": "match", "trigger_id": None, "config_hash": None},
            "occurred_at": datetime(2026, 8, 5, 8, 30, tzinfo=UTC),
            "source": {
                "domain": event_key.split(".", 1)[0],
                "system": "copilotgtm",
                "operation": operation,
                "mode": "interactive",
                "source_event_id": str(uuid4()),
            },
            "actor": {"type": "user", "id": str(uuid4())},
            "subject": {"type": "deal", "id": str(uuid4())},
            "record": record
            or {
                "id": str(uuid4()),
                "name": "Acme expansion",
                "pipeline_id": str(uuid4()),
                "stage_id": "proposal-stage",
                "deal_status": "open",
                "amount": "125000.00",
                "currency": "USD",
                "owner_id": str(uuid4()),
                "created_by_user_id": str(uuid4()),
                "expected_close_date": "2026-09-30T00:00:00+00:00",
                "account_id": None,
                "primary_contact_id": None,
                "custom_properties": {"region": "APAC"},
            },
            "changes": changes
            if changes is not None
            else {
                "stage_id": {"before": "qualification-stage", "after": "proposal-stage"},
                "amount": {"before": "90000.00", "after": "125000.00"},
            },
            "attributes": {},
            "references": {},
            "correlation_id": None,
            "caused_by_run_id": None,
            "causation_depth": 0,
            "idempotency_key": str(uuid4()),
        }
    )


def test_deal_created_matches_specific_stage_and_custom_value() -> None:
    """Verify the contract that deal created matches specific stage and custom value."""
    base = AUTOMATION_REGISTRY.event("crm.deal.created")
    event = replace(
        base,
        record_fields=base.record_fields
        + (
            RecordField(
                "custom_properties.region",
                "Region",
                FieldType.TEXT,
                accessor=("custom_properties", "region"),
            ),
        ),
    )
    candidate = envelope(
        "crm.deal.created",
        operation="create",
        changes={},
    )
    filter_tree = validate_filter_tree(
        {
            "all": [
                {"path": "record.stage_id", "operator": "eq", "value": "proposal-stage"},
                {
                    "path": "record.custom_properties.region",
                    "operator": "eq",
                    "value": "apac",
                },
            ]
        },
        (event,),
    )

    assert matches_binding(candidate, event, watch=None, filter_tree=filter_tree)


def test_multifield_update_matches_before_after_threshold_once() -> None:
    """Verify the contract that multifield update matches before after threshold once."""
    event = AUTOMATION_REGISTRY.event("crm.deal.updated")
    candidate = envelope()
    filter_tree = validate_filter_tree(
        {
            "all": [
                {"path": "changes.amount.before", "operator": "lt", "value": "100000"},
                {"path": "changes.amount.after", "operator": "gte", "value": "100000"},
                {
                    "path": "changes.stage_id.after",
                    "operator": "eq",
                    "value": "proposal-stage",
                },
            ]
        },
        (event,),
    )
    watch = validate_watch({"mode": "all", "paths": ["amount", "stage_id"]}, (event,))

    assert matches_binding(candidate, event, watch=watch, filter_tree=filter_tree)


def test_watch_rejects_non_watched_update_and_all_requires_every_path() -> None:
    """Verify the contract that watch rejects non watched update and all requires every path."""
    event = AUTOMATION_REGISTRY.event("crm.deal.updated")
    candidate = envelope(changes={"amount": {"before": "1.00", "after": "2.00"}})

    any_watch = validate_watch({"mode": "any", "paths": ["stage_id"]}, (event,))
    all_watch = validate_watch({"mode": "all", "paths": ["amount", "stage_id"]}, (event,))

    assert not matches_binding(candidate, event, watch=any_watch, filter_tree=None)
    assert not matches_binding(candidate, event, watch=all_watch, filter_tree=None)


def test_watch_gates_updates_only_so_create_reaches_current_value_filter() -> None:
    """Verify the contract that watch gates updates only so create reaches current value filter."""
    event = AUTOMATION_REGISTRY.event("crm.deal.created")
    candidate = envelope("crm.deal.created", operation="create", changes={})
    update_event = AUTOMATION_REGISTRY.event("crm.deal.updated")
    watch = validate_watch({"mode": "any", "paths": ["amount"]}, (update_event,))
    filter_tree = validate_filter_tree(
        {"path": "record.amount", "operator": "gte", "value": "100000"},
        (event, update_event),
    )

    assert matches_binding(candidate, event, watch=watch, filter_tree=filter_tree)


def test_missing_path_is_not_null_and_never_matches_is_empty() -> None:
    """Verify the contract that missing path is not null and never matches is empty."""
    event = AUTOMATION_REGISTRY.event("crm.deal.updated")
    record = dict(envelope().record)
    record.pop("account_id")
    missing = envelope(record=record)
    explicit_null = envelope(record={**record, "account_id": None})
    filter_tree = validate_filter_tree(
        {"path": "record.account_id", "operator": "is_empty"},
        (event,),
    )

    assert not matches_binding(missing, event, watch=None, filter_tree=filter_tree)
    assert matches_binding(explicit_null, event, watch=None, filter_tree=filter_tree)


def test_deleted_record_matches_from_pre_delete_snapshot() -> None:
    """Verify the contract that deleted record matches from pre delete snapshot."""
    event = AUTOMATION_REGISTRY.event("crm.deal.deleted")
    candidate = envelope("crm.deal.deleted", operation="delete", changes={})
    filter_tree = validate_filter_tree(
        {"path": "record.deal_status", "operator": "eq", "value": "open"},
        (event,),
    )

    assert matches_binding(candidate, event, watch=None, filter_tree=filter_tree)


def test_unicode_text_is_casefolded_but_exact_fields_are_not() -> None:
    """Verify the contract that unicode text is casefolded but exact fields are not."""
    event = AUTOMATION_REGISTRY.event("crm.deal.updated")
    candidate = envelope(record={**dict(envelope().record), "name": "Straße", "currency": "USD"})
    text_filter = validate_filter_tree(
        {"path": "record.name", "operator": "contains", "value": "STRASSE"},
        (event,),
    )
    exact_filter = validate_filter_tree(
        {"path": "record.currency", "operator": "eq", "value": "usd"},
        (event,),
    )

    assert matches_binding(candidate, event, watch=None, filter_tree=text_filter)
    assert not matches_binding(candidate, event, watch=None, filter_tree=exact_filter)


def test_wrong_value_and_any_group_fail_or_match_deterministically() -> None:
    """Verify the contract that wrong value and any group fail or match deterministically."""
    event = AUTOMATION_REGISTRY.event("crm.deal.updated")
    candidate = envelope()
    filter_tree = validate_filter_tree(
        {
            "any": [
                {"path": "record.deal_status", "operator": "eq", "value": "lost"},
                {"path": "record.amount", "operator": "gt", "value": "100000"},
            ]
        },
        (event,),
    )

    assert matches_binding(candidate, event, watch=None, filter_tree=filter_tree)


def test_schema_invalid_actual_value_fails_closed() -> None:
    """Verify the contract that schema invalid actual value fails closed."""
    event = AUTOMATION_REGISTRY.event("crm.deal.updated")
    candidate = envelope(record={**dict(envelope().record), "amount": "not-a-decimal"})
    filter_tree = validate_filter_tree(
        {"path": "record.amount", "operator": "gte", "value": "1"},
        (event,),
    )

    with pytest.raises(MatcherEvaluationError, match="record.amount"):
        matches_binding(candidate, event, watch=None, filter_tree=filter_tree)


def test_fixed_and_user_filters_are_anded_without_mutation() -> None:
    """Verify the contract that fixed and user filters are anded without mutation."""
    fixed = {"path": "changes.amount.after", "operator": "gte", "value": "100000"}
    user = {"path": "record.deal_status", "operator": "eq", "value": "open"}

    combined = combine_filter_trees(fixed, user)

    assert combined == {"all": [fixed, user]}
    assert fixed == {"path": "changes.amount.after", "operator": "gte", "value": "100000"}
