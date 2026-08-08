"""Serialize persisted trigger bindings into safe, business-readable responses.

The serializer keeps stable IDs available for actions while resolving labels
through the same registry and authority context used during validation. Saved
values that are no longer selectable are surfaced as warnings rather than
silently rewritten.
"""

from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID

from pydantic import TypeAdapter
from src.api.schemas.automation_trigger_models import (
    AutomationTriggerDetail,
    AutomationTriggerPresentation,
    AutomationTriggerSummary,
    TriggerAudienceDecision,
    TriggerConditionPresentation,
    TriggerDeliveryDecision,
    TriggerScopeDecision,
    TriggerTaskDecision,
    TriggerWatchDecision,
)
from src.domain.models.organisation_tables import AutomationTrigger
from src.service.automation.triggers.catalog import (
    trigger_rule_presentation,
    trigger_scope_presentation,
)
from src.service.automation.triggers.matcher import extract_user_filter
from src.service.automation.triggers.options import TriggerOptionRecord
from src.service.automation.triggers.registry import (
    AUTOMATION_REGISTRY,
    AutomationRegistry,
    FieldDefinition,
    TriggerDefinition,
    filter_field_group,
)
from src.service.automation.triggers.webhook import public_webhook_config

# The discriminator adapter keeps old or malformed JSON from being silently
# coerced into a different delivery mode when a trigger is read back.
_DELIVERY_ADAPTER = TypeAdapter(TriggerDeliveryDecision)


def _value(value: object) -> str:
    return str(getattr(value, "value", value))


def _principal_mode(trigger: AutomationTrigger) -> Literal["personal", "organisation"]:
    return "personal" if _value(trigger.execution_principal_type) == "user" else "organisation"


def _display_status(trigger: AutomationTrigger) -> str:
    if trigger.last_error or trigger.consecutive_failures:
        return "needs_attention"
    if (
        _value(trigger.family) == "schedule"
        and _value(trigger.status) == "active"
        and trigger.next_trigger_at is None
        and trigger.trigger_config.get("recurrence") == "none"
    ):
        return "completed"
    return _value(trigger.status)


def trigger_summary(
    trigger: AutomationTrigger,
    *,
    current_target_revision: int,
    definition: TriggerDefinition | None = None,
    registry: AutomationRegistry = AUTOMATION_REGISTRY,
    resolved_options: Mapping[tuple[str, str], TriggerOptionRecord] | None = None,
) -> AutomationTriggerSummary:
    """Build the lightweight list representation and target drift signal."""

    definition = definition or registry.trigger(trigger.trigger_key)
    presentation = _trigger_presentation(
        trigger,
        definition,
        registry,
        resolved_options or {},
    )
    return AutomationTriggerSummary(
        trigger_id=trigger.trigger_id,
        name=trigger.name,
        target_type=trigger.target_type,
        target_id=trigger.target_id,
        target_revision=trigger.target_revision,
        target_update_available=trigger.target_revision != current_target_revision,
        trigger_key=trigger.trigger_key,
        family=trigger.family,
        match_event_keys=list(trigger.match_event_keys),
        principal_mode=_principal_mode(trigger),
        status=trigger.status,
        display_status=_display_status(trigger),
        next_trigger_at=trigger.next_trigger_at,
        last_triggered_at=trigger.last_triggered_at,
        trigger_count=trigger.trigger_count,
        consecutive_failures=trigger.consecutive_failures,
        last_error_code=trigger.last_error_code,
        last_error=trigger.last_error,
        created_at=trigger.created_at,
        updated_at=trigger.updated_at,
        presentation=presentation,
    )


def trigger_detail(
    trigger: AutomationTrigger,
    *,
    current_target_revision: int,
    definition: TriggerDefinition,
    registry: AutomationRegistry = AUTOMATION_REGISTRY,
    resolved_options: Mapping[tuple[str, str], TriggerOptionRecord] | None = None,
) -> AutomationTriggerDetail:
    """Build the complete owner-visible trigger definition."""

    summary = trigger_summary(
        trigger,
        current_target_revision=current_target_revision,
        definition=definition,
        registry=registry,
        resolved_options=resolved_options,
    )
    config = public_webhook_config(trigger.trigger_config)
    try:
        fixed = definition.compile(config)
        user_filter = extract_user_filter(fixed.filter, trigger.filter_config)
    except ValueError:
        summary.display_status = "needs_attention"
        user_filter = None
    return AutomationTriggerDetail(
        **summary.model_dump(),
        target_snapshot=trigger.target_snapshot,
        config_schema_version=trigger.config_schema_version,
        config=config,
        watch=(
            TriggerWatchDecision.model_validate(trigger.watch_config)
            if definition.allow_user_watch and trigger.watch_config
            else None
        ),
        filter=user_filter,
        execution_principal_type=trigger.execution_principal_type,
        execution_principal_id=trigger.execution_principal_id,
        scope=TriggerScopeDecision(type=trigger.scope_type, config=trigger.scope_config),
        audience=TriggerAudienceDecision(
            mode=trigger.audience_mode,
            user_ids=trigger.audience_config.get("user_ids", []),
        ),
        task=TriggerTaskDecision.model_validate(trigger.task_config),
        delivery=_DELIVERY_ADAPTER.validate_python(trigger.delivery_config),
        config_hash=trigger.config_hash,
    )


def _trigger_presentation(
    trigger: AutomationTrigger,
    definition: TriggerDefinition,
    registry: AutomationRegistry,
    resolved: Mapping[tuple[str, str], TriggerOptionRecord],
) -> AutomationTriggerPresentation:
    principal_mode = _principal_mode(trigger)
    scope = trigger_scope_presentation(definition)
    rule = trigger_rule_presentation(definition)
    warnings: list[str] = []
    source_summary = _source_summary(trigger, definition, principal_mode, resolved, warnings)
    # Delivery copy is derived from server-owned config and audience state; it
    # must not invent a recipient label from an inaccessible saved ID.
    delivery_summary, delivery_status, delivery_reason = _delivery_presentation(trigger)
    conditions = _condition_presentations(
        trigger,
        definition,
        registry,
        resolved,
        warnings,
    )
    return AutomationTriggerPresentation(
        breadcrumb=(
            f"{definition.category_label} / {definition.resource_label} / {definition.event_label}"
        ),
        authority_label="Runs as you" if principal_mode == "personal" else "Runs as organisation",
        scope_summary=(scope.personal if principal_mode == "personal" else scope.organisation)
        or "Saved execution scope",
        built_in_rule=rule.summary if rule else None,
        source_summary=source_summary,
        delivery_mode=(
            "email_notification"
            if trigger.delivery_config.get("mode") == "email_notification"
            else "none"
        ),
        delivery_summary=delivery_summary,
        delivery_status=delivery_status,
        delivery_reason=delivery_reason,
        conditions=conditions,
        warnings=list(dict.fromkeys(warnings)),
    )


def presentation_option_selections(
    trigger: AutomationTrigger,
    definition: TriggerDefinition,
    registry: AutomationRegistry,
) -> dict[tuple[str, bool], set[str]]:
    """Collect one card's option values, marking sources that require record authority."""

    selections: dict[tuple[str, bool], set[str]] = {}
    selector = definition.source_selector
    selection = trigger.trigger_config.get("selection")
    if selector and isinstance(selection, dict) and selection.get("mode") == "selected":
        selections[(selector.option_source, True)] = {
            str(value) for value in selection.get("ids", [])
        }
    subject_type = trigger.trigger_config.get("subject_type")
    subject_source = {
        "deal": "deals",
        "contact": "contacts",
        "account": "accounts",
    }.get(subject_type)
    if subject_source:
        values = {str(value) for value in trigger.trigger_config.get("subject_ids", [])}
        if values:
            selections[(subject_source, True)] = values
    try:
        compiled = definition.compile(trigger.trigger_config)
        tree = extract_user_filter(compiled.filter, trigger.filter_config)
    except ValueError:
        return selections
    fields: dict[str, FieldDefinition] = {}
    for event_key in definition.match_event_keys:
        fields.update(registry.event(event_key).fields)
    for leaf in _filter_leaves(tree):
        path = leaf.get("path")
        field = fields.get(path) if isinstance(path, str) else None
        if field is None or field.option_source is None:
            continue
        source = field.option_source
        if (
            definition.trigger_key.startswith("ai.")
            and definition.trigger_key.endswith(".resolved")
            and path == "record.status"
        ):
            source = "risk_resolution_statuses"
        value = leaf.get("value")
        values = value if isinstance(value, list) else ([] if value is None else [value])
        selections.setdefault((source, False), set()).update(str(item) for item in values)
    return selections


def _delivery_presentation(
    trigger: AutomationTrigger,
) -> tuple[str, Literal["active", "needs_attention"], str | None]:
    """Describe the saved delivery mode without exposing recipient identities."""

    delivery = trigger.delivery_config
    if delivery.get("mode") != "email_notification":
        return "Run history only", "active", None
    if _principal_mode(trigger) == "personal":
        return "Email you from CopilotGTM", "active", None
    count = len(trigger.audience_config.get("user_ids", []))
    if _value(trigger.audience_mode) == "subject_participants":
        return "Email internal subject participants from CopilotGTM", "active", None
    return f"Email {count} audience members from CopilotGTM", "active", None


def _source_summary(
    trigger: AutomationTrigger,
    definition: TriggerDefinition,
    principal_mode: str,
    resolved: Mapping[tuple[str, str], TriggerOptionRecord],
    warnings: list[str],
) -> str | None:
    selector = definition.source_selector
    if selector is not None:
        selection = trigger.trigger_config.get("selection")
        if not isinstance(selection, dict) or selection.get("mode") != "selected":
            return (
                selector.all_label_personal
                if principal_mode == "personal"
                else selector.all_label_organisation
            )
        labels = _resolved_labels(
            selector.option_source,
            selection.get("ids", []),
            resolved,
            warnings,
        )
        return f"{selector.selected_label}: {', '.join(labels)}"

    subject_type = trigger.trigger_config.get("subject_type")
    subject_ids = trigger.trigger_config.get("subject_ids")
    source = {"deal": "deals", "contact": "contacts", "account": "accounts"}.get(subject_type)
    if source and isinstance(subject_ids, list) and subject_ids:
        labels = _resolved_labels(source, subject_ids, resolved, warnings)
        return f"Specific {subject_type} records: {', '.join(labels)}"
    if definition.family.value in {"manual", "schedule"}:
        return "One global run" if definition.family.value == "manual" else "One run per occurrence"
    return None


def _condition_presentations(
    trigger: AutomationTrigger,
    definition: TriggerDefinition,
    registry: AutomationRegistry,
    resolved: Mapping[tuple[str, str], TriggerOptionRecord],
    warnings: list[str],
) -> list[TriggerConditionPresentation]:
    try:
        compiled = definition.compile(trigger.trigger_config)
        tree = extract_user_filter(compiled.filter, trigger.filter_config)
    except ValueError:
        warnings.append("Needs attention")
        return []
    fields: dict[str, FieldDefinition] = {}
    for event_key in definition.match_event_keys:
        fields.update(registry.event(event_key).fields)
    result: list[TriggerConditionPresentation] = []
    for leaf in _filter_leaves(tree):
        path = leaf.get("path")
        field = fields.get(path) if isinstance(path, str) else None
        if field is None:
            warnings.append("Needs attention")
            continue
        values = leaf.get("value")
        raw_values = values if isinstance(values, list) else ([] if values is None else [values])
        status: Literal["active", "needs_attention"] = "active"
        if field.option_source:
            source = field.option_source
            if (
                definition.trigger_key.startswith("ai.")
                and definition.trigger_key.endswith(".resolved")
                and path == "record.status"
            ):
                source = "risk_resolution_statuses"
            labels = _resolved_labels(source, raw_values, resolved, warnings)
            if any(
                resolved.get((source, str(value))) is None
                or not resolved[(source, str(value))].selectable
                for value in raw_values
            ):
                status = "needs_attention"
        else:
            labels = [_scalar_label(value, warnings) for value in raw_values]
            if "Unavailable selection" in labels:
                status = "needs_attention"
        result.append(
            TriggerConditionPresentation(
                field_label=field.label,
                group=filter_field_group(path),
                operator=str(leaf.get("operator", "")),
                value_labels=labels,
                status=status,
            )
        )
    return result


def _filter_leaves(tree: Any) -> list[dict[str, Any]]:
    if not isinstance(tree, dict):
        return []
    for mode in ("all", "any"):
        children = tree.get(mode)
        if isinstance(children, list):
            return [leaf for child in children for leaf in _filter_leaves(child)]
    return [tree]


def _resolved_labels(
    source: str,
    values: Any,
    resolved: Mapping[tuple[str, str], TriggerOptionRecord],
    warnings: list[str],
) -> list[str]:
    labels = []
    for value in values if isinstance(values, list) else []:
        item = resolved.get((source, str(value)))
        if item is None:
            labels.append("Unavailable selection")
            warnings.append("Needs attention")
        else:
            labels.append(item.label)
            if not item.selectable:
                warnings.append("Needs attention")
    return labels


def _scalar_label(value: Any, warnings: list[str]) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    text = str(value)
    try:
        UUID(text)
    except ValueError:
        return text
    warnings.append("Needs attention")
    return "Unavailable selection"


__all__ = ["presentation_option_selections", "trigger_detail", "trigger_summary"]
