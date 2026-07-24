from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from sqlalchemy import CheckConstraint, Index
from sqlalchemy.dialects.postgresql import JSONB
from src.domain.models.organisation_tables import (
    AgentOutputMode,
    AgentRun,
    AgentRunStatus,
    AgentSourceType,
    AgentStatus,
    AgentVisibility,
    AutomationEventDispatch,
    AutomationScopeType,
    AutomationTargetType,
    AutomationTrigger,
    AutomationTriggerStatus,
    AutomationTriggerType,
    UserAgent,
)

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "org_migrations"
    / "versions"
    / "3c7f79637721_078_agent_automation_tables.py"
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


def test_agent_automation_tables_and_enums_match_the_product_contract() -> None:
    assert UserAgent.__tablename__ == "user_agents"
    assert AutomationTrigger.__tablename__ == "automation_triggers"
    assert AgentRun.__tablename__ == "agent_runs"
    assert AutomationEventDispatch.__tablename__ == "automation_event_dispatches"

    assert _enum_values(AgentSourceType) == {"user", "system"}
    assert _enum_values(AgentVisibility) == {"private", "organisation"}
    assert _enum_values(AgentStatus) == {"active", "paused", "archived"}
    assert _enum_values(AgentOutputMode) == {"text", "json"}
    assert _enum_values(AutomationTargetType) == {"agent", "workflow"}
    assert _enum_values(AutomationTriggerType) == {"schedule", "event"}
    assert _enum_values(AutomationScopeType) == {"owner"}
    assert _enum_values(AutomationTriggerStatus) == {"active", "paused", "archived"}
    assert _enum_values(AgentRunStatus) == {
        "pending",
        "running",
        "succeeded",
        "failed",
        "delivery_failed",
        "validation_failed",
        "cancelled",
    }


def test_automation_event_dispatch_database_rules_keep_recovery_idempotent() -> None:
    """Persist one terminal activation outcome per semantic source occurrence."""

    assert _column_names(AutomationEventDispatch) == {
        "dispatch_id",
        "organisation_id",
        "source_domain",
        "source_event_id",
        "event_type",
        "attempt_count",
        "last_attempted_at",
        "processed_at",
        "failed_at",
        "last_error",
        "created_at",
    }
    assert _check_names(AutomationEventDispatch) == {
        "ck_automation_event_dispatch_attempts",
        "ck_automation_event_dispatch_terminal_state",
    }

    unique = _index(AutomationEventDispatch, "uq_automation_event_dispatch_source")
    assert unique.unique is True
    assert _index_columns(unique) == (
        "organisation_id",
        "source_domain",
        "source_event_id",
        "event_type",
    )

    pending = _index(AutomationEventDispatch, "ix_automation_event_dispatch_pending")
    assert pending.unique is False
    assert _index_columns(pending) == ("created_at", "dispatch_id")
    assert _postgresql_where(pending) == "processed_at IS NULL AND failed_at IS NULL"


def test_user_agent_database_rules_stop_at_stable_definition_invariants() -> None:
    assert _column_names(UserAgent) == {
        "agent_id",
        "organisation_id",
        "owner_user_id",
        "source_type",
        "source_agent_id",
        "source_version",
        "revision",
        "visibility",
        "status",
        "agent_type",
        "name",
        "description",
        "prompt",
        "model_name",
        "model_config",
        "allowed_tools",
        "allowed_skills",
        "max_turns",
        "input_schema",
        "output_mode",
        "output_schema",
        "created_at",
        "updated_at",
    }
    # JSON shape, catalog membership, and cross-field product policy belong in
    # service validation; the database keeps only stable relational invariants.
    assert _check_names(UserAgent) == {
        "ck_user_agents_revision",
        "ck_user_agents_source_type",
        "ck_user_agents_visibility",
        "ck_user_agents_status",
        "ck_user_agents_output_mode",
    }

    owner_index = _index(UserAgent, "ix_user_agents_owner_status_updated")
    assert _index_columns(owner_index) == (
        "organisation_id",
        "owner_user_id",
        "status",
        "updated_at DESC",
    )

    for column_name in (
        "model_config",
        "allowed_tools",
        "allowed_skills",
        "input_schema",
        "output_schema",
    ):
        assert isinstance(UserAgent.__table__.c[column_name].type, JSONB)
    for column_name in ("model_config", "allowed_tools", "allowed_skills"):
        column = UserAgent.__table__.c[column_name]
        assert column.nullable is False
        assert column.server_default is not None

    assert "model_settings" in UserAgent.model_fields
    assert "model_config" not in UserAgent.model_fields


def test_trigger_database_rules_keep_routing_states_and_proven_access_paths() -> None:
    assert _column_names(AutomationTrigger) == {
        "trigger_id",
        "organisation_id",
        "owner_user_id",
        "name",
        "target_type",
        "target_id",
        "target_revision",
        "target_snapshot",
        "trigger_type",
        "source_domain",
        "event_type",
        "config_schema_version",
        "trigger_config",
        "filter_config",
        "scope_type",
        "scope_config",
        "task_config",
        "delivery_config",
        "config_hash",
        "status",
        "next_trigger_at",
        "last_triggered_at",
        "trigger_count",
        "consecutive_failures",
        "last_error",
        "created_at",
        "updated_at",
    }
    assert not AutomationTrigger.__table__.c.target_id.foreign_keys
    assert _check_names(AutomationTrigger) == {
        "ck_automation_triggers_target_type",
        "ck_automation_triggers_target_revision",
        "ck_automation_triggers_trigger_type",
        "ck_automation_triggers_config_schema_version",
        "ck_automation_triggers_status",
        "ck_automation_triggers_counts",
    }
    assert _index_names(AutomationTrigger) == {
        "ix_automation_triggers_target",
        "ix_automation_triggers_due_schedule",
        "ix_automation_triggers_active_event",
        "uq_automation_triggers_active_config",
    }

    target_index = _index(AutomationTrigger, "ix_automation_triggers_target")
    assert _index_columns(target_index) == (
        "organisation_id",
        "owner_user_id",
        "target_type",
        "target_id",
        "status",
        "updated_at DESC",
    )

    due_index = _index(AutomationTrigger, "ix_automation_triggers_due_schedule")
    assert _index_columns(due_index) == ("next_trigger_at", "trigger_id")
    assert _postgresql_where(due_index) == (
        "status = 'active' AND trigger_type = 'schedule' AND next_trigger_at IS NOT NULL"
    )

    event_index = _index(AutomationTrigger, "ix_automation_triggers_active_event")
    assert _index_columns(event_index) == (
        "organisation_id",
        "source_domain",
        "event_type",
        "trigger_id",
    )
    assert _postgresql_where(event_index) == "status = 'active' AND trigger_type = 'event'"

    duplicate_index = _index(AutomationTrigger, "uq_automation_triggers_active_config")
    assert duplicate_index.unique is True
    assert _index_columns(duplicate_index) == (
        "organisation_id",
        "owner_user_id",
        "target_type",
        "target_id",
        "config_hash",
    )
    assert _postgresql_where(duplicate_index) == "status <> 'archived'"

    for column_name in (
        "target_snapshot",
        "trigger_config",
        "filter_config",
        "scope_config",
        "task_config",
        "delivery_config",
    ):
        column = AutomationTrigger.__table__.c[column_name]
        assert isinstance(column.type, JSONB)
        assert column.nullable is False

    for column_name in (
        "trigger_config",
        "filter_config",
        "scope_config",
        "task_config",
        "delivery_config",
    ):
        column = AutomationTrigger.__table__.c[column_name]
        assert column.server_default is not None


def test_agent_run_database_rules_keep_history_claiming_and_idempotency() -> None:
    assert _column_names(AgentRun) == {
        "run_id",
        "organisation_id",
        "owner_user_id",
        "agent_id",
        "trigger_id",
        "agent_revision",
        "status",
        "idempotency_key",
        "source_domain",
        "event_type",
        "source_event_id",
        "scheduled_for",
        "trigger_snapshot",
        "agent_snapshot",
        "input_snapshot",
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
    agent_fk = next(iter(AgentRun.__table__.c.agent_id.foreign_keys))
    trigger_fk = next(iter(AgentRun.__table__.c.trigger_id.foreign_keys))
    assert agent_fk.target_fullname == "user_agents.agent_id"
    assert trigger_fk.target_fullname == "automation_triggers.trigger_id"
    assert agent_fk.ondelete == "RESTRICT"
    assert trigger_fk.ondelete == "RESTRICT"
    assert AgentRun.__table__.c.trigger_id.nullable is True

    assert _check_names(AgentRun) == {
        "ck_agent_runs_agent_revision",
        "ck_agent_runs_status",
        "ck_agent_runs_attempt_count",
        "ck_agent_runs_cost",
        "ck_agent_runs_latency_ms",
    }

    expected_indexes = {
        "ix_agent_runs_owner_created": (
            ("organisation_id", "owner_user_id", "created_at DESC"),
            None,
            False,
        ),
        "ix_agent_runs_agent_created": (
            ("agent_id", "organisation_id", "created_at DESC"),
            None,
            False,
        ),
        "ix_agent_runs_trigger_created": (
            ("trigger_id", "organisation_id", "created_at DESC"),
            None,
            False,
        ),
        "ix_agent_runs_pending_claim": (
            ("created_at", "run_id"),
            "status = 'pending'",
            False,
        ),
        "uq_agent_runs_idempotency": (
            ("organisation_id", "idempotency_key"),
            "idempotency_key IS NOT NULL",
            True,
        ),
    }
    assert _index_names(AgentRun) == set(expected_indexes)
    for name, (columns, predicate, unique) in expected_indexes.items():
        index = _index(AgentRun, name)
        assert _index_columns(index) == columns
        assert index.unique is unique
        if predicate is not None:
            assert _postgresql_where(index) == predicate

    for column_name in (
        "trigger_snapshot",
        "agent_snapshot",
        "input_snapshot",
        "output_json",
        "token_usage",
    ):
        assert isinstance(AgentRun.__table__.c[column_name].type, JSONB)


def test_agent_automation_migration_is_the_single_next_reversible_revision() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert 'revision: str = "3c7f79637721"' in source
    assert 'down_revision: str | None = "3c7f79637722"' in source
    assert re.findall(r'op\.create_table\(\s*"([^"]+)"', source) == [
        "user_agents",
        "automation_triggers",
        "agent_runs",
    ]
    assert re.findall(r'op\.drop_table\("([^"]+)"\)', source) == [
        "agent_runs",
        "automation_triggers",
        "user_agents",
    ]
    assert source.count('ondelete="RESTRICT"') == 2

    for name in (
        "ix_user_agents_owner_status_updated",
        "ix_automation_triggers_target",
        "ix_automation_triggers_due_schedule",
        "ix_automation_triggers_active_event",
        "uq_automation_triggers_active_config",
        "ix_agent_runs_owner_created",
        "ix_agent_runs_agent_created",
        "ix_agent_runs_trigger_created",
        "ix_agent_runs_pending_claim",
        "uq_agent_runs_idempotency",
    ):
        assert name in source

    for removed_name in (
        "ix_user_agents_created_at",
        "ix_user_agents_updated_at",
        "ix_user_agents_source_agent",
        "ix_automation_triggers_created_at",
        "ix_automation_triggers_updated_at",
        "ix_agent_runs_running_lease",
        "ck_user_agents_max_turns",
        "ck_user_agents_system_source",
        "ck_user_agents_json_output_schema",
        "ck_automation_triggers_scope_type",
        "ck_automation_triggers_schedule_fields",
        "ck_automation_triggers_event_fields",
    ):
        assert removed_name not in source
    assert "jsonb_typeof(" not in source

    assert '"user_reminders"' not in source
    assert '"workflows"' not in source

    versions_dir = MIGRATION_PATH.parent
    matching_revisions = [
        path.name
        for path in versions_dir.glob("*.py")
        if 'revision: str = "3c7f79637721"' in path.read_text(encoding="utf-8")
    ]
    assert matching_revisions == ["3c7f79637721_078_agent_automation_tables.py"]
