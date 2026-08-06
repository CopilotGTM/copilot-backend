"""Serialize the shared automation registry for the generic trigger picker."""

from __future__ import annotations

from collections import OrderedDict

from src.api.schemas.automation_trigger_models import (
    AutomationTriggerCatalog,
    AutomationTriggerCatalogLimits,
    AutomationTriggerOptionSets,
    TriggerCatalogCategory,
    TriggerCatalogConfigField,
    TriggerCatalogDefinition,
    TriggerCatalogField,
    TriggerCatalogPipelineOption,
    TriggerCatalogResource,
    TriggerCatalogValueOption,
    TriggerCatalogWatch,
)
from src.service.automation.triggers.matcher import (
    MAX_FILTER_GROUP_LEVELS,
    MAX_FILTER_LEAVES,
)
from src.service.automation.triggers.registry import (
    AUTOMATION_REGISTRY,
    AutomationRegistry,
    FieldDefinition,
    TriggerDefinition,
)
from src.service.automation.triggers.webhook import WEBHOOK_MAX_BODY_BYTES

_CATEGORY_ORDER = (
    "system",
    "time",
    "crm",
    "communication",
    "conversation",
    "integration",
    "ai",
    "agent",
)


def build_trigger_catalog(
    pipelines: list[TriggerCatalogPipelineOption],
    *,
    registry: AutomationRegistry = AUTOMATION_REGISTRY,
    custom_option_sets: dict[str, tuple[tuple[str, str], ...]] | None = None,
) -> AutomationTriggerCatalog:
    """Build only definitions backed by every producer they require."""

    grouped: dict[str, OrderedDict[str, list[TriggerDefinition]]] = {}
    category_labels: dict[str, str] = {}
    resource_labels: dict[tuple[str, str], str] = {}
    for definition in registry.catalog_triggers():
        category_labels[definition.category_key] = definition.category_label
        resource_labels[(definition.category_key, definition.resource_key)] = (
            definition.resource_label
        )
        grouped.setdefault(definition.category_key, OrderedDict()).setdefault(
            definition.resource_key,
            [],
        ).append(definition)

    ordered_categories = [key for key in _CATEGORY_ORDER if key in grouped]
    ordered_categories.extend(key for key in grouped if key not in _CATEGORY_ORDER)
    categories = [
        TriggerCatalogCategory(
            category_key=category_key,
            label=category_labels[category_key],
            resources=[
                TriggerCatalogResource(
                    resource_key=resource_key,
                    label=resource_labels[(category_key, resource_key)],
                    triggers=[_serialize_trigger(item, registry) for item in definitions],
                )
                for resource_key, definitions in grouped[category_key].items()
            ],
        )
        for category_key in ordered_categories
    ]
    return AutomationTriggerCatalog(
        categories=categories,
        limits=AutomationTriggerCatalogLimits(
            webhook_max_body_bytes=WEBHOOK_MAX_BODY_BYTES,
            max_direct_subject_ids=100,
            max_filter_group_levels=MAX_FILTER_GROUP_LEVELS,
            max_filter_conditions=MAX_FILTER_LEAVES,
        ),
        option_sets=AutomationTriggerOptionSets(
            pipelines=pipelines,
            enums={
                "deal_statuses": [
                    TriggerCatalogValueOption(value=value, label=value.title())
                    for value in ("open", "won", "lost", "closed")
                ],
                **{
                    key: [
                        TriggerCatalogValueOption(value=value, label=label)
                        for value, label in values
                    ]
                    for key, values in (custom_option_sets or {}).items()
                },
            },
        ),
    )


def _serialize_trigger(
    definition: TriggerDefinition,
    registry: AutomationRegistry,
) -> TriggerCatalogDefinition:
    fields: OrderedDict[str, FieldDefinition] = OrderedDict()
    watch_paths: OrderedDict[str, None] = OrderedDict()
    for event_key in definition.match_event_keys:
        event = registry.event(event_key)
        for path, field in event.fields.items():
            if not field.operators:
                continue
            existing = fields.get(path)
            if existing is not None and (
                existing.value_type != field.value_type
                or existing.accessor != field.accessor
                or existing.operators != field.operators
            ):
                raise ValueError(f"incompatible field path across trigger events: {path}")
            fields.setdefault(path, field)
        if definition.allow_user_watch:
            for path in event.watch_fields:
                watch_paths.setdefault(path, None)

    default_config = {
        item.key: item.default for item in definition.config_fields if item.default is not None
    }
    supports_subject_participants = all(
        registry.event(key).supports_subject_participants for key in definition.match_event_keys
    )
    audience_modes = [
        mode
        for mode in definition.allowed_audience_modes
        if mode != "subject_participants" or supports_subject_participants
    ]
    return TriggerCatalogDefinition(
        trigger_key=definition.trigger_key,
        family=definition.family.value,
        event_label=definition.event_label,
        description=definition.description,
        match_event_keys=list(definition.match_event_keys),
        subject_type=definition.subject_type,
        subject_required=definition.subject_required,
        allow_user_watch=definition.allow_user_watch,
        watch_paths=list(watch_paths),
        fixed_watch=(
            TriggerCatalogWatch(
                mode=definition.fixed_watch.mode,
                paths=list(definition.fixed_watch.paths),
            )
            if definition.fixed_watch
            else None
        ),
        fixed_filter=definition.fixed_filter,
        default_config=default_config,
        config_fields=[
            TriggerCatalogConfigField(
                key=item.key,
                label=item.label,
                value_type=item.value_type.value,
                input_type=item.input_type,
                required=item.required,
                default=item.default,
                options=list(item.options),
                option_source=item.option_source,
                depends_on=item.depends_on,
            )
            for item in definition.config_fields
        ],
        fields=[
            TriggerCatalogField(
                path=item.path,
                label=item.label,
                description=item.description,
                value_type=item.value_type.value,
                operators=list(item.operators),
                option_source=item.option_source,
                required=item.required,
                exact=item.exact,
            )
            for item in fields.values()
        ],
        principal_modes=list(definition.allowed_principal_modes),
        scope_types=list(definition.allowed_scope_types),
        audience_modes=audience_modes,
        output_schema=definition.output_schema,
    )


__all__ = ["build_trigger_catalog"]
