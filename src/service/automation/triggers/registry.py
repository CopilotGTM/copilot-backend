"""Backend-owned automation event contracts and trigger definitions.

The registry is deliberately ordinary typed Python data. Producers, request
validation, catalog rendering, and activation all consult this same object so
an event path or preset cannot drift between those boundaries.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType
from typing import Any, Literal, TypeVar
from uuid import UUID

from src.service.automation.events.contracts import (
    AutomationEventEnvelope,
    AutomationRouteMode,
    AutomationSourceMode,
    AutomationSourceOperation,
)


class TriggerFamily(str, Enum):
    """Small set of invocation families exposed by Agent Studio."""

    EVENT = "event"
    MANUAL = "manual"
    SCHEDULE = "schedule"
    WEBHOOK = "webhook"


class FieldType(str, Enum):
    """Canonical value types understood by the deterministic matcher."""

    TEXT = "text"
    URL = "url"
    EMAIL = "email"
    UUID = "uuid"
    RELATION = "relation"
    ENUM = "enum"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    LIST = "list"
    OBJECT = "object"


FIELD_OPERATORS: Mapping[FieldType, tuple[str, ...]] = MappingProxyType(
    {
        FieldType.TEXT: (
            "eq",
            "neq",
            "in",
            "not_in",
            "contains",
            "starts_with",
            "is_empty",
            "not_empty",
        ),
        FieldType.URL: (
            "eq",
            "neq",
            "in",
            "not_in",
            "contains",
            "starts_with",
            "is_empty",
            "not_empty",
        ),
        FieldType.EMAIL: (
            "eq",
            "neq",
            "in",
            "not_in",
            "contains",
            "starts_with",
            "is_empty",
            "not_empty",
        ),
        FieldType.UUID: ("eq", "neq", "in", "not_in", "is_empty", "not_empty"),
        FieldType.RELATION: ("eq", "neq", "in", "not_in", "is_empty", "not_empty"),
        FieldType.ENUM: ("eq", "neq", "in", "not_in", "is_empty", "not_empty"),
        FieldType.INTEGER: (
            "eq",
            "neq",
            "gt",
            "gte",
            "lt",
            "lte",
            "between",
            "is_empty",
            "not_empty",
        ),
        FieldType.DECIMAL: (
            "eq",
            "neq",
            "gt",
            "gte",
            "lt",
            "lte",
            "between",
            "is_empty",
            "not_empty",
        ),
        FieldType.BOOLEAN: ("eq", "neq"),
        FieldType.DATE: (
            "eq",
            "before",
            "on_or_before",
            "after",
            "on_or_after",
            "between",
            "is_empty",
        ),
        FieldType.DATETIME: ("eq", "before", "after", "between", "is_empty"),
        FieldType.LIST: (
            "contains",
            "contains_any",
            "contains_all",
            "is_empty",
            "not_empty",
        ),
        FieldType.OBJECT: (),
    }
)

_NORMAL_SOURCE_MODES = frozenset(
    {
        AutomationSourceMode.INTERACTIVE,
        AutomationSourceMode.INCREMENTAL_SYNC,
        AutomationSourceMode.INITIAL_SYNC,
        AutomationSourceMode.BACKFILL,
        AutomationSourceMode.SYSTEM,
    }
)


@dataclass(frozen=True)
class RecordField:
    """One canonical field present in an event-time record snapshot."""

    key: str
    label: str
    value_type: FieldType
    description: str = ""
    required: bool = False
    exact: bool = False
    option_source: str | None = None
    filterable: bool = True
    accessor: tuple[str, ...] | None = None


@dataclass(frozen=True)
class FieldDefinition:
    """One opaque filter path and its explicit envelope accessor."""

    path: str
    label: str
    value_type: FieldType
    accessor: tuple[str, ...]
    description: str = ""
    operators: tuple[str, ...] = ()
    required: bool = False
    exact: bool = False
    option_source: str | None = None
    watchable: bool = False


@dataclass(frozen=True)
class ConfigFieldDefinition:
    """Frontend metadata and save-time contract for one trigger option."""

    key: str
    label: str
    value_type: FieldType
    input_type: str
    required: bool = False
    default: Any = None
    options: tuple[str, ...] = ()
    option_source: str | None = None
    depends_on: str | None = None


@dataclass(frozen=True)
class WatchDefinition:
    """Cheap update gate over canonical record field keys."""

    mode: Literal["any", "all"]
    paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.mode not in {"any", "all"}:
            raise ValueError("watch mode must be any or all")
        if not self.paths or len(self.paths) != len(set(self.paths)):
            raise ValueError("watch paths must be non-empty and unique")


@dataclass(frozen=True)
class CompiledTrigger:
    """Server-derived preset configuration consumed by binding validation."""

    config: dict[str, Any]
    watch: WatchDefinition | None = None
    filter: dict[str, Any] | None = None


PresetCompiler = Callable[[Mapping[str, Any]], CompiledTrigger]
_DefinitionT = TypeVar("_DefinitionT")


@dataclass(frozen=True)
class EventDefinition:
    """Producer-owned schema for one durable event fact."""

    event_key: str
    route_mode: AutomationRouteMode
    operation: AutomationSourceOperation
    subject_type: str | None
    subject_required: bool = True
    record_fields: tuple[RecordField, ...] = ()
    attribute_fields: tuple[RecordField, ...] = ()
    reference_fields: tuple[RecordField, ...] = ()
    allowed_source_modes: frozenset[AutomationSourceMode] = _NORMAL_SOURCE_MODES
    producer_available: bool = True
    allow_dynamic_record: bool = False
    supports_subject_participants: bool = False

    def __post_init__(self) -> None:
        keys = [item.key for item in self.record_fields]
        if len(keys) != len(set(keys)):
            raise ValueError(f"duplicate record field in {self.event_key}")
        if self.operation == AutomationSourceOperation.UPDATE and not self.record_fields:
            raise ValueError(f"updated event {self.event_key} requires record fields")

    @property
    def fields(self) -> Mapping[str, FieldDefinition]:
        """Return the exact filter-path catalog for this event."""

        definitions: dict[str, FieldDefinition] = {}
        for item in self.record_fields:
            definitions[f"record.{item.key}"] = _path_field("record", item)
            if self.operation == AutomationSourceOperation.UPDATE and item.filterable:
                definitions[f"changes.{item.key}.before"] = _change_field(item, "before")
                definitions[f"changes.{item.key}.after"] = _change_field(item, "after")
        for item in self.attribute_fields:
            definitions[f"attributes.{item.key}"] = _path_field("attributes", item)
        for item in self.reference_fields:
            definitions[f"references.{item.key}"] = _path_field("references", item)
        definitions.update(_COMMON_EVENT_FIELDS)
        return MappingProxyType(definitions)

    @property
    def watch_fields(self) -> Mapping[str, FieldDefinition]:
        """Return record keys that may gate this update event."""

        if self.operation != AutomationSourceOperation.UPDATE:
            return MappingProxyType({})
        return MappingProxyType(
            {
                item.key: FieldDefinition(
                    path=item.key,
                    label=item.label,
                    value_type=item.value_type,
                    accessor=("changes", item.key),
                    description=item.description,
                    operators=(),
                    required=item.required,
                    exact=item.exact,
                    option_source=item.option_source,
                    watchable=True,
                )
                for item in self.record_fields
                if item.filterable
            }
        )

    def validate_envelope(self, envelope: AutomationEventEnvelope) -> None:
        """Reject producer drift before a dispatch is persisted or activated."""

        if envelope.event_key != self.event_key:
            raise ValueError("envelope event_key does not match registry definition")
        if envelope.route.mode != self.route_mode:
            raise ValueError("envelope route does not match registry definition")
        if envelope.source.domain != self.event_key.split(".", 1)[0]:
            raise ValueError("envelope source domain does not match event key")
        if envelope.source.operation != self.operation:
            raise ValueError("envelope operation does not match registry definition")
        if envelope.source.mode not in self.allowed_source_modes:
            raise ValueError("envelope source mode is not allowed")
        if self.subject_required and envelope.subject.type != self.subject_type:
            raise ValueError("envelope subject type does not match registry definition")
        if (
            not self.subject_required
            and self.subject_type is not None
            and envelope.subject.type not in {None, self.subject_type}
        ):
            raise ValueError("envelope subject type does not match registry definition")

        record_keys = {(item.accessor or (item.key,))[0] for item in self.record_fields}
        if not self.allow_dynamic_record:
            unknown_record = set(envelope.record) - record_keys
            if unknown_record:
                raise ValueError(f"unknown event record fields: {sorted(unknown_record)}")
        missing_record = {
            item.key
            for item in self.record_fields
            if item.required
            and not _mapping_path_exists(envelope.record, item.accessor or (item.key,))
        }
        if missing_record:
            raise ValueError(f"missing event record fields: {sorted(missing_record)}")
        for item in self.record_fields:
            found, value = _mapping_path_value(
                envelope.record,
                item.accessor or (item.key,),
            )
            if found:
                _validate_event_value(
                    f"record.{item.key}",
                    item,
                    value,
                    allow_null=not item.required,
                )

        record_id = envelope.record.get("id")
        if envelope.subject.type is not None and record_id != envelope.subject.id:
            raise ValueError("envelope subject id does not match record id")

        if self.operation in {AutomationSourceOperation.CREATE, AutomationSourceOperation.DELETE}:
            if envelope.changes:
                raise ValueError("created and deleted events must not contain changes")
        elif self.operation == AutomationSourceOperation.UPDATE and not envelope.changes:
            raise ValueError("updated events must contain at least one changed field")
        unknown_changes = set(envelope.changes) - set(self.watch_fields)
        if unknown_changes and not self.allow_dynamic_record:
            raise ValueError(f"unknown event change fields: {sorted(unknown_changes)}")
        record_fields = {item.key: item for item in self.record_fields}
        for key, change in envelope.changes.items():
            record_field = record_fields.get(key)
            if record_field is None:
                continue
            _validate_event_value(
                f"changes.{key}.before",
                record_field,
                change.before,
                allow_null=True,
            )
            _validate_event_value(
                f"changes.{key}.after",
                record_field,
                change.after,
                allow_null=True,
            )

        _validate_section_keys("attributes", envelope.attributes, self.attribute_fields)
        _validate_section_keys("references", envelope.references, self.reference_fields)


@dataclass(frozen=True)
class TriggerDefinition:
    """User-selectable intent compiled onto one or more event definitions."""

    trigger_key: str
    family: TriggerFamily
    category_key: str
    category_label: str
    resource_key: str
    resource_label: str
    event_label: str
    description: str
    match_event_keys: tuple[str, ...]
    subject_type: str | None
    subject_required: bool = True
    fixed_watch: WatchDefinition | None = None
    fixed_filter: dict[str, Any] | None = None
    allow_user_watch: bool = False
    config_fields: tuple[ConfigFieldDefinition, ...] = ()
    allowed_principal_modes: tuple[str, ...] = ("personal", "organisation")
    allowed_scope_types: tuple[str, ...] = (
        "owned_or_created_by_principal",
        "organisation",
    )
    allowed_audience_modes: tuple[str, ...] = (
        "principal",
        "none",
        "explicit_users",
        "subject_participants",
    )
    output_schema: dict[str, Any] | None = None
    producer_available: bool = True
    preset_compiler: PresetCompiler | None = field(default=None, compare=False, repr=False)

    def compile(self, config: Mapping[str, Any]) -> CompiledTrigger:
        """Validate preset options and derive its non-overridable matcher."""

        if self.preset_compiler is not None:
            return self.preset_compiler(config)
        if config:
            allowed = {item.key for item in self.config_fields}
            unknown = set(config) - allowed
            if unknown:
                raise ValueError(f"unknown trigger config fields: {sorted(unknown)}")
        normalized = dict(config)
        for item in self.config_fields:
            if item.required and item.key not in normalized:
                raise ValueError(f"{item.key} is required")
            if item.key not in normalized and item.default is not None:
                normalized[item.key] = item.default
        return CompiledTrigger(
            config=normalized,
            watch=self.fixed_watch,
            filter=self.fixed_filter,
        )


class AutomationRegistry:
    """Validated immutable lookup maps shared across automation boundaries."""

    def __init__(
        self,
        events: Iterable[EventDefinition],
        triggers: Iterable[TriggerDefinition],
    ) -> None:
        self.events = MappingProxyType(_unique_map("event_key", events))
        self.triggers = MappingProxyType(_unique_map("trigger_key", triggers))
        self._validate_references()

    def event(self, event_key: str) -> EventDefinition:
        """Return the registered event definition for the requested key."""
        try:
            return self.events[event_key]
        except KeyError as exc:
            raise ValueError(f"unknown automation event: {event_key}") from exc

    def trigger(self, trigger_key: str) -> TriggerDefinition:
        """Return the registered trigger definition for the requested key."""
        try:
            return self.triggers[trigger_key]
        except KeyError as exc:
            raise ValueError(f"unknown automation trigger: {trigger_key}") from exc

    def catalog_triggers(self) -> tuple[TriggerDefinition, ...]:
        """Apply the catalog triggers contract for this organisation-scoped module."""
        return tuple(
            item
            for item in self.triggers.values()
            if item.producer_available
            and all(self.events[key].producer_available for key in item.match_event_keys)
        )

    def _validate_references(self) -> None:
        for trigger in self.triggers.values():
            unknown = set(trigger.match_event_keys) - set(self.events)
            if unknown:
                raise ValueError(
                    f"trigger {trigger.trigger_key} references unknown event: {sorted(unknown)}"
                )
            expected_route = (
                AutomationRouteMode.MATCH
                if trigger.family == TriggerFamily.EVENT
                else AutomationRouteMode.DIRECT
            )
            if any(
                self.events[key].route_mode != expected_route for key in trigger.match_event_keys
            ):
                family_label = (
                    "event family"
                    if expected_route == AutomationRouteMode.MATCH
                    else "direct family"
                )
                raise ValueError(
                    f"{family_label} {trigger.trigger_key} references the wrong route mode"
                )
            if not trigger.match_event_keys:
                raise ValueError(f"trigger {trigger.trigger_key} has no event keys")
            subject_types = {self.events[key].subject_type for key in trigger.match_event_keys}
            if len(subject_types) != 1 or trigger.subject_type not in subject_types:
                raise ValueError(f"trigger {trigger.trigger_key} subject does not match its events")


def _unique_map(
    attribute: str,
    values: Iterable[_DefinitionT],
) -> dict[str, _DefinitionT]:
    result: dict[str, _DefinitionT] = {}
    for value in values:
        key = getattr(value, attribute)
        if key in result:
            raise ValueError(f"duplicate {attribute}: {key}")
        result[key] = value
    return result


def _path_field(section: str, item: RecordField) -> FieldDefinition:
    operators = FIELD_OPERATORS[item.value_type] if item.filterable else ()
    return FieldDefinition(
        path=f"{section}.{item.key}",
        label=item.label,
        value_type=item.value_type,
        accessor=(section, *(item.accessor or (item.key,))),
        description=item.description,
        operators=operators,
        required=item.required,
        exact=item.exact,
        option_source=item.option_source,
    )


def _change_field(item: RecordField, side: str) -> FieldDefinition:
    return FieldDefinition(
        path=f"changes.{item.key}.{side}",
        label=f"{item.label} {side}",
        value_type=item.value_type,
        accessor=("changes", item.key, side),
        description=item.description,
        operators=FIELD_OPERATORS[item.value_type],
        required=False,
        exact=item.exact,
        option_source=item.option_source,
    )


def _validate_section_keys(
    section: str,
    values: Mapping[str, Any],
    definitions: tuple[RecordField, ...],
) -> None:
    known = {item.key for item in definitions}
    unknown = set(values) - known
    if unknown:
        raise ValueError(f"unknown event {section} fields: {sorted(unknown)}")
    missing = {item.key for item in definitions if item.required and item.key not in values}
    if missing:
        raise ValueError(f"missing event {section} fields: {sorted(missing)}")
    for item in definitions:
        if item.key in values:
            _validate_event_value(
                f"{section}.{item.key}",
                item,
                values[item.key],
                allow_null=not item.required,
            )


def _mapping_path_exists(values: Mapping[str, Any], accessor: tuple[str, ...]) -> bool:
    current: Any = values
    for segment in accessor:
        if not isinstance(current, Mapping) or segment not in current:
            return False
        current = current[segment]
    return True


def _mapping_path_value(
    values: Mapping[str, Any],
    accessor: tuple[str, ...],
) -> tuple[bool, Any]:
    current: Any = values
    for segment in accessor:
        if not isinstance(current, Mapping) or segment not in current:
            return False, None
        current = current[segment]
    return True, current


def _validate_event_value(
    path: str,
    field: RecordField,
    value: Any,
    *,
    allow_null: bool,
) -> None:
    if value is None:
        if allow_null:
            return
        raise ValueError(f"{path} must not be null")
    value_type = field.value_type
    if value_type in {FieldType.TEXT, FieldType.URL, FieldType.EMAIL}:
        valid = type(value) is str
    elif value_type == FieldType.UUID:
        try:
            valid = type(value) is str and str(UUID(value)) == value.lower()
        except ValueError:
            valid = False
    elif value_type in {FieldType.RELATION, FieldType.ENUM}:
        valid = type(value) is str and bool(value)
    elif value_type == FieldType.INTEGER:
        valid = type(value) is int
    elif value_type == FieldType.DECIMAL:
        if type(value) is not str:
            valid = False
        else:
            try:
                valid = Decimal(value).is_finite()
            except InvalidOperation:
                valid = False
    elif value_type == FieldType.BOOLEAN:
        valid = type(value) is bool
    elif value_type == FieldType.DATE:
        try:
            valid = type(value) is str and date.fromisoformat(value).isoformat() == value
        except ValueError:
            valid = False
    elif value_type == FieldType.DATETIME:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            valid = type(value) is str and parsed.utcoffset() is not None
        except (AttributeError, ValueError):
            valid = False
    elif value_type == FieldType.LIST:
        valid = type(value) is list
    elif value_type == FieldType.OBJECT:
        valid = type(value) is dict
    else:
        valid = False
    if not valid:
        raise ValueError(f"{path} violates its {value_type.value} field contract")


_COMMON_EVENT_FIELDS: Mapping[str, FieldDefinition] = MappingProxyType(
    {
        "actor.type": FieldDefinition(
            path="actor.type",
            label="Actor type",
            value_type=FieldType.ENUM,
            accessor=("actor", "type"),
            operators=FIELD_OPERATORS[FieldType.ENUM],
            required=True,
        ),
        "actor.id": FieldDefinition(
            path="actor.id",
            label="Actor ID",
            value_type=FieldType.TEXT,
            accessor=("actor", "id"),
            operators=FIELD_OPERATORS[FieldType.TEXT],
        ),
        "source.domain": FieldDefinition(
            path="source.domain",
            label="Source domain",
            value_type=FieldType.ENUM,
            accessor=("source", "domain"),
            operators=FIELD_OPERATORS[FieldType.ENUM],
            required=True,
            exact=True,
        ),
        "source.system": FieldDefinition(
            path="source.system",
            label="Source system",
            value_type=FieldType.TEXT,
            accessor=("source", "system"),
            operators=FIELD_OPERATORS[FieldType.TEXT],
            required=True,
            exact=True,
        ),
        "source.operation": FieldDefinition(
            path="source.operation",
            label="Source operation",
            value_type=FieldType.ENUM,
            accessor=("source", "operation"),
            operators=FIELD_OPERATORS[FieldType.ENUM],
            required=True,
            exact=True,
        ),
        "source.mode": FieldDefinition(
            path="source.mode",
            label="Source mode",
            value_type=FieldType.ENUM,
            accessor=("source", "mode"),
            operators=FIELD_OPERATORS[FieldType.ENUM],
            required=True,
            exact=True,
        ),
        "source.source_event_id": FieldDefinition(
            path="source.source_event_id",
            label="Source event ID",
            value_type=FieldType.TEXT,
            accessor=("source", "source_event_id"),
            operators=FIELD_OPERATORS[FieldType.TEXT],
            required=True,
            exact=True,
        ),
    }
)


def _id_field(label: str = "ID") -> RecordField:
    return RecordField("id", label, FieldType.UUID, required=True, exact=True)


_DEAL_FIELDS = (
    _id_field("Deal ID"),
    RecordField("name", "Deal name", FieldType.TEXT),
    RecordField(
        "pipeline_id",
        "Pipeline",
        FieldType.RELATION,
        exact=True,
        option_source="pipelines",
    ),
    RecordField(
        "stage_id",
        "Stage",
        FieldType.RELATION,
        exact=True,
        option_source="pipeline_stages",
    ),
    RecordField(
        "deal_status",
        "Deal status",
        FieldType.ENUM,
        required=True,
        exact=True,
        option_source="deal_statuses",
    ),
    RecordField("amount", "Amount", FieldType.DECIMAL),
    RecordField("currency", "Currency", FieldType.ENUM, exact=True),
    RecordField("owner_id", "Owner", FieldType.RELATION, exact=True, option_source="crm_users"),
    RecordField(
        "created_by_user_id",
        "Created by",
        FieldType.RELATION,
        exact=True,
        option_source="crm_users",
    ),
    RecordField("expected_close_date", "Expected close date", FieldType.DATETIME),
    RecordField("account_id", "Account", FieldType.RELATION, exact=True),
    RecordField("primary_contact_id", "Primary contact", FieldType.RELATION, exact=True),
    RecordField(
        "custom_properties",
        "Custom properties",
        FieldType.OBJECT,
        filterable=False,
    ),
)

_CONTACT_FIELDS = (
    _id_field("Contact ID"),
    RecordField("first_name", "First name", FieldType.TEXT),
    RecordField("last_name", "Last name", FieldType.TEXT),
    RecordField("email", "Email", FieldType.EMAIL),
    RecordField("phone", "Phone", FieldType.TEXT),
    RecordField("owner_id", "Owner", FieldType.RELATION, exact=True, option_source="crm_users"),
    RecordField("account_id", "Account", FieldType.RELATION, exact=True),
    RecordField("lifecycle_stage", "Lifecycle", FieldType.ENUM, exact=True),
)

_ACCOUNT_FIELDS = (
    _id_field("Account ID"),
    RecordField("name", "Account name", FieldType.TEXT),
    RecordField("domain", "Domain", FieldType.TEXT),
    RecordField("owner_id", "Owner", FieldType.RELATION, exact=True, option_source="crm_users"),
    RecordField("lifecycle_stage", "Lifecycle", FieldType.ENUM, exact=True),
    RecordField("tier", "Tier", FieldType.ENUM, exact=True),
    RecordField("annual_revenue", "Annual revenue", FieldType.DECIMAL),
)

_LEAD_FIELDS = (
    _id_field("Lead ID"),
    RecordField("first_name", "First name", FieldType.TEXT),
    RecordField("last_name", "Last name", FieldType.TEXT),
    RecordField("email", "Email", FieldType.EMAIL),
    RecordField("phone", "Phone", FieldType.TEXT),
    RecordField("company", "Company", FieldType.TEXT),
    RecordField("status", "Status", FieldType.ENUM, exact=True),
    RecordField("source", "Source", FieldType.ENUM, exact=True),
    RecordField("score", "Score", FieldType.DECIMAL),
    RecordField("owner_id", "Owner", FieldType.RELATION, exact=True, option_source="crm_users"),
    RecordField("created_by_user_id", "Created by", FieldType.RELATION, exact=True),
)

_TASK_FIELDS = (
    _id_field("Task ID"),
    RecordField("title", "Title", FieldType.TEXT, required=True),
    RecordField("status", "Status", FieldType.ENUM, required=True, exact=True),
    RecordField("due_at", "Due at", FieldType.DATETIME),
    RecordField("due_date", "Due date", FieldType.DATE),
    RecordField("assignee_id", "Assignee", FieldType.RELATION, exact=True),
    RecordField("owner_id", "Owner", FieldType.RELATION, exact=True),
    RecordField("created_by_user_id", "Created by", FieldType.RELATION, exact=True),
    RecordField("deal_id", "Deal", FieldType.RELATION, exact=True),
)

_NOTE_FIELDS = (
    _id_field("Note ID"),
    RecordField("created_by_user_id", "Created by", FieldType.RELATION, exact=True),
    RecordField("deal_id", "Deal", FieldType.RELATION, exact=True),
    RecordField("contact_id", "Contact", FieldType.RELATION, exact=True),
    RecordField("account_id", "Account", FieldType.RELATION, exact=True),
)

_EMAIL_FIELDS = (
    _id_field("Email ID"),
    RecordField("direction", "Direction", FieldType.ENUM, required=True, exact=True),
    RecordField("sender_email", "Sender", FieldType.EMAIL),
    RecordField("sender_domain", "Sender domain", FieldType.TEXT),
    RecordField("recipient_emails", "Recipients", FieldType.LIST),
    RecordField("has_attachments", "Has attachments", FieldType.BOOLEAN, required=True),
    RecordField("thread_id", "Thread", FieldType.TEXT, exact=True),
    RecordField("owner_id", "Mailbox owner", FieldType.RELATION, exact=True),
    RecordField("deal_id", "Related deal", FieldType.RELATION, exact=True),
)

_MEETING_FIELDS = (
    _id_field("Meeting ID"),
    RecordField("provider", "Provider", FieldType.ENUM, exact=True),
    RecordField("language", "Language", FieldType.ENUM, exact=True),
    RecordField("owner_id", "Owner", FieldType.RELATION, exact=True),
    RecordField("team_ids", "Teams", FieldType.LIST),
    RecordField("attendee_ids", "Attendees", FieldType.LIST),
    RecordField("deal_id", "Related deal", FieldType.RELATION, exact=True),
)

_SIGNAL_FIELDS = (
    _id_field("Subject ID"),
    RecordField("signal_id", "Signal ID", FieldType.UUID, required=True, exact=True),
    RecordField("signal_type", "Signal type", FieldType.ENUM, required=True, exact=True),
    RecordField("status", "Status", FieldType.ENUM, required=True, exact=True),
    RecordField("severity", "Severity", FieldType.ENUM, required=True, exact=True),
    RecordField("confidence", "Confidence", FieldType.DECIMAL, required=True),
    RecordField("deal_id", "Deal", FieldType.RELATION, required=True, exact=True),
    RecordField("contact_id", "Buyer", FieldType.RELATION, exact=True),
)

_RUN_FIELDS = (
    _id_field("Run ID"),
    RecordField("agent_id", "Agent", FieldType.RELATION, required=True, exact=True),
    RecordField("trigger_id", "Trigger", FieldType.RELATION, required=True, exact=True),
    RecordField("status", "Outcome", FieldType.ENUM, required=True, exact=True),
    RecordField("event_key", "Causing event", FieldType.ENUM, required=True, exact=True),
    RecordField("created_by_user_id", "Created by", FieldType.RELATION, required=True, exact=True),
)


def _record_events(
    domain: str,
    resource: str,
    fields: tuple[RecordField, ...],
    *,
    available: bool = True,
) -> list[EventDefinition]:
    return [
        EventDefinition(
            event_key=f"{domain}.{resource}.created",
            route_mode=AutomationRouteMode.MATCH,
            operation=AutomationSourceOperation.CREATE,
            subject_type=resource,
            record_fields=fields,
            producer_available=available,
            allow_dynamic_record=domain == "crm",
            supports_subject_participants=False,
        ),
        EventDefinition(
            event_key=f"{domain}.{resource}.updated",
            route_mode=AutomationRouteMode.MATCH,
            operation=AutomationSourceOperation.UPDATE,
            subject_type=resource,
            record_fields=fields,
            producer_available=available,
            allow_dynamic_record=domain == "crm",
            supports_subject_participants=False,
        ),
        EventDefinition(
            event_key=f"{domain}.{resource}.deleted",
            route_mode=AutomationRouteMode.MATCH,
            operation=AutomationSourceOperation.DELETE,
            subject_type=resource,
            record_fields=fields,
            producer_available=available,
            allow_dynamic_record=domain == "crm",
            supports_subject_participants=False,
        ),
    ]


def _signal_events(
    prefix: str,
    subject_type: str,
    *,
    available: bool,
) -> list[EventDefinition]:
    return [
        EventDefinition(
            event_key=f"{prefix}.{transition}",
            route_mode=AutomationRouteMode.MATCH,
            operation=AutomationSourceOperation.SIGNAL,
            subject_type=subject_type,
            record_fields=_SIGNAL_FIELDS,
            reference_fields=(
                RecordField("evidence_id", "Evidence", FieldType.TEXT, required=True, exact=True),
                RecordField("model_name", "Model", FieldType.TEXT, required=True, exact=True),
                RecordField("prompt_version", "Prompt version", FieldType.TEXT, required=True),
                RecordField(
                    "inference_run_id",
                    "Inference run",
                    FieldType.UUID,
                    required=True,
                    exact=True,
                ),
            ),
            producer_available=available,
            supports_subject_participants=False,
        )
        for transition in ("detected", "changed", "resolved")
    ]


def _events() -> list[EventDefinition]:
    events = [
        EventDefinition(
            event_key="system.manual.invoked",
            route_mode=AutomationRouteMode.DIRECT,
            operation=AutomationSourceOperation.INVOKE,
            subject_type=None,
            subject_required=False,
            allow_dynamic_record=True,
        ),
        EventDefinition(
            event_key="time.schedule.due",
            route_mode=AutomationRouteMode.DIRECT,
            operation=AutomationSourceOperation.SCHEDULE,
            subject_type=None,
            subject_required=False,
            allow_dynamic_record=True,
            attribute_fields=(
                RecordField("scheduled_for", "Scheduled for", FieldType.DATETIME, required=True),
                RecordField("timezone", "Timezone", FieldType.TEXT, required=True, exact=True),
            ),
        ),
        EventDefinition(
            event_key="integration.webhook.received",
            route_mode=AutomationRouteMode.DIRECT,
            operation=AutomationSourceOperation.WEBHOOK,
            subject_type=None,
            subject_required=False,
            allow_dynamic_record=True,
            attribute_fields=(
                RecordField("dedupe_key", "Dedupe key", FieldType.TEXT, required=True, exact=True),
            ),
        ),
    ]
    events.extend(_record_events("crm", "deal", _DEAL_FIELDS))
    events.extend(_record_events("crm", "contact", _CONTACT_FIELDS))
    events.extend(_record_events("crm", "account", _ACCOUNT_FIELDS))
    events.extend(_record_events("crm", "lead", _LEAD_FIELDS, available=False))
    events.extend(_record_events("crm", "task", _TASK_FIELDS, available=False))
    events.extend(_record_events("crm", "note", _NOTE_FIELDS, available=False))
    events.extend(
        [
            EventDefinition(
                event_key="crm.lead.converted",
                route_mode=AutomationRouteMode.MATCH,
                operation=AutomationSourceOperation.SIGNAL,
                subject_type="lead",
                record_fields=_LEAD_FIELDS,
                reference_fields=(
                    RecordField("contact_id", "Converted contact", FieldType.RELATION, exact=True),
                    RecordField("deal_id", "Converted deal", FieldType.RELATION, exact=True),
                ),
                producer_available=False,
            ),
            EventDefinition(
                event_key="crm.task.overdue",
                route_mode=AutomationRouteMode.MATCH,
                operation=AutomationSourceOperation.SIGNAL,
                subject_type="task",
                record_fields=_TASK_FIELDS,
                producer_available=False,
            ),
            EventDefinition(
                event_key="crm.task.due_soon",
                route_mode=AutomationRouteMode.MATCH,
                operation=AutomationSourceOperation.SIGNAL,
                subject_type="task",
                record_fields=_TASK_FIELDS,
                producer_available=False,
            ),
        ]
    )
    for event_key, available in (
        ("email.message.received", True),
        ("email.message.sent", True),
        ("email.message.replied", False),
    ):
        events.append(
            EventDefinition(
                event_key=event_key,
                route_mode=AutomationRouteMode.MATCH,
                operation=AutomationSourceOperation.SIGNAL,
                subject_type="email",
                record_fields=_EMAIL_FIELDS,
                attribute_fields=(
                    RecordField(
                        "direction", "Direction", FieldType.ENUM, required=True, exact=True
                    ),
                ),
                reference_fields=(
                    RecordField("email_id", "Email content", FieldType.UUID, required=True),
                ),
                producer_available=available,
                supports_subject_participants=available,
            )
        )
    for name, available in (
        ("scheduled", False),
        ("rescheduled", False),
        ("cancelled", False),
        ("ended", False),
        ("transcript.ready", True),
        ("summary.ready", True),
    ):
        events.append(
            EventDefinition(
                event_key=f"meeting.{name}",
                route_mode=AutomationRouteMode.MATCH,
                operation=AutomationSourceOperation.SIGNAL,
                subject_type="meeting",
                record_fields=_MEETING_FIELDS,
                reference_fields=(
                    RecordField("meeting_id", "Meeting", FieldType.UUID, required=True),
                ),
                producer_available=available,
                supports_subject_participants=available,
            )
        )
    for direction in ("received", "sent"):
        events.append(
            EventDefinition(
                event_key=f"conversation.message.{direction}",
                route_mode=AutomationRouteMode.MATCH,
                operation=AutomationSourceOperation.SIGNAL,
                subject_type="message",
                producer_available=False,
            )
        )
    events.extend(_signal_events("ai.deal_risk", "deal", available=True))
    events.extend(_signal_events("ai.deal_blocker", "deal", available=True))
    events.extend(_signal_events("ai.buyer_signal", "contact", available=False))
    for outcome in ("completed", "failed", "cancelled"):
        events.append(
            EventDefinition(
                event_key=f"agent.run.{outcome}",
                route_mode=AutomationRouteMode.MATCH,
                operation=AutomationSourceOperation.SIGNAL,
                subject_type="agent_run",
                record_fields=_RUN_FIELDS,
                producer_available=outcome != "cancelled",
            )
        )
    for outcome in ("requested", "granted", "rejected", "expired"):
        events.append(
            EventDefinition(
                event_key=f"agent.approval.{outcome}",
                route_mode=AutomationRouteMode.MATCH,
                operation=AutomationSourceOperation.SIGNAL,
                subject_type="approval",
                producer_available=False,
            )
        )
    return events


def _empty_compiler(
    *,
    watch: WatchDefinition | None = None,
    filter_tree: dict[str, Any] | None = None,
) -> PresetCompiler:
    def compile_empty(config: Mapping[str, Any]) -> CompiledTrigger:
        """Compile empty into the runtime representation consumed by activation."""
        if config:
            raise ValueError("this trigger does not accept config")
        return CompiledTrigger(config={}, watch=watch, filter=filter_tree)

    return compile_empty


def _changed_compiler(path: str) -> PresetCompiler:
    return _empty_compiler(watch=WatchDefinition("any", (path,)))


def _stage_compiler(side: str) -> PresetCompiler:
    def compile_stage(config: Mapping[str, Any]) -> CompiledTrigger:
        """Compile stage into the runtime representation consumed by activation."""
        if set(config) != {"stage_id"} or not isinstance(config.get("stage_id"), str):
            raise ValueError("stage_id is required")
        stage_id = config["stage_id"].strip()
        if not stage_id:
            raise ValueError("stage_id is required")
        return CompiledTrigger(
            config={"stage_id": stage_id},
            watch=WatchDefinition("any", ("stage_id",)),
            filter={
                "all": [
                    {
                        "path": f"changes.stage_id.{side}",
                        "operator": "eq",
                        "value": stage_id,
                    }
                ]
            },
        )

    return compile_stage


def _status_compiler(status: str) -> PresetCompiler:
    return _empty_compiler(
        watch=WatchDefinition("any", ("deal_status",)),
        filter_tree={
            "all": [
                {
                    "path": "changes.deal_status.after",
                    "operator": "eq",
                    "value": status,
                }
            ]
        },
    )


def _amount_crossed_compiler(config: Mapping[str, Any]) -> CompiledTrigger:
    if set(config) != {"direction", "threshold"}:
        raise ValueError("direction and threshold are required")
    direction = config["direction"]
    threshold = config["threshold"]
    if direction not in {"up", "down"} or type(threshold) is not str:
        raise ValueError("invalid amount crossing config")
    try:
        parsed = Decimal(threshold)
    except InvalidOperation as exc:
        raise ValueError("threshold must be a decimal string") from exc
    if not parsed.is_finite():
        raise ValueError("threshold must be a finite decimal string")
    normalized = format(parsed, "f")
    before_operator, after_operator = ("lt", "gte") if direction == "up" else ("gt", "lte")
    return CompiledTrigger(
        config={"direction": direction, "threshold": normalized},
        watch=WatchDefinition("any", ("amount",)),
        filter={
            "all": [
                {
                    "path": "changes.amount.before",
                    "operator": before_operator,
                    "value": normalized,
                },
                {
                    "path": "changes.amount.after",
                    "operator": after_operator,
                    "value": normalized,
                },
            ]
        },
    )


def _event_trigger(
    event_key: str,
    *,
    category_key: str,
    category_label: str,
    resource_key: str,
    resource_label: str,
    event_label: str,
    description: str,
    subject_type: str | None = None,
    trigger_key: str | None = None,
    producer_available: bool = True,
    compiler: PresetCompiler | None = None,
    config_fields: tuple[ConfigFieldDefinition, ...] = (),
    fixed_watch: WatchDefinition | None = None,
    fixed_filter: dict[str, Any] | None = None,
) -> TriggerDefinition:
    return TriggerDefinition(
        trigger_key=trigger_key or event_key,
        family=TriggerFamily.EVENT,
        category_key=category_key,
        category_label=category_label,
        resource_key=resource_key,
        resource_label=resource_label,
        event_label=event_label,
        description=description,
        match_event_keys=(event_key,),
        subject_type=subject_type or resource_key,
        fixed_watch=fixed_watch,
        fixed_filter=fixed_filter,
        config_fields=config_fields,
        producer_available=producer_available,
        preset_compiler=compiler or _empty_compiler(),
    )


def _record_triggers(
    resource: str,
    resource_label: str,
    *,
    available: bool = True,
) -> list[TriggerDefinition]:
    base = f"crm.{resource}"
    triggers = [
        _event_trigger(
            f"{base}.{operation}",
            category_key="crm",
            category_label="CRM",
            resource_key=resource,
            resource_label=resource_label,
            event_label=operation.title(),
            description=f"Run when a {resource_label.lower()} is {operation}.",
            producer_available=available,
        )
        for operation in ("created", "updated", "deleted")
    ]
    triggers[1] = replace(triggers[1], allow_user_watch=True)
    triggers.append(
        TriggerDefinition(
            trigger_key=f"{base}.created_or_updated",
            family=TriggerFamily.EVENT,
            category_key="crm",
            category_label="CRM",
            resource_key=resource,
            resource_label=resource_label,
            event_label="Created or updated",
            description=f"Run when a {resource_label.lower()} is created or updated.",
            match_event_keys=(f"{base}.created", f"{base}.updated"),
            subject_type=resource,
            allow_user_watch=True,
            producer_available=available,
            preset_compiler=_empty_compiler(),
        )
    )
    return triggers


def _field_change_trigger(
    resource: str,
    resource_label: str,
    field_key: str,
    field_label: str,
) -> TriggerDefinition:
    return _event_trigger(
        f"crm.{resource}.updated",
        trigger_key=f"crm.{resource}.{field_key}_changed",
        category_key="crm",
        category_label="CRM",
        resource_key=resource,
        resource_label=resource_label,
        event_label=f"{field_label} changed",
        description=f"Run when {field_label.lower()} changes.",
        compiler=_changed_compiler(field_key),
        fixed_watch=WatchDefinition("any", (field_key,)),
    )


def _triggers() -> list[TriggerDefinition]:
    triggers = [
        TriggerDefinition(
            trigger_key="system.manual.invoked",
            family=TriggerFamily.MANUAL,
            category_key="system",
            category_label="System",
            resource_key="manual",
            resource_label="Manual",
            event_label="Manual invocation",
            description="Run globally or once per selected record.",
            match_event_keys=("system.manual.invoked",),
            subject_type=None,
            subject_required=False,
        ),
        TriggerDefinition(
            trigger_key="time.schedule.due",
            family=TriggerFamily.SCHEDULE,
            category_key="time",
            category_label="Time",
            resource_key="schedule",
            resource_label="Schedule",
            event_label="Scheduled time",
            description="Run once or on a supported local-time cadence.",
            match_event_keys=("time.schedule.due",),
            subject_type=None,
            subject_required=False,
            config_fields=(
                ConfigFieldDefinition(
                    "timezone", "Timezone", FieldType.TEXT, "timezone", required=True, default="UTC"
                ),
                ConfigFieldDefinition(
                    "starts_at", "Starts at", FieldType.DATETIME, "datetime", required=True
                ),
                ConfigFieldDefinition(
                    "recurrence",
                    "Repeats",
                    FieldType.ENUM,
                    "select",
                    required=True,
                    default="none",
                    options=(
                        "none",
                        "daily",
                        "weekdays",
                        "weekly",
                        "biweekly",
                        "monthly",
                        "custom",
                    ),
                ),
                ConfigFieldDefinition(
                    "interval_days",
                    "Every N days",
                    FieldType.INTEGER,
                    "number",
                    depends_on="recurrence",
                ),
                ConfigFieldDefinition(
                    "subject_type",
                    "Record type",
                    FieldType.ENUM,
                    "select",
                    options=("deal", "contact", "account"),
                ),
                ConfigFieldDefinition(
                    "subject_ids",
                    "Records",
                    FieldType.LIST,
                    "record_multi_select",
                    depends_on="subject_type",
                ),
            ),
        ),
        TriggerDefinition(
            trigger_key="integration.webhook.received",
            family=TriggerFamily.WEBHOOK,
            category_key="integration",
            category_label="Integration",
            resource_key="webhook",
            resource_label="Webhook",
            event_label="Authenticated webhook",
            description="Run for a signed payload matching the saved schema.",
            match_event_keys=("integration.webhook.received",),
            subject_type=None,
            subject_required=False,
            config_fields=(
                ConfigFieldDefinition(
                    "payload_schema",
                    "Payload schema",
                    FieldType.OBJECT,
                    "json_schema",
                    required=True,
                ),
            ),
        ),
    ]
    triggers.extend(_record_triggers("deal", "Deal"))
    triggers.extend(
        [
            _event_trigger(
                "crm.deal.updated",
                trigger_key="crm.deal.stage_changed",
                category_key="crm",
                category_label="CRM",
                resource_key="deal",
                resource_label="Deal",
                event_label="Stage changed",
                description="Run when a deal stage changes.",
                compiler=_changed_compiler("stage_id"),
                fixed_watch=WatchDefinition("any", ("stage_id",)),
            ),
            _event_trigger(
                "crm.deal.updated",
                trigger_key="crm.deal.entered_stage",
                category_key="crm",
                category_label="CRM",
                resource_key="deal",
                resource_label="Deal",
                event_label="Entered stage",
                description="Run when a deal enters one selected stage.",
                compiler=_stage_compiler("after"),
                fixed_watch=WatchDefinition("any", ("stage_id",)),
                fixed_filter={
                    "all": [
                        {
                            "path": "changes.stage_id.after",
                            "operator": "eq",
                            "value_from_config": "stage_id",
                        }
                    ]
                },
                config_fields=(
                    ConfigFieldDefinition(
                        "stage_id",
                        "Stage",
                        FieldType.RELATION,
                        "stage_select",
                        required=True,
                        option_source="pipeline_stages",
                    ),
                ),
            ),
            _event_trigger(
                "crm.deal.updated",
                trigger_key="crm.deal.exited_stage",
                category_key="crm",
                category_label="CRM",
                resource_key="deal",
                resource_label="Deal",
                event_label="Exited stage",
                description="Run when a deal exits one selected stage.",
                compiler=_stage_compiler("before"),
                fixed_watch=WatchDefinition("any", ("stage_id",)),
                fixed_filter={
                    "all": [
                        {
                            "path": "changes.stage_id.before",
                            "operator": "eq",
                            "value_from_config": "stage_id",
                        }
                    ]
                },
                config_fields=(
                    ConfigFieldDefinition(
                        "stage_id",
                        "Stage",
                        FieldType.RELATION,
                        "stage_select",
                        required=True,
                        option_source="pipeline_stages",
                    ),
                ),
            ),
        ]
    )
    for key, label, status in (
        ("won", "Deal won", "won"),
        ("lost", "Deal lost", "lost"),
        ("reopened", "Deal reopened", "open"),
    ):
        triggers.append(
            _event_trigger(
                "crm.deal.updated",
                trigger_key=f"crm.deal.{key}",
                category_key="crm",
                category_label="CRM",
                resource_key="deal",
                resource_label="Deal",
                event_label=label,
                description=f"Run when a deal becomes {status}.",
                compiler=_status_compiler(status),
                fixed_watch=WatchDefinition("any", ("deal_status",)),
                fixed_filter={
                    "all": [
                        {
                            "path": "changes.deal_status.after",
                            "operator": "eq",
                            "value": status,
                        }
                    ]
                },
            )
        )
    triggers.extend(
        [
            _field_change_trigger("deal", "Deal", "amount", "Amount"),
            _field_change_trigger("deal", "Deal", "expected_close_date", "Close date"),
            _field_change_trigger("deal", "Deal", "owner_id", "Owner"),
            _field_change_trigger("deal", "Deal", "account_id", "Account"),
            _field_change_trigger("deal", "Deal", "primary_contact_id", "Primary contact"),
            _event_trigger(
                "crm.deal.updated",
                trigger_key="crm.deal.amount_crossed",
                category_key="crm",
                category_label="CRM",
                resource_key="deal",
                resource_label="Deal",
                event_label="Amount crossed threshold",
                description="Run when deal amount crosses a decimal threshold.",
                compiler=_amount_crossed_compiler,
                fixed_watch=WatchDefinition("any", ("amount",)),
                fixed_filter={
                    "all": [
                        {
                            "path": "changes.amount.before",
                            "operator_from_config": {
                                "up": "lt",
                                "down": "gt",
                                "field": "direction",
                            },
                            "value_from_config": "threshold",
                        },
                        {
                            "path": "changes.amount.after",
                            "operator_from_config": {
                                "up": "gte",
                                "down": "lte",
                                "field": "direction",
                            },
                            "value_from_config": "threshold",
                        },
                    ]
                },
                config_fields=(
                    ConfigFieldDefinition(
                        "direction",
                        "Direction",
                        FieldType.ENUM,
                        "select",
                        required=True,
                        options=("up", "down"),
                    ),
                    ConfigFieldDefinition(
                        "threshold", "Threshold", FieldType.DECIMAL, "decimal", required=True
                    ),
                ),
            ),
        ]
    )
    for resource, label, changed_fields in (
        (
            "contact",
            "Contact",
            (
                ("email", "Email"),
                ("phone", "Phone"),
                ("owner_id", "Owner"),
                ("account_id", "Account"),
                ("lifecycle_stage", "Lifecycle"),
            ),
        ),
        (
            "account",
            "Account",
            (
                ("domain", "Domain"),
                ("owner_id", "Owner"),
                ("lifecycle_stage", "Lifecycle"),
                ("tier", "Tier"),
            ),
        ),
        (
            "lead",
            "Lead",
            (("source", "Source"), ("status", "Status"), ("score", "Score"), ("owner_id", "Owner")),
        ),
        (
            "task",
            "Task",
            (("assignee_id", "Assignee"), ("status", "Status"), ("due_at", "Due time")),
        ),
    ):
        triggers.extend(_record_triggers(resource, label))
        triggers.extend(
            _field_change_trigger(resource, label, field_key, field_label)
            for field_key, field_label in changed_fields
        )
    triggers.extend(_record_triggers("note", "Note"))
    triggers.append(
        _event_trigger(
            "crm.lead.converted",
            category_key="crm",
            category_label="CRM",
            resource_key="lead",
            resource_label="Lead",
            event_label="Converted",
            description="Run when a lead is durably converted.",
        )
    )
    for key, label in (
        ("email.message.received", "Email received"),
        ("email.message.sent", "Email sent"),
        ("email.message.replied", "Email replied"),
    ):
        triggers.append(
            _event_trigger(
                key,
                category_key="communication",
                category_label="Communication",
                resource_key="email",
                resource_label="Email",
                event_label=label,
                description=f"Run when an {label.lower()} fact is durable.",
                producer_available=key != "email.message.replied",
            )
        )
    for key, label, available in (
        ("meeting.scheduled", "Scheduled", False),
        ("meeting.rescheduled", "Rescheduled", False),
        ("meeting.cancelled", "Cancelled", False),
        ("meeting.ended", "Ended", False),
        ("meeting.transcript.ready", "Transcript ready", True),
        ("meeting.summary.ready", "Summary ready", True),
    ):
        triggers.append(
            _event_trigger(
                key,
                category_key="conversation",
                category_label="Conversation",
                resource_key="meeting",
                resource_label="Meeting",
                event_label=label,
                description=f"Run when meeting {label.lower()} is durable.",
                producer_available=available,
            )
        )
    for key in ("conversation.message.received", "conversation.message.sent"):
        triggers.append(
            _event_trigger(
                key,
                category_key="conversation",
                category_label="Conversation",
                resource_key="message",
                resource_label="Message",
                event_label=key.rsplit(".", 1)[-1].title(),
                description="Reserved until durable message identity rules exist.",
                producer_available=False,
            )
        )
    for prefix, resource, label in (
        ("ai.deal_risk", "deal_risk", "Deal risk"),
        ("ai.deal_blocker", "deal_blocker", "Deal blocker"),
        ("ai.buyer_signal", "contact", "Buyer signal"),
    ):
        for transition in ("detected", "changed", "resolved"):
            item = _event_trigger(
                f"{prefix}.{transition}",
                category_key="ai",
                category_label="AI",
                resource_key=resource,
                resource_label=label,
                subject_type="deal" if prefix.startswith("ai.deal_") else resource,
                event_label=transition.title(),
                description=f"Run when a persisted {label.lower()} is {transition}.",
            )
            if prefix in {"ai.deal_risk", "ai.deal_blocker"}:
                item = replace(
                    item,
                    allowed_principal_modes=("organisation",),
                    allowed_scope_types=("organisation",),
                    allowed_audience_modes=("none", "explicit_users"),
                )
            triggers.append(item)
    for outcome in ("completed", "failed", "cancelled"):
        triggers.append(
            _event_trigger(
                f"agent.run.{outcome}",
                category_key="agent",
                category_label="Agent",
                resource_key="agent_run",
                resource_label="Run",
                event_label=outcome.title(),
                description=f"Run after an agent run is {outcome}.",
            )
        )
    for outcome in ("requested", "granted", "rejected", "expired"):
        triggers.append(
            _event_trigger(
                f"agent.approval.{outcome}",
                category_key="agent",
                category_label="Agent",
                resource_key="approval",
                resource_label="Approval",
                event_label=outcome.title(),
                description="Reserved until an approval decision has durable identity.",
                producer_available=False,
            )
        )
    return triggers


AUTOMATION_REGISTRY = AutomationRegistry(_events(), _triggers())


__all__ = [
    "AUTOMATION_REGISTRY",
    "FIELD_OPERATORS",
    "AutomationRegistry",
    "CompiledTrigger",
    "ConfigFieldDefinition",
    "EventDefinition",
    "FieldDefinition",
    "FieldType",
    "RecordField",
    "TriggerDefinition",
    "TriggerFamily",
    "WatchDefinition",
]
