"""Serialize the shared automation registry for the generic trigger picker."""

from __future__ import annotations

from collections import OrderedDict
from typing import Literal, cast

from src.api.schemas.automation_trigger_models import (
    AutomationTriggerCatalog,
    AutomationTriggerCatalogLimits,
    AutomationTriggerOptionSets,
    FilterFieldGroup,
    FilterInputType,
    TriggerCatalogCategory,
    TriggerCatalogConfigField,
    TriggerCatalogDefinition,
    TriggerCatalogField,
    TriggerCatalogPipelineOption,
    TriggerCatalogResource,
    TriggerCatalogRulePresentation,
    TriggerCatalogScopePresentation,
    TriggerCatalogSourceSelector,
    TriggerCatalogValueOption,
    TriggerCatalogWatchField,
)
from src.domain.models.organisation_tables.sync.meeting_intelligence import MeetingSource
from src.service.automation.triggers.matcher import (
    MAX_FILTER_GROUP_LEVELS,
    MAX_FILTER_LEAVES,
)
from src.service.automation.triggers.registry import (
    AUTOMATION_REGISTRY,
    AutomationRegistry,
    FieldDefinition,
    TriggerDefinition,
    filter_field_group,
    filter_field_input_type,
    filter_field_principal_modes,
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


def _trigger_event_options(
    registry: AutomationRegistry,
) -> list[TriggerCatalogValueOption]:
    labels: OrderedDict[str, str] = OrderedDict()
    for definition in registry.catalog_triggers():
        for event_key in definition.match_event_keys:
            labels.setdefault(
                event_key,
                f"{definition.category_label} / {definition.resource_label} / "
                f"{definition.event_label}",
            )
    return [TriggerCatalogValueOption(value=value, label=label) for value, label in labels.items()]


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
                "meeting_providers": [
                    TriggerCatalogValueOption(
                        value=item.value,
                        label=item.value.replace("_", " ").title(),
                    )
                    for item in MeetingSource
                ],
                "risk_severities": [
                    TriggerCatalogValueOption(value=value, label=value.title())
                    for value in ("LOW", "MEDIUM", "HIGH", "CRITICAL")
                ],
                "agent_run_failure_statuses": [
                    TriggerCatalogValueOption(value=value, label=label)
                    for value, label in (
                        ("failed", "Execution failed"),
                        ("delivery_failed", "Delivery failed"),
                        ("validation_failed", "Output validation failed"),
                    )
                ],
                "trigger_event_keys": _trigger_event_options(registry),
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
    field_event_keys: dict[str, list[str]] = {}
    watch_fields: OrderedDict[str, str] = OrderedDict()
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
            field_event_keys.setdefault(path, []).append(event_key)
        if definition.allow_user_watch:
            for path, field in event.watch_fields.items():
                watch_fields.setdefault(path, field.label)

    default_config = {
        item.key: item.default for item in definition.config_fields if item.default is not None
    }
    if definition.source_selector is not None:
        default_config["selection"] = {"mode": "all", "ids": []}
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
        description=(
            f"{definition.description} Changes recorded in CopilotGTM."
            if definition.category_key == "crm"
            else definition.description
        ),
        match_event_keys=list(definition.match_event_keys),
        subject_type=definition.subject_type,
        subject_required=definition.subject_required,
        allow_user_watch=definition.allow_user_watch,
        watch_fields=[
            TriggerCatalogWatchField(path=path, label=label) for path, label in watch_fields.items()
        ],
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
                input_type=cast(FilterInputType, filter_field_input_type(item)),
                option_source=item.option_source,
                visibility=(
                    "standard" if filter_field_principal_modes(definition, item.path) else "hidden"
                ),
                group=cast(FilterFieldGroup, filter_field_group(item.path)),
                principal_modes=[
                    cast(Literal["personal", "organisation"], mode)
                    for mode in filter_field_principal_modes(definition, item.path)
                ],
                event_keys=field_event_keys[item.path],
                required=item.required,
                exact=item.exact,
            )
            for item in fields.values()
        ],
        principal_modes=list(definition.allowed_principal_modes),
        scope_types=list(definition.allowed_scope_types),
        audience_modes=audience_modes,
        output_schema=definition.output_schema,
        scope_presentation=_scope_presentation(definition),
        rule_presentation=_rule_presentation(definition),
        timing_note=_timing_note(definition),
        source_selector=(
            TriggerCatalogSourceSelector(
                label=definition.source_selector.label,
                all_label_personal=definition.source_selector.all_label_personal,
                all_label_organisation=definition.source_selector.all_label_organisation,
                selected_label=definition.source_selector.selected_label,
                option_source=definition.source_selector.option_source,
                filter_path=definition.source_selector.filter_path,
                max_selected=definition.source_selector.max_selected,
            )
            if definition.source_selector is not None
            else None
        ),
        filter_empty_reason=(
            None
            if any(filter_field_principal_modes(definition, item.path) for item in fields.values())
            else "No additional business conditions are available for this trigger."
        ),
    )


def _scope_presentation(definition: TriggerDefinition) -> TriggerCatalogScopePresentation:
    personal_by_resource = {
        "deal": "Deals you own or created",
        "contact": "Contacts currently assigned to you",
        "account": "Accounts currently assigned to you",
        "email": "New messages in active mailboxes connected by you",
        "meeting": (
            "Meetings where you are the resolved internal organiser; otherwise the "
            "connected-calendar owner"
        ),
        "agent_run": "Runs created under your user identity",
        "manual": "Runs invoked under your user identity",
        "schedule": "Scheduled runs under your saved user identity",
        "webhook": "Webhook runs under your saved user identity",
    }
    organisation_by_resource = {
        "deal": "All organisation deals",
        "contact": "All organisation contacts",
        "account": "All organisation accounts",
        "email": "New messages in all active organisation mailboxes",
        "meeting": "All organisation meetings",
        "deal_risk": "All organisation deal risks",
        "deal_blocker": "All organisation deal blockers",
        "agent_run": "All organisation runs",
        "manual": "Runs invoked as the organisation service",
        "schedule": "Scheduled runs as the organisation service",
        "webhook": "Webhook runs as the organisation service",
    }
    return TriggerCatalogScopePresentation(
        personal=(
            personal_by_resource.get(definition.resource_key)
            if "personal" in definition.allowed_principal_modes
            else None
        ),
        organisation=(
            organisation_by_resource.get(definition.resource_key)
            if "organisation" in definition.allowed_principal_modes
            else None
        ),
    )


def trigger_scope_presentation(
    definition: TriggerDefinition,
) -> TriggerCatalogScopePresentation:
    """Return the same authority copy used by catalog definitions and saved cards."""

    return _scope_presentation(definition)


_RULE_SUMMARIES = {
    "crm.deal.stage_changed": "Stage changed",
    "crm.deal.entered_stage": "New stage is the selected stage",
    "crm.deal.exited_stage": "Previous stage is the selected stage",
    "crm.deal.won": "New deal status is Won",
    "crm.deal.lost": "New deal status is Lost",
    "crm.deal.reopened": "New deal status is Open",
    "crm.deal.amount_crossed": "Amount crosses the configured threshold",
}


def _rule_presentation(
    definition: TriggerDefinition,
) -> TriggerCatalogRulePresentation | None:
    summary = (
        "Creates always run; updates run only when one of the selected fields changes"
        if definition.trigger_key.endswith("created_or_updated")
        else _RULE_SUMMARIES.get(definition.trigger_key)
    )
    if summary is None and definition.fixed_watch is not None:
        summary = definition.description.removeprefix("Run when ").removesuffix(".").capitalize()
    return TriggerCatalogRulePresentation(summary=summary) if summary else None


def trigger_rule_presentation(
    definition: TriggerDefinition,
) -> TriggerCatalogRulePresentation | None:
    """Return the catalog-owned built-in rule for saved-card presentation."""

    return _rule_presentation(definition)


def _timing_note(definition: TriggerDefinition) -> str:
    if definition.trigger_key == "crm.deal.created":
        return "Starts with deals created after this trigger is active."
    if definition.resource_key == "email":
        return "Starts with new mail after this trigger is active; imported history is excluded."
    if definition.resource_key == "meeting":
        return "Starts when a new durable meeting artifact becomes ready after activation."
    if definition.family.value == "manual":
        return "Saving this trigger does not run it; use Run now to invoke it."
    if definition.family.value == "schedule":
        return "Runs when the next saved schedule occurrence becomes due."
    if definition.family.value == "webhook":
        return "Runs for new signed webhook deliveries after activation."
    return "Starts with new matching activity after this trigger is active."


__all__ = [
    "build_trigger_catalog",
    "trigger_rule_presentation",
    "trigger_scope_presentation",
]
