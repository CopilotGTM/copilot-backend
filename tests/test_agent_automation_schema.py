"""Regression tests for test agent automation schema; these cases lock the backend contract used by the changed feature."""

from __future__ import annotations

from typing import Any

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from src.domain.models.organisation_tables import (
    AgentOutputMode,
    AgentRun,
    AgentRunStatus,
    AgentSourceType,
    AgentStatus,
    AgentVisibility,
    AutomationAudienceMode,
    AutomationEventDispatch,
    AutomationPrincipalType,
    AutomationRouteMode,
    AutomationScopeType,
    AutomationTargetType,
    AutomationTrigger,
    AutomationTriggerFamily,
    AutomationTriggerStatus,
    UserAgent,
)


def _column_names(model: type[Any]) -> set[str]:
    return set(model.__table__.columns.keys())


def _check_names(model: type[Any]) -> set[str]:
    return {
        constraint.name
        for constraint in model.__table__.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    }


def _index(model: type[Any], name: str) -> Index:
    for index in model.__table__.indexes:
        if index.name == name:
            return index
    raise AssertionError(f"{name} index not found on {model.__name__}")


def _index_names(model: type[Any]) -> set[str]:
    return {index.name for index in model.__table__.indexes if index.name is not None}


def _index_columns(index: Index) -> tuple[str, ...]:
    return tuple(getattr(expression, "name", str(expression)) for expression in index.expressions)


def _postgresql_where(index: Index) -> str:
    return str(index.dialect_options["postgresql"]["where"])


def _enum_values(enum_type: type[Any]) -> set[str]:
    return {member.value for member in enum_type}


def _foreign_key(model: type[Any], name: str) -> ForeignKeyConstraint:
    for constraint in model.__table__.constraints:
        if isinstance(constraint, ForeignKeyConstraint) and constraint.name == name:
            return constraint
    raise AssertionError(f"{name} foreign key not found on {model.__name__}")


def _foreign_key_targets(constraint: ForeignKeyConstraint) -> tuple[str, ...]:
    return tuple(element.target_fullname for element in constraint.elements)


def test_agent_automation_enums_match_the_ai_native_contract() -> None:
    """Verify the contract that agent automation enums match the ai native contract."""
    assert _enum_values(AgentSourceType) == {"user", "system"}
    assert _enum_values(AgentVisibility) == {"private", "organisation"}
    assert _enum_values(AgentStatus) == {"active", "paused", "archived"}
    assert _enum_values(AgentOutputMode) == {"text", "json"}
    assert _enum_values(AutomationTargetType) == {"agent", "workflow"}
    assert _enum_values(AutomationTriggerFamily) == {
        "event",
        "manual",
        "schedule",
        "webhook",
    }
    assert _enum_values(AutomationPrincipalType) == {"user", "organisation_service"}
    assert _enum_values(AutomationScopeType) == {
        "owned_or_created_by_principal",
        "organisation",
    }
    assert _enum_values(AutomationAudienceMode) == {
        "principal",
        "none",
        "explicit_users",
        "subject_participants",
    }
    assert _enum_values(AutomationRouteMode) == {"match", "direct"}
    assert _enum_values(AutomationTriggerStatus) == {"active", "paused", "archived"}
    assert _enum_values(AgentRunStatus) == {
        "pending",
        "running",
        "succeeded",
        "failed",
        "validation_failed",
        "cancelled",
    }


def test_user_agent_owns_output_delivery_configuration() -> None:
    """Keep output delivery with the agent definition, not its triggers."""
    assert UserAgent.__tablename__ == "user_agents"
    assert "model_settings" in UserAgent.model_fields
    assert "model_config" not in UserAgent.model_fields
    assert isinstance(UserAgent.__table__.c.model_config.type, JSONB)
    assert isinstance(UserAgent.__table__.c.output_delivery_config.type, JSONB)
    assert UserAgent.__table__.c.output_delivery_config.nullable is False
    assert UserAgent.__table__.c.output_delivery_updated_at.nullable is False


def test_trigger_persists_compiled_routing_authority_and_target_snapshot() -> None:
    """Verify the contract that trigger persists compiled routing authority and target snapshot."""
    assert _column_names(AutomationTrigger) == {
        "trigger_id",
        "organisation_id",
        "created_by_user_id",
        "name",
        "target_type",
        "target_id",
        "target_revision",
        "target_snapshot",
        "trigger_key",
        "family",
        "match_event_keys",
        "config_schema_version",
        "trigger_config",
        "watch_config",
        "filter_config",
        "execution_principal_type",
        "execution_principal_id",
        "scope_type",
        "scope_config",
        "audience_mode",
        "audience_config",
        "task_config",
        "config_hash",
        "status",
        "next_trigger_at",
        "last_triggered_at",
        "trigger_count",
        "consecutive_failures",
        "last_error_code",
        "last_error",
        "created_at",
        "updated_at",
    }
    assert not AutomationTrigger.__table__.c.target_id.foreign_keys
    assert isinstance(AutomationTrigger.__table__.c.match_event_keys.type, ARRAY)
    for column_name in (
        "target_snapshot",
        "trigger_config",
        "watch_config",
        "filter_config",
        "scope_config",
        "audience_config",
        "task_config",
    ):
        assert isinstance(AutomationTrigger.__table__.c[column_name].type, JSONB)
    assert AutomationTrigger.__table__.c.watch_config.nullable is True
    assert AutomationTrigger.__table__.c.filter_config.nullable is True

    assert _check_names(AutomationTrigger) == {
        "ck_automation_triggers_target_type",
        "ck_automation_triggers_target_revision",
        "ck_automation_triggers_family",
        "ck_automation_triggers_match_event_keys",
        "ck_automation_triggers_config_schema_version",
        "ck_automation_triggers_principal_scope_audience",
        "ck_automation_triggers_status",
        "ck_automation_triggers_counts",
    }
    assert _index_names(AutomationTrigger) == {
        "ix_automation_triggers_target",
        "ix_automation_triggers_due_schedule",
        "ix_automation_triggers_active_event_keys",
        "uq_automation_triggers_active_config",
        "uq_automation_triggers_active_org_service_config",
    }
    assert _index_columns(_index(AutomationTrigger, "ix_automation_triggers_target")) == (
        "organisation_id",
        "created_by_user_id",
        "target_type",
        "target_id",
        "status",
        "updated_at DESC",
    )
    due = _index(AutomationTrigger, "ix_automation_triggers_due_schedule")
    assert _index_columns(due) == ("next_trigger_at", "trigger_id")
    assert _postgresql_where(due) == (
        "status = 'active' AND family = 'schedule' AND next_trigger_at IS NOT NULL"
    )
    event = _index(AutomationTrigger, "ix_automation_triggers_active_event_keys")
    assert _index_columns(event) == ("match_event_keys",)
    assert event.dialect_options["postgresql"]["using"] == "gin"
    assert _postgresql_where(event) == "status = 'active' AND family = 'event'"
    duplicate = _index(AutomationTrigger, "uq_automation_triggers_active_config")
    assert duplicate.unique is True
    assert _index_columns(duplicate) == (
        "organisation_id",
        "created_by_user_id",
        "target_type",
        "target_id",
        "config_hash",
    )
    assert _postgresql_where(duplicate) == "status <> 'archived'"
    shared_duplicate = _index(
        AutomationTrigger,
        "uq_automation_triggers_active_org_service_config",
    )
    assert shared_duplicate.unique is True
    assert _index_columns(shared_duplicate) == (
        "organisation_id",
        "target_type",
        "target_id",
        "config_hash",
    )
    assert _postgresql_where(shared_duplicate) == (
        "status <> 'archived' AND execution_principal_type = 'organisation_service'"
    )


def test_dispatch_persists_the_canonical_envelope_and_activation_decision() -> None:
    """Verify the contract that dispatch persists the canonical envelope and activation decision."""
    assert _column_names(AutomationEventDispatch) == {
        "event_id",
        "organisation_id",
        "event_key",
        "route_mode",
        "direct_trigger_id",
        "idempotency_key",
        "envelope",
        "activation_outcomes",
        "candidate_count",
        "matched_count",
        "attempt_count",
        "last_attempted_at",
        "processed_at",
        "failed_at",
        "last_error_code",
        "last_error",
        "created_at",
    }
    assert isinstance(AutomationEventDispatch.__table__.c.envelope.type, JSONB)
    assert isinstance(AutomationEventDispatch.__table__.c.activation_outcomes.type, JSONB)
    assert _check_names(AutomationEventDispatch) == {
        "ck_automation_event_dispatch_route",
        "ck_automation_event_dispatch_counts",
        "ck_automation_event_dispatch_attempts",
        "ck_automation_event_dispatch_terminal_state",
    }
    trigger_fk = _foreign_key(
        AutomationEventDispatch,
        "fk_automation_event_dispatches_direct_trigger_id_org",
    )
    assert _foreign_key_targets(trigger_fk) == (
        "automation_triggers.organisation_id",
        "automation_triggers.trigger_id",
    )
    assert trigger_fk.ondelete == "RESTRICT"
    assert _index_names(AutomationEventDispatch) == {
        "uq_automation_event_dispatch_idempotency",
        "ix_automation_event_dispatch_direct_trigger",
        "ix_automation_event_dispatch_pending",
        "ix_automation_event_dispatch_event_created",
    }
    unique = _index(AutomationEventDispatch, "uq_automation_event_dispatch_idempotency")
    assert unique.unique is True
    assert _index_columns(unique) == ("organisation_id", "idempotency_key")
    pending = _index(AutomationEventDispatch, "ix_automation_event_dispatch_pending")
    assert _index_columns(pending) == ("created_at", "event_id")
    assert _postgresql_where(pending) == "processed_at IS NULL AND failed_at IS NULL"


def test_run_pins_authority_lineage_and_one_trigger_event_decision() -> None:
    """Verify the contract that run pins authority lineage and one trigger event decision."""
    assert _column_names(AgentRun) == {
        "run_id",
        "organisation_id",
        "created_by_user_id",
        "execution_principal_type",
        "execution_principal_id",
        "audience_mode",
        "audience_config",
        "agent_id",
        "trigger_id",
        "agent_revision",
        "status",
        "idempotency_key",
        "event_id",
        "event_key",
        "scheduled_for",
        "correlation_id",
        "caused_by_run_id",
        "causation_depth",
        "trigger_snapshot",
        "agent_snapshot",
        "input_snapshot",
        "output_delivery_config",
        "output_text",
        "output_json",
        "error_code",
        "error_message",
        "attempt_count",
        "claimed_at",
        "claimed_by",
        "model_name",
        "token_usage",
        "cost",
        "latency_ms",
        "created_at",
        "started_at",
        "completed_at",
    }
    assert AgentRun.__table__.c.trigger_id.nullable is False
    assert AgentRun.__table__.c.event_id.nullable is False
    assert AgentRun.__table__.c.idempotency_key.nullable is False

    expected_fks = {
        "fk_agent_runs_agent_id_org": (
            "user_agents.organisation_id",
            "user_agents.agent_id",
        ),
        "fk_agent_runs_trigger_id_org": (
            "automation_triggers.organisation_id",
            "automation_triggers.trigger_id",
        ),
        "fk_agent_runs_event_id_org": (
            "automation_event_dispatches.organisation_id",
            "automation_event_dispatches.event_id",
        ),
        "fk_agent_runs_caused_by_run_id_org": (
            "agent_runs.organisation_id",
            "agent_runs.run_id",
        ),
    }
    for name, targets in expected_fks.items():
        constraint = _foreign_key(AgentRun, name)
        assert _foreign_key_targets(constraint) == targets
        assert constraint.ondelete == "RESTRICT"

    assert _check_names(AgentRun) == {
        "ck_agent_runs_principal_audience",
        "ck_agent_runs_agent_revision",
        "ck_agent_runs_status",
        "ck_agent_runs_causation_depth",
        "ck_agent_runs_attempt_count",
        "ck_agent_runs_cost",
        "ck_agent_runs_latency_ms",
    }
    assert _index_names(AgentRun) == {
        "ix_agent_runs_creator_created",
        "ix_agent_runs_principal_created",
        "ix_agent_runs_agent_created",
        "ix_agent_runs_trigger_created",
        "ix_agent_runs_event",
        "ix_agent_runs_causation",
        "ix_agent_runs_pending_claim",
        "uq_agent_runs_trigger_event",
    }
    unique = _index(AgentRun, "uq_agent_runs_trigger_event")
    assert unique.unique is True
    assert _index_columns(unique) == ("organisation_id", "trigger_id", "event_id")
    for column_name in (
        "audience_config",
        "trigger_snapshot",
        "agent_snapshot",
        "input_snapshot",
        "output_delivery_config",
        "output_json",
        "token_usage",
    ):
        assert isinstance(AgentRun.__table__.c[column_name].type, JSONB)


def test_every_parent_used_by_a_tenant_foreign_key_has_a_tenant_unique_key() -> None:
    """Verify the contract that every parent used by a tenant foreign key has a tenant unique key."""
    for model, columns in (
        (UserAgent, ("organisation_id", "agent_id")),
        (AutomationTrigger, ("organisation_id", "trigger_id")),
        (AutomationEventDispatch, ("organisation_id", "event_id")),
        (AgentRun, ("organisation_id", "run_id")),
    ):
        unique_columns = {
            tuple(column.name for column in constraint.columns)
            for constraint in model.__table__.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        assert columns in unique_columns
