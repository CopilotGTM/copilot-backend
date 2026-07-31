from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from migrations.org_migrations.tenant_policy import (
    TENANT_CONTEXT_FUNCTION,
    install_tenant_rls,
)


class RecordingConnection:
    def __init__(self, table_names: tuple[str, ...]) -> None:
        self.table_names = table_names
        self.statements: list[str] = []

    def execute(
        self,
        statement: Any,
        parameters: dict[str, object] | None = None,
    ) -> Any:
        sql = str(statement)
        self.statements.append(sql)
        if "FROM pg_class" in sql:
            return [(name,) for name in self.table_names]
        return None


def _load_revision() -> Any:
    path = (
        Path(__file__).resolve().parents[3]
        / "migrations"
        / "org_migrations"
        / "versions"
        / "3c7f79637714_085_enable_forced_rls.py"
    )
    spec = importlib.util.spec_from_file_location("org_migration_085", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_policy_helper_installs_fail_closed_forced_rls() -> None:
    connection = RecordingConnection(("parents", "children"))

    install_tenant_rls(
        connection,
        schema_name="tenant_data",
        runtime_role="app_tenant_runtime",
    )

    sql = "\n".join(connection.statements)
    assert (
        'CREATE OR REPLACE FUNCTION "tenant_data"."current_organisation_id"()'
        in sql
    )
    assert "current_setting('app.current_organisation_id', true)" in sql
    assert "RETURNS uuid" in sql
    assert "SECURITY INVOKER" in sql
    assert 'REVOKE ALL ON FUNCTION "tenant_data"."current_organisation_id"() FROM PUBLIC' in sql
    assert (
        'GRANT EXECUTE ON FUNCTION "tenant_data"."current_organisation_id"() '
        'TO "app_tenant_runtime"'
    ) in sql
    assert 'GRANT USAGE ON SCHEMA "tenant_data" TO "app_tenant_runtime"' in sql
    assert (
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
        'IN SCHEMA "tenant_data" TO "app_tenant_runtime"'
    ) in sql
    assert (
        "ALTER DEFAULT PRIVILEGES IN SCHEMA "
        '"tenant_data" GRANT SELECT, INSERT, UPDATE, DELETE '
        'ON TABLES TO "app_tenant_runtime"'
    ) in sql

    for table_name in ("parents", "children"):
        table = f'"tenant_data"."{table_name}"'
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in sql
        assert (
            f'CREATE POLICY "tenant_access" ON {table} AS PERMISSIVE '
            'FOR ALL TO "app_tenant_runtime"'
        ) in sql
        assert (
            f'CREATE POLICY "tenant_isolation" ON {table} AS RESTRICTIVE '
            'FOR ALL TO "app_tenant_runtime"'
        ) in sql
        predicate = (
            '"organisation_id" = '
            f'"tenant_data"."{TENANT_CONTEXT_FUNCTION}"()'
        )
        assert sql.count(f"USING ({predicate})") == 4
        assert sql.count(f"WITH CHECK ({predicate})") == 4


def test_policy_helper_discovers_only_base_tables_with_tenant_identity() -> None:
    connection = RecordingConnection(("parents",))

    install_tenant_rls(
        connection,
        schema_name="tenant_data",
        runtime_role="app_tenant_runtime",
    )

    inventory_sql = connection.statements[0]
    assert "attribute.attname = 'organisation_id'" in inventory_sql
    assert "class_record.relkind IN ('r', 'p')" in inventory_sql
    assert "namespace_record.nspname = :schema_name" in inventory_sql


def test_revision_uses_the_current_head_and_shared_policy_helper() -> None:
    revision = _load_revision()

    assert revision.revision == "3c7f79637714"
    assert revision.down_revision == "3c7f79637715"
    assert revision.upgrade.__doc__
    assert revision.downgrade.__doc__
