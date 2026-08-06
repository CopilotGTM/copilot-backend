"""Pure typed watch and filter validation over automation event envelopes.

This module deliberately has no database or model-runtime dependency. A retry
therefore evaluates the immutable event-time values exactly as the first
delivery did, including the distinction between a missing path and JSON null.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal, TypeAlias, cast
from uuid import UUID

from pydantic import BaseModel
from src.service.automation.events.contracts import (
    AutomationEventEnvelope,
    AutomationSourceOperation,
)
from src.service.automation.triggers.registry import (
    EventDefinition,
    FieldDefinition,
    FieldType,
    TriggerDefinition,
    WatchDefinition,
)

# Bound recursive matching work at validation time; the registry contract lets
# callers compose two group levels and at most twenty leaf predicates.
MAX_FILTER_GROUP_LEVELS = 2
MAX_FILTER_LEAVES = 20
_MISSING = object()


class MatcherValidationError(ValueError):
    """A saved watch or filter does not match its registry contract."""


class MatcherEvaluationError(ValueError):
    """An event value drifted from the field type promised by its producer."""


@dataclass(frozen=True)
class FilterLeaf:
    """One typed predicate with its registry-owned accessor."""

    field: FieldDefinition
    operator: str
    value: Any = _MISSING

    @property
    def leaf_count(self) -> int:
        """Count the filter leaves in the supplied matcher tree."""
        return 1


@dataclass(frozen=True)
class FilterGroup:
    """Boolean group containing leaves or one further group level."""

    mode: str
    children: tuple[FilterNode, ...]

    @property
    def leaf_count(self) -> int:
        """Count the filter leaves in the supplied matcher tree."""
        return sum(child.leaf_count for child in self.children)


FilterNode: TypeAlias = FilterLeaf | FilterGroup


def collect_option_selections(
    definition: TriggerDefinition,
    config: Mapping[str, Any],
    filter_node: FilterNode | None,
) -> dict[str, set[str]]:
    """Collect tenant-owned stable IDs referenced by one compiled binding."""

    selections: dict[str, set[str]] = {}
    for field in definition.config_fields:
        if field.option_source is None or field.key not in config:
            continue
        selections.setdefault(field.option_source, set()).update(
            _option_values(config[field.key], field.key)
        )

    def visit(node: FilterNode) -> None:
        """Visit one matcher node while accumulating the compiled filter state."""
        if isinstance(node, FilterGroup):
            for child in node.children:
                visit(child)
            return
        if node.field.option_source is None or node.operator in {"is_empty", "not_empty"}:
            return
        selections.setdefault(node.field.option_source, set()).update(
            _option_values(node.value, node.field.path)
        )

    if filter_node is not None:
        visit(filter_node)
    return selections


def _option_values(value: Any, field_name: str) -> set[str]:
    values = value if type(value) is list else [value]
    if any(type(item) is not str for item in values):
        raise MatcherValidationError(f"{field_name} must contain stable string IDs")
    return set(values)


def validate_watch(
    value: Mapping[str, Any] | WatchDefinition | None,
    events: Iterable[EventDefinition],
) -> WatchDefinition | None:
    """Validate update-field names against the selected event definitions."""

    if value is None:
        return None
    if isinstance(value, WatchDefinition):
        mode, paths = value.mode, value.paths
    else:
        if not isinstance(value, Mapping) or set(value) != {"mode", "paths"}:
            raise MatcherValidationError("watch must contain only mode and paths")
        mode = value["mode"]
        raw_paths = value["paths"]
        if type(raw_paths) is not list or not all(type(item) is str for item in raw_paths):
            raise MatcherValidationError("watch paths must be a list of strings")
        paths = tuple(raw_paths)
    if mode not in {"any", "all"}:
        raise MatcherValidationError("watch mode must be any or all")
    if not paths or len(paths) != len(set(paths)):
        raise MatcherValidationError("watch paths must be non-empty and unique")

    available = {path for event in events for path in event.watch_fields}
    unknown = set(paths) - available
    if unknown:
        raise MatcherValidationError(f"unknown watch paths: {sorted(unknown)}")
    return WatchDefinition(mode=cast(Literal["any", "all"], mode), paths=tuple(paths))


def validate_filter_tree(
    value: Mapping[str, Any] | None,
    events: Iterable[EventDefinition],
) -> FilterNode | None:
    """Validate paths, operators, types, depth, and total leaf count."""

    if value is None:
        return None
    fields = _merged_field_catalog(events)
    node = _parse_filter_node(value, fields, group_level=0)
    if node.leaf_count > MAX_FILTER_LEAVES:
        raise MatcherValidationError(f"filter supports at most {MAX_FILTER_LEAVES} conditions")
    return node


def combine_filter_trees(
    fixed: Mapping[str, Any] | None,
    user: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """AND fixed and user predicates without sharing their mutable objects."""

    if fixed is None:
        return deepcopy(dict(user)) if user is not None else None
    if user is None:
        return deepcopy(dict(fixed))
    return {"all": [deepcopy(dict(fixed)), deepcopy(dict(user))]}


def matches_binding(
    envelope: AutomationEventEnvelope,
    event: EventDefinition,
    *,
    watch: WatchDefinition | None,
    filter_tree: FilterNode | None,
) -> bool:
    """Evaluate one already-validated matcher against event-time truth."""

    if envelope.event_key != event.event_key:
        raise MatcherEvaluationError("event definition does not match envelope event_key")
    if watch is not None and envelope.source.operation == AutomationSourceOperation.UPDATE:
        changed = set(envelope.changes)
        watched = set(watch.paths)
        if watch.mode == "all" and not watched.issubset(changed):
            return False
        if watch.mode == "any" and changed.isdisjoint(watched):
            return False
    return filter_tree is None or _evaluate_node(envelope, filter_tree)


def evaluate_operator(
    field_type: FieldType,
    operator: str,
    actual: Any,
    expected: Any = _MISSING,
    *,
    exact: bool = False,
) -> bool:
    """Evaluate one operator after enforcing canonical actual/expected types."""

    if operator in {"is_empty", "not_empty"}:
        if expected is not _MISSING:
            raise ValueError(f"{operator} does not accept a value")
        empty = _is_typed_empty(field_type, actual, exact=exact)
        return empty if operator == "is_empty" else not empty
    if actual is None:
        return False

    normalized_actual = _normalize_scalar(field_type, actual, exact=exact)
    if operator == "between":
        if type(expected) is not list or len(expected) != 2:
            raise ValueError("between requires exactly two values")
        lower = _normalize_scalar(field_type, expected[0], exact=exact)
        upper = _normalize_scalar(field_type, expected[1], exact=exact)
        if lower > upper:
            raise ValueError("between lower value must not exceed upper value")
        return bool(lower <= normalized_actual <= upper)
    if operator in {"in", "not_in"}:
        if type(expected) is not list or not expected:
            raise ValueError(f"{operator} requires a non-empty list")
        normalized_expected = [
            _normalize_scalar(field_type, item, exact=exact) for item in expected
        ]
        contained = normalized_actual in normalized_expected
        return contained if operator == "in" else not contained
    if field_type == FieldType.LIST:
        return _evaluate_list_operator(operator, normalized_actual, expected, exact=exact)

    normalized_expected = _normalize_scalar(field_type, expected, exact=exact)
    if operator == "eq":
        return bool(normalized_actual == normalized_expected)
    if operator == "neq":
        return bool(normalized_actual != normalized_expected)
    if operator == "contains":
        return bool(normalized_expected in normalized_actual)
    if operator == "starts_with":
        return bool(normalized_actual.startswith(normalized_expected))
    if operator in {"gt", "after"}:
        return bool(normalized_actual > normalized_expected)
    if operator in {"gte", "on_or_after"}:
        return bool(normalized_actual >= normalized_expected)
    if operator in {"lt", "before"}:
        return bool(normalized_actual < normalized_expected)
    if operator in {"lte", "on_or_before"}:
        return bool(normalized_actual <= normalized_expected)
    raise ValueError(f"unsupported {field_type.value} operator: {operator}")


def filter_tree_to_dict(node: FilterNode | None) -> dict[str, Any] | None:
    """Serialize a validated tree into the canonical JSON storage shape."""

    if node is None:
        return None
    if isinstance(node, FilterLeaf):
        result = {"path": node.field.path, "operator": node.operator}
        if node.value is not _MISSING:
            result["value"] = deepcopy(node.value)
        return result
    return {node.mode: [filter_tree_to_dict(child) for child in node.children]}


def _merged_field_catalog(events: Iterable[EventDefinition]) -> dict[str, FieldDefinition]:
    fields: dict[str, FieldDefinition] = {}
    for event in events:
        for path, definition in event.fields.items():
            if not definition.operators:
                continue
            existing = fields.get(path)
            if existing is not None and (
                existing.value_type != definition.value_type
                or existing.accessor != definition.accessor
                or existing.operators != definition.operators
                or existing.exact != definition.exact
            ):
                raise MatcherValidationError(f"incompatible registry field path: {path}")
            fields.setdefault(path, definition)
    return fields


def _parse_filter_node(
    value: Mapping[str, Any],
    fields: Mapping[str, FieldDefinition],
    *,
    group_level: int,
) -> FilterNode:
    if not isinstance(value, Mapping):
        raise MatcherValidationError("filter nodes must be objects")
    group_keys = set(value) & {"all", "any"}
    if group_keys:
        if len(group_keys) != 1 or len(value) != 1:
            raise MatcherValidationError("filter group must contain exactly one of all or any")
        next_level = group_level + 1
        if next_level > MAX_FILTER_GROUP_LEVELS:
            raise MatcherValidationError(
                f"filter supports at most {MAX_FILTER_GROUP_LEVELS} group levels"
            )
        mode = next(iter(group_keys))
        raw_children = value[mode]
        if type(raw_children) is not list or not raw_children:
            raise MatcherValidationError("filter groups must contain at least one child")
        children = tuple(
            _parse_filter_node(child, fields, group_level=next_level) for child in raw_children
        )
        return FilterGroup(mode=mode, children=children)

    required_keys = {"path", "operator"}
    if not required_keys.issubset(value) or set(value) - {"path", "operator", "value"}:
        raise MatcherValidationError("filter leaf must contain path, operator, and optional value")
    path, operator = value["path"], value["operator"]
    if type(path) is not str or type(operator) is not str:
        raise MatcherValidationError("filter path and operator must be strings")
    try:
        definition = fields[path]
    except KeyError as exc:
        raise MatcherValidationError(f"unknown filter path: {path}") from exc
    if operator not in definition.operators:
        raise MatcherValidationError(f"operator {operator} is not allowed for {path}")

    has_value = "value" in value
    if operator in {"is_empty", "not_empty"}:
        if has_value:
            raise MatcherValidationError(f"{operator} must not include value")
        normalized = _MISSING
    else:
        if not has_value:
            raise MatcherValidationError(f"{operator} requires value")
        try:
            normalized = _normalize_expected(
                definition.value_type,
                operator,
                value["value"],
                exact=definition.exact,
            )
        except ValueError as exc:
            raise MatcherValidationError(f"invalid value for {path}: {exc}") from exc
    return FilterLeaf(field=definition, operator=operator, value=normalized)


def _normalize_expected(
    field_type: FieldType,
    operator: str,
    value: Any,
    *,
    exact: bool,
) -> Any:
    if operator == "between":
        if type(value) is not list or len(value) != 2:
            raise ValueError("between requires a two-value list")
        comparable = [_normalize_scalar(field_type, item, exact=exact) for item in value]
        if comparable[0] > comparable[1]:
            raise ValueError("between lower value must not exceed upper value")
        return [_json_safe_scalar(item) for item in comparable]
    if operator in {"in", "not_in"}:
        if type(value) is not list or not value:
            raise ValueError(f"{operator} requires a non-empty list")
        return [
            _json_safe_scalar(_normalize_scalar(field_type, item, exact=exact)) for item in value
        ]
    if field_type == FieldType.LIST:
        if operator == "contains":
            return _normalize_list_item(value, exact=exact)
        if operator in {"contains_any", "contains_all"}:
            if type(value) is not list or not value:
                raise ValueError(f"{operator} requires a non-empty list")
            return [_normalize_list_item(item, exact=exact) for item in value]
    return _json_safe_scalar(_normalize_scalar(field_type, value, exact=exact))


def _evaluate_node(envelope: AutomationEventEnvelope, node: FilterNode) -> bool:
    if isinstance(node, FilterGroup):
        if node.mode == "all":
            return all(_evaluate_node(envelope, child) for child in node.children)
        return any(_evaluate_node(envelope, child) for child in node.children)

    actual = _resolve_path(envelope, node.field.accessor)
    if actual is _MISSING:
        return False
    try:
        return evaluate_operator(
            node.field.value_type,
            node.operator,
            actual,
            node.value,
            exact=node.field.exact,
        )
    except (TypeError, ValueError) as exc:
        raise MatcherEvaluationError(
            f"event value at {node.field.path} violates its registry type"
        ) from exc


def _resolve_path(envelope: AutomationEventEnvelope, accessor: tuple[str, ...]) -> Any:
    current: Any = envelope
    for segment in accessor:
        if isinstance(current, BaseModel):
            if not hasattr(current, segment):
                raise MatcherEvaluationError(f"registry accessor is invalid at {segment}")
            current = getattr(current, segment)
        elif isinstance(current, Mapping):
            if segment not in current:
                return _MISSING
            current = current[segment]
        else:
            raise MatcherEvaluationError(f"event path has an invalid container at {segment}")
    return current.value if isinstance(current, Enum) else current


def _normalize_scalar(field_type: FieldType, value: Any, *, exact: bool) -> Any:
    if isinstance(value, Enum):
        value = value.value
    if field_type in {FieldType.TEXT, FieldType.URL, FieldType.EMAIL}:
        if type(value) is not str:
            raise ValueError("value must be a string")
        if field_type == FieldType.EMAIL:
            return value.lower()
        return value if exact else value.casefold()
    if field_type == FieldType.UUID:
        if type(value) is not str:
            raise ValueError("value must be a UUID string")
        try:
            return str(UUID(value))
        except ValueError as exc:
            raise ValueError("value must be a UUID string") from exc
    if field_type in {FieldType.RELATION, FieldType.ENUM}:
        if type(value) is not str or not value:
            raise ValueError("value must be a stable non-empty string")
        return value
    if field_type == FieldType.INTEGER:
        if type(value) is not int:
            raise ValueError("value must be an integer")
        return value
    if field_type == FieldType.DECIMAL:
        if type(value) is not str:
            raise ValueError("value must be a decimal string")
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("value must be a decimal string") from exc
        if not parsed.is_finite():
            raise ValueError("value must be a finite decimal string")
        return parsed
    if field_type == FieldType.BOOLEAN:
        if type(value) is not bool:
            raise ValueError("value must be a boolean")
        return value
    if field_type == FieldType.DATE:
        if type(value) is not str:
            raise ValueError("value must be an ISO date string")
        try:
            parsed_date = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("value must be an ISO date string") from exc
        if parsed_date.isoformat() != value:
            raise ValueError("value must use YYYY-MM-DD")
        return parsed_date
    if field_type == FieldType.DATETIME:
        if type(value) is not str:
            raise ValueError("value must be an RFC 3339 datetime string")
        try:
            parsed_datetime = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("value must be an RFC 3339 datetime string") from exc
        if parsed_datetime.utcoffset() is None:
            raise ValueError("datetime value must include an offset")
        return parsed_datetime.astimezone(UTC)
    if field_type == FieldType.LIST:
        if type(value) is not list:
            raise ValueError("value must be a list")
        return [_normalize_list_item(item, exact=exact) for item in value]
    raise ValueError(f"field type {field_type.value} is not filterable")


def _normalize_list_item(value: Any, *, exact: bool) -> Any:
    if type(value) is str:
        return value if exact else value.casefold()
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float:
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError("list values must be finite JSON scalars")
        return value
    raise ValueError("list values must be JSON scalars")


def _is_typed_empty(field_type: FieldType, value: Any, *, exact: bool) -> bool:
    if value is None or value == "":
        return True
    normalized = _normalize_scalar(field_type, value, exact=exact)
    return field_type == FieldType.LIST and not normalized


def _json_safe_scalar(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _evaluate_list_operator(
    operator: str,
    actual: Sequence[Any],
    expected: Any,
    *,
    exact: bool,
) -> bool:
    if operator == "contains":
        return _normalize_list_item(expected, exact=exact) in actual
    if operator in {"contains_any", "contains_all"}:
        if type(expected) is not list or not expected:
            raise ValueError(f"{operator} requires a non-empty list")
        normalized = [_normalize_list_item(item, exact=exact) for item in expected]
        if operator == "contains_any":
            return any(item in actual for item in normalized)
        return all(item in actual for item in normalized)
    raise ValueError(f"unsupported list operator: {operator}")


__all__ = [
    "MAX_FILTER_GROUP_LEVELS",
    "MAX_FILTER_LEAVES",
    "FilterGroup",
    "FilterLeaf",
    "FilterNode",
    "MatcherEvaluationError",
    "MatcherValidationError",
    "collect_option_selections",
    "combine_filter_trees",
    "evaluate_operator",
    "filter_tree_to_dict",
    "matches_binding",
    "validate_filter_tree",
    "validate_watch",
]
