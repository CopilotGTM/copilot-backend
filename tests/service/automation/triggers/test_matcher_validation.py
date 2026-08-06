"""Regression tests for test matcher validation; these cases lock the backend contract used by the changed feature."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from src.service.automation.triggers.matcher import (
    MatcherValidationError,
    evaluate_operator,
    validate_filter_tree,
    validate_watch,
)
from src.service.automation.triggers.registry import AUTOMATION_REGISTRY, FieldType


@pytest.mark.parametrize(
    ("field_type", "operator", "actual", "expected", "matches"),
    [
        (FieldType.DECIMAL, "gte", "125000.00", "100000.00", True),
        (FieldType.DECIMAL, "between", "100.00", ["100", "200"], True),
        (FieldType.INTEGER, "lt", 4, 5, True),
        (FieldType.TEXT, "starts_with", "Expansion", "exp", True),
        (FieldType.EMAIL, "eq", "Owner@Acme.COM", "owner@acme.com", True),
        (FieldType.DATE, "before", "2026-08-04", "2026-08-05", True),
        (
            FieldType.DATETIME,
            "after",
            "2026-08-05T10:00:00+00:00",
            "2026-08-05T09:00:00Z",
            True,
        ),
        (FieldType.LIST, "contains_all", ["a", "b"], ["b", "a"], True),
        (FieldType.LIST, "contains_any", ["a", "b"], ["z", "b"], True),
        (
            FieldType.LIST,
            "contains",
            ["buyer@example.com"],
            "Buyer@Example.com",
            True,
        ),
        (FieldType.BOOLEAN, "neq", True, False, True),
    ],
)
def test_typed_operators(field_type, operator, actual, expected, matches) -> None:
    """Verify the contract that typed operators."""
    assert evaluate_operator(field_type, operator, actual, expected) is matches


@pytest.mark.parametrize(
    ("field_type", "operator", "actual", "expected"),
    [
        (FieldType.DECIMAL, "gte", "NaN", "1"),
        (FieldType.INTEGER, "gt", True, 0),
        (FieldType.DATE, "before", "2026-02-30", "2026-03-01"),
        (FieldType.DATETIME, "after", "2026-08-05T10:00:00", "2026-08-05T09:00:00Z"),
        (FieldType.LIST, "contains_all", "not-a-list", ["a"]),
    ],
)
def test_invalid_actual_values_raise_schema_error(field_type, operator, actual, expected) -> None:
    """Verify the contract that invalid actual values raise schema error."""
    with pytest.raises(ValueError):
        evaluate_operator(field_type, operator, actual, expected)


def test_empty_operator_still_rejects_a_wrong_runtime_type() -> None:
    """Verify the contract that empty operator still rejects a wrong runtime type."""
    with pytest.raises(ValueError):
        evaluate_operator(FieldType.TEXT, "is_empty", {})


@pytest.mark.parametrize(
    "tree",
    [
        {"path": "record.missing", "operator": "eq", "value": "x"},
        {"path": "record.amount", "operator": "regex", "value": ".*"},
        {"path": "record.amount", "operator": "gte", "value": 1.0},
        {"path": "record.amount", "operator": "between", "value": ["1"]},
        {"path": "record.name", "operator": "is_empty", "value": None},
        {"all": []},
        {"all": [], "any": []},
        {"all": [{"unexpected": True}]},
    ],
)
def test_filter_validation_rejects_unknown_or_ill_typed_contracts(tree) -> None:
    """Verify the contract that filter validation rejects unknown or ill typed contracts."""
    event = AUTOMATION_REGISTRY.event("crm.deal.updated")

    with pytest.raises(MatcherValidationError):
        validate_filter_tree(tree, (event,))


def test_filter_tree_allows_two_group_levels_and_twenty_leaves() -> None:
    """Verify the contract that filter tree allows two group levels and twenty leaves."""
    event = AUTOMATION_REGISTRY.event("crm.deal.updated")
    leaf = {"path": "record.deal_status", "operator": "eq", "value": "open"}
    tree = {"all": [{"any": [dict(leaf) for _ in range(10)]}, *[dict(leaf) for _ in range(10)]]}

    validated = validate_filter_tree(tree, (event,))

    assert validated.leaf_count == 20


def test_filter_tree_rejects_third_group_level_and_twenty_first_leaf() -> None:
    """Verify the contract that filter tree rejects third group level and twenty first leaf."""
    event = AUTOMATION_REGISTRY.event("crm.deal.updated")
    leaf = {"path": "record.deal_status", "operator": "eq", "value": "open"}

    with pytest.raises(MatcherValidationError, match="group levels"):
        validate_filter_tree({"all": [{"any": [{"all": [leaf]}]}]}, (event,))
    with pytest.raises(MatcherValidationError, match="20"):
        validate_filter_tree({"all": [dict(leaf) for _ in range(21)]}, (event,))


@pytest.mark.parametrize(
    "watch",
    [
        {"mode": "some", "paths": ["amount"]},
        {"mode": "any", "paths": []},
        {"mode": "any", "paths": ["missing"]},
        {"mode": "any", "paths": ["amount", "amount"]},
        {"mode": "any", "paths": ["amount"], "extra": True},
    ],
)
def test_watch_validation_rejects_invalid_contracts(watch) -> None:
    """Verify the contract that watch validation rejects invalid contracts."""
    event = AUTOMATION_REGISTRY.event("crm.deal.updated")

    with pytest.raises(MatcherValidationError):
        validate_watch(watch, (event,))


def test_datetime_comparison_normalizes_offsets_to_utc() -> None:
    """Verify the contract that datetime comparison normalizes offsets to utc."""
    assert evaluate_operator(
        FieldType.DATETIME,
        "eq",
        datetime(2026, 8, 5, 15, 30, tzinfo=UTC).isoformat(),
        "2026-08-05T21:00:00+05:30",
    )
