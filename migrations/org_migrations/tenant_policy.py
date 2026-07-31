"""Shared forced-RLS convention for organisation migrations."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import sqlalchemy as sa
from migrations.org_migrations.tenant_identity import (
    validate_migration_schema_name,
)

TENANT_CONTEXT_FUNCTION = "current_organisation_id"
TENANT_CONTEXT_SETTING = "app.current_organisation_id"
TENANT_COLUMN = "organisation_id"
TENANT_PERMISSIVE_POLICY = "tenant_access"
TENANT_RESTRICTIVE_POLICY = "tenant_isolation"
TENANT_RUNTIME_ROLE = "app_tenant_runtime"


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _qualified(schema_name: str, object_name: str) -> str:
    return f"{_quote_identifier(schema_name)}.{_quote_identifier(object_name)}"


def tenant_table_names(connection: Any, *, schema_name: str) -> tuple[str, ...]:
    """Return physical tenant tables that carry the required tenant identity."""

    rows = connection.execute(
        sa.text(
            """
            SELECT class_record.relname
              FROM pg_class class_record
              JOIN pg_namespace namespace_record
                ON namespace_record.oid = class_record.relnamespace
              JOIN pg_attribute attribute
                ON attribute.attrelid = class_record.oid
               AND attribute.attname = 'organisation_id'
               AND NOT attribute.attisdropped
             WHERE namespace_record.nspname = :schema_name
               AND class_record.relkind IN ('r', 'p')
             ORDER BY class_record.relname
            """
        ),
        {"schema_name": schema_name},
    )
    return tuple(str(row[0]) for row in rows)


def _tenant_predicate(schema_name: str) -> str:
    return (
        f"{_quote_identifier(TENANT_COLUMN)} = "
        f"{_qualified(schema_name, TENANT_CONTEXT_FUNCTION)}()"
    )


def protect_tenant_table(
    connection: Any,
    *,
    schema_name: str,
    table_name: str,
    runtime_role: str = TENANT_RUNTIME_ROLE,
) -> None:
    """Apply the complete tenant policy convention to one table."""

    table = _qualified(schema_name, table_name)
    role = _quote_identifier(runtime_role)
    predicate = _tenant_predicate(schema_name)
    permissive = _quote_identifier(TENANT_PERMISSIVE_POLICY)
    restrictive = _quote_identifier(TENANT_RESTRICTIVE_POLICY)

    statements = (
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {permissive} ON {table}",
        f"DROP POLICY IF EXISTS {restrictive} ON {table}",
        (
            f"CREATE POLICY {permissive} ON {table} AS PERMISSIVE "
            f"FOR ALL TO {role} USING ({predicate}) WITH CHECK ({predicate})"
        ),
        (
            f"CREATE POLICY {restrictive} ON {table} AS RESTRICTIVE "
            f"FOR ALL TO {role} USING ({predicate}) WITH CHECK ({predicate})"
        ),
    )
    for statement in statements:
        connection.execute(sa.text(statement))


def install_tenant_rls(
    connection: Any,
    *,
    schema_name: str,
    runtime_role: str = TENANT_RUNTIME_ROLE,
    table_names: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """Install the fail-closed context function and protect every tenant table."""

    schema_name = validate_migration_schema_name(schema_name)
    validate_migration_schema_name(runtime_role)
    function = _qualified(schema_name, TENANT_CONTEXT_FUNCTION)
    role = _quote_identifier(runtime_role)
    tables = (
        tuple(table_names)
        if table_names is not None
        else tenant_table_names(connection, schema_name=schema_name)
    )
    if not tables:
        raise RuntimeError(f"schema {schema_name!r} has no tenant tables")

    connection.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION {function}()
            RETURNS uuid
            LANGUAGE sql
            STABLE
            PARALLEL SAFE
            SECURITY INVOKER
            SET search_path = pg_catalog
            AS $$
                SELECT NULLIF(
                    current_setting('{TENANT_CONTEXT_SETTING}', true),
                    ''
                )::uuid
            $$
            """
        )
    )
    connection.execute(
        sa.text(f"REVOKE ALL ON FUNCTION {function}() FROM PUBLIC")
    )
    connection.execute(
        sa.text(f"GRANT EXECUTE ON FUNCTION {function}() TO {role}")
    )
    runtime_grants = (
        f"GRANT USAGE ON SCHEMA {_quote_identifier(schema_name)} TO {role}",
        (
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
            f"IN SCHEMA {_quote_identifier(schema_name)} TO {role}"
        ),
        (
            "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES "
            f"IN SCHEMA {_quote_identifier(schema_name)} TO {role}"
        ),
        (
            "ALTER DEFAULT PRIVILEGES IN SCHEMA "
            f"{_quote_identifier(schema_name)} "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES "
            f"TO {role}"
        ),
        (
            "ALTER DEFAULT PRIVILEGES IN SCHEMA "
            f"{_quote_identifier(schema_name)} "
            "GRANT USAGE, SELECT, UPDATE ON SEQUENCES "
            f"TO {role}"
        ),
    )
    for statement in runtime_grants:
        connection.execute(sa.text(statement))

    for table_name in tables:
        validate_migration_schema_name(table_name)
        protect_tenant_table(
            connection,
            schema_name=schema_name,
            table_name=table_name,
            runtime_role=runtime_role,
        )
    return tables


def remove_tenant_rls(
    connection: Any,
    *,
    schema_name: str,
    table_names: Iterable[str] | None = None,
) -> None:
    """Remove only the policies and function installed by this convention."""

    schema_name = validate_migration_schema_name(schema_name)
    tables = (
        tuple(table_names)
        if table_names is not None
        else tenant_table_names(connection, schema_name=schema_name)
    )
    for table_name in tables:
        validate_migration_schema_name(table_name)
        table = _qualified(schema_name, table_name)
        connection.execute(
            sa.text(
                f"DROP POLICY IF EXISTS "
                f"{_quote_identifier(TENANT_RESTRICTIVE_POLICY)} ON {table}"
            )
        )
        connection.execute(
            sa.text(
                f"DROP POLICY IF EXISTS "
                f"{_quote_identifier(TENANT_PERMISSIVE_POLICY)} ON {table}"
            )
        )
        connection.execute(
            sa.text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        )
        connection.execute(
            sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        )

    connection.execute(
        sa.text(
            f"DROP FUNCTION IF EXISTS "
            f"{_qualified(schema_name, TENANT_CONTEXT_FUNCTION)}()"
        )
    )
