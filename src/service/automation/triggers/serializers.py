"""Public response mapping for persisted automation triggers."""

from src.api.schemas.automation_trigger_models import (
    AutomationTriggerDetail,
    AutomationTriggerSummary,
)
from src.domain.models.organisation_tables import AutomationTrigger


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
        trigger_type=trigger.trigger_type,
        source_domain=trigger.source_domain,
        event_type=trigger.event_type,
        status=trigger.status,
        next_trigger_at=trigger.next_trigger_at,
        last_triggered_at=trigger.last_triggered_at,
        trigger_count=trigger.trigger_count,
        consecutive_failures=trigger.consecutive_failures,
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
        trigger_config=trigger.trigger_config,
        filter_config=trigger.filter_config,
        scope_type=trigger.scope_type,
        scope_config=trigger.scope_config,
        task_config=trigger.task_config,
        delivery_config=trigger.delivery_config,
        config_hash=trigger.config_hash,
    )
