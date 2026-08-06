"""Public response mapping for persisted automation triggers."""

from typing import Literal

from src.api.schemas.automation_trigger_models import (
    AutomationTriggerDetail,
    AutomationTriggerSummary,
    TriggerAudienceDecision,
    TriggerDeliveryDecision,
    TriggerScopeDecision,
    TriggerTaskDecision,
    TriggerWatchDecision,
)
from src.domain.models.organisation_tables import AutomationTrigger
from src.service.automation.triggers.webhook import public_webhook_config


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
    trigger: AutomationTrigger, *, current_target_revision: int
) -> AutomationTriggerSummary:
    """Build the lightweight list representation and target drift signal."""

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
    )


def trigger_detail(
    trigger: AutomationTrigger, *, current_target_revision: int
) -> AutomationTriggerDetail:
    """Build the complete owner-visible trigger definition."""

    summary = trigger_summary(trigger, current_target_revision=current_target_revision)
    return AutomationTriggerDetail(
        **summary.model_dump(),
        target_snapshot=trigger.target_snapshot,
        config_schema_version=trigger.config_schema_version,
        config=public_webhook_config(trigger.trigger_config),
        watch=(
            TriggerWatchDecision.model_validate(trigger.watch_config)
            if trigger.watch_config
            else None
        ),
        filter=trigger.filter_config,
        execution_principal_type=trigger.execution_principal_type,
        execution_principal_id=trigger.execution_principal_id,
        scope=TriggerScopeDecision(type=trigger.scope_type, config=trigger.scope_config),
        audience=TriggerAudienceDecision(
            mode=trigger.audience_mode,
            user_ids=trigger.audience_config.get("user_ids", []),
        ),
        task=TriggerTaskDecision.model_validate(trigger.task_config),
        delivery=TriggerDeliveryDecision.model_validate(trigger.delivery_config),
        config_hash=trigger.config_hash,
    )
