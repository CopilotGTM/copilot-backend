"""Regression tests for test 087 ai native automation; these cases lock the backend contract used by the changed feature."""
from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
VERSIONS = ROOT / "migrations" / "org_migrations" / "versions"
MIGRATION_PATH = VERSIONS / "3c7f79637712_087_ai_native_automation.py"


def _load_revision() -> Any:
    assert MIGRATION_PATH.exists(), f"missing migration: {MIGRATION_PATH.name}"
    spec = importlib.util.spec_from_file_location("org_migration_087", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_087_is_the_single_head_after_retired_table_cleanup() -> None:
    """Verify the contract that revision 087 is the single head after retired table cleanup."""
    migration = _load_revision()

    assert migration.revision == "3c7f79637712"
    assert migration.down_revision == "3c7f79637713"
    assert [path.name for path in VERSIONS.glob("*_087_*.py")] == [MIGRATION_PATH.name]


def test_upgrade_replaces_the_three_runtime_tables_and_reinstalls_rls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the contract that upgrade replaces the three runtime tables and reinstalls rls."""
    migration = _load_revision()
    dropped: list[str] = []
    created: list[str] = []
    indexes: list[str] = []
    index_definitions: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
    protected: list[tuple[Any, str, str]] = []
    bind = object()

    def record_index(name: str, *args: Any, **kwargs: Any) -> None:
        indexes.append(name)
        index_definitions[name] = (args, kwargs)

    monkeypatch.setattr(migration, "_migration_schema", lambda: "tenant_test")
    monkeypatch.setattr(migration, "_assert_runtime_tables_empty", lambda: None)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(migration.op, "drop_table", lambda name: dropped.append(name))
    monkeypatch.setattr(migration.op, "add_column", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        migration.op,
        "create_foreign_key",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        migration.op,
        "create_table",
        lambda name, *_args, **_kwargs: created.append(name),
    )
    monkeypatch.setattr(
        migration.op,
        "create_index",
        record_index,
    )
    monkeypatch.setattr(
        migration,
        "protect_tenant_table",
        lambda connection, *, schema_name, table_name: protected.append(
            (connection, schema_name, table_name)
        ),
    )

    migration.upgrade()

    assert dropped == [
        "agent_runs",
        "automation_event_dispatches",
        "automation_triggers",
    ]
    assert created == [
        "automation_triggers",
        "automation_event_dispatches",
        "agent_runs",
    ]
    assert indexes == [
        "ix_automation_triggers_target",
        "ix_automation_triggers_due_schedule",
        "ix_automation_triggers_active_event_keys",
        "uq_automation_triggers_active_config",
        "uq_automation_triggers_active_org_service_config",
        "uq_automation_event_dispatch_idempotency",
        "ix_automation_event_dispatch_direct_trigger",
        "ix_automation_event_dispatch_pending",
        "ix_automation_event_dispatch_event_created",
        "ix_agent_runs_creator_created",
        "ix_agent_runs_principal_created",
        "ix_agent_runs_agent_created",
        "ix_agent_runs_trigger_created",
        "ix_agent_runs_event",
        "ix_agent_runs_causation",
        "ix_agent_runs_pending_claim",
        "uq_agent_runs_trigger_event",
        "ix_crm_mutation_events_causation",
    ]
    shared_index_args, shared_index_kwargs = index_definitions[
        "uq_automation_triggers_active_org_service_config"
    ]
    assert shared_index_args == (
        "automation_triggers",
        ["organisation_id", "target_type", "target_id", "config_hash"],
    )
    assert shared_index_kwargs["unique"] is True
    assert str(shared_index_kwargs["postgresql_where"]) == (
        "status <> 'archived' AND execution_principal_type = 'organisation_service'"
    )
    assert protected == [
        (bind, "tenant_test", "automation_triggers"),
        (bind, "tenant_test", "automation_event_dispatches"),
        (bind, "tenant_test", "agent_runs"),
    ]


def test_upgrade_declares_composite_tenant_relationships_and_json_storage() -> None:
    """Verify the contract that upgrade declares composite tenant relationships and json storage."""
    source = inspect.getsource(_load_revision().upgrade)

    for name in (
        "fk_automation_event_dispatches_direct_trigger_id_org",
        "fk_agent_runs_agent_id_org",
        "fk_agent_runs_trigger_id_org",
        "fk_agent_runs_event_id_org",
        "fk_agent_runs_caused_by_run_id_org",
        "uq_automation_triggers_org_trigger_id",
        "uq_automation_event_dispatches_org_event_id",
        "uq_agent_runs_org_run_id",
        "fk_crm_mutation_events_caused_by_run_id_org",
        "ck_crm_mutation_events_causation_depth",
        "ix_automation_event_dispatch_direct_trigger",
        "ix_agent_runs_event",
        "ix_agent_runs_causation",
        "ix_crm_mutation_events_causation",
    ):
        assert name in source
    assert "postgresql.ARRAY(sa.Text())" in source
    assert source.count("postgresql.JSONB") >= 15
    assert source.count('ondelete="RESTRICT"') == 6


def test_runtime_table_replacement_refuses_existing_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the contract that runtime table replacement refuses existing data."""
    migration = _load_revision()

    class Result:
        """Represent the Result test double used by the surrounding regression cases."""
        def __init__(self, value: bool) -> None:
            self.value = value

        def scalar(self) -> bool:
            """Expose the scalar operation expected by the Result test double."""
            return self.value

    class Bind:
        """Represent the Bind test double used by the surrounding regression cases."""
        def execute(self, statement: Any) -> Result:
            """Build the execute fixture used by the surrounding regression cases."""
            return Result("automation_event_dispatches" in str(statement))

    monkeypatch.setattr(migration, "_migration_schema", lambda: "tenant_test")
    monkeypatch.setattr(migration.op, "get_bind", Bind)

    with pytest.raises(RuntimeError, match="automation_event_dispatches is not empty"):
        migration._assert_runtime_tables_empty()


def test_downgrade_restores_revision_086_schema_and_rls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the contract that downgrade restores revision 086 schema and rls."""
    migration = _load_revision()
    dropped_constraints: list[tuple[str, str, str | None]] = []
    dropped_indexes: list[tuple[str, str | None]] = []
    dropped_tables: list[str] = []
    dropped_columns: list[tuple[str, str]] = []
    created_tables: dict[str, list[Any]] = {}
    created_indexes: list[str] = []
    protected: list[tuple[Any, str, str]] = []
    bind = object()

    monkeypatch.setattr(migration, "_migration_schema", lambda: "tenant_test")
    monkeypatch.setattr(migration, "_assert_runtime_tables_empty", lambda: None)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda name, table_name, **kwargs: dropped_constraints.append(
            (name, table_name, kwargs.get("type_"))
        ),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_index",
        lambda name, **kwargs: dropped_indexes.append((name, kwargs.get("table_name"))),
    )
    monkeypatch.setattr(migration.op, "drop_table", dropped_tables.append)
    monkeypatch.setattr(
        migration.op,
        "drop_column",
        lambda table_name, column_name: dropped_columns.append((table_name, column_name)),
    )
    monkeypatch.setattr(
        migration.op,
        "create_table",
        lambda name, *items, **_kwargs: created_tables.setdefault(name, list(items)),
    )
    monkeypatch.setattr(
        migration.op,
        "create_index",
        lambda name, *_args, **_kwargs: created_indexes.append(name),
    )
    monkeypatch.setattr(
        migration,
        "protect_tenant_table",
        lambda connection, *, schema_name, table_name: protected.append(
            (connection, schema_name, table_name)
        ),
    )

    migration.downgrade()

    assert dropped_constraints == [
        (
            "fk_crm_mutation_events_caused_by_run_id_org",
            "crm_mutation_events",
            "foreignkey",
        ),
        (
            "ck_crm_mutation_events_causation_depth",
            "crm_mutation_events",
            "check",
        ),
    ]
    assert dropped_indexes == [("ix_crm_mutation_events_causation", "crm_mutation_events")]
    assert dropped_tables == [
        "agent_runs",
        "automation_event_dispatches",
        "automation_triggers",
    ]
    assert list(created_tables) == [
        "automation_triggers",
        "automation_event_dispatches",
        "agent_runs",
    ]

    column_names = {
        table_name: [item.name for item in items if isinstance(item, migration.sa.Column)]
        for table_name, items in created_tables.items()
    }
    assert column_names["automation_triggers"] == [
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
    ]
    assert column_names["automation_event_dispatches"] == [
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
    ]
    assert column_names["agent_runs"] == [
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
    ]
    constraint_names = {
        item.name
        for items in created_tables.values()
        for item in items
        if isinstance(item, migration.sa.Constraint)
    }
    assert {
        "uq_automation_triggers_org_trigger_id",
        "fk_agent_runs_agent_id_org",
        "fk_agent_runs_trigger_id_org",
    } <= constraint_names
    assert created_indexes == [
        "ix_automation_triggers_target",
        "ix_automation_triggers_due_schedule",
        "ix_automation_triggers_active_event",
        "uq_automation_triggers_active_config",
        "uq_automation_event_dispatch_source",
        "ix_automation_event_dispatch_pending",
        "ix_agent_runs_owner_created",
        "ix_agent_runs_agent_created",
        "ix_agent_runs_trigger_created",
        "ix_agent_runs_pending_claim",
        "uq_agent_runs_idempotency",
    ]
    assert dropped_columns == [
        ("crm_mutation_events", "causation_depth"),
        ("crm_mutation_events", "caused_by_run_id"),
        ("crm_mutation_events", "automation_source_mode"),
        ("crm_mutation_events", "record_snapshot"),
    ]
    assert protected == [
        (bind, "tenant_test", "automation_triggers"),
        (bind, "tenant_test", "automation_event_dispatches"),
        (bind, "tenant_test", "agent_runs"),
    ]
