"""Report whether organisation tables satisfy the shared-row tenancy contract.

Release automation and operators can run the model audit without a database or
add the catalog audit against a configured PostgreSQL admin connection. The
database path is read-only and reports object names only, never credentials or
row contents.
"""

from __future__ import annotations

import argparse
import importlib
import json
import pkgutil
import sys
import warnings
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import MetaData, Table, text
from sqlalchemy.exc import SAWarning, SQLAlchemyError
from sqlalchemy.sql.schema import ForeignKeyConstraint, UniqueConstraint
from sqlmodel import SQLModel
from src.infra.database.sessions import master_session_scope

ORGANISATION_COLUMN = "organisation_id"
DEFAULT_SCHEMA = "tenant_data"
DEFAULT_RUNTIME_ROLE = "app_tenant_runtime"
# These paths identify physical objects in shared blob storage. Their global
# uniqueness is a cross-tenant safety rule, not an omitted tenant qualifier.
INTENTIONAL_GLOBAL_UNIQUE_RULES = {
    ("attachments", ("storage_path",)),
    ("deal_assets", ("storage_path",)),
    ("organisation_sources", ("storage_path",)),
}
REPORT_LIST_FIELDS = (
    "unclassified",
    "missing_organisation_id",
    "nullable_organisation_id",
    "unscoped_unique_rules",
    "unscoped_tenant_foreign_keys",
    "missing_tenant_indexes",
    "missing_deterministic_keys",
    "rls_not_enabled",
    "rls_not_forced",
    "missing_policies",
    "unsafe_runtime_roles",
)


def _empty_report(*, table_count: int) -> dict[str, Any]:
    report: dict[str, Any] = {"table_count": table_count}
    report.update({field: [] for field in REPORT_LIST_FIELDS})
    report["ok"] = True
    return report


def _finding_name(table: Table, constraint: Any) -> str:
    if constraint.name:
        return f"{table.name}.{constraint.name}"
    columns = "_".join(column.name for column in constraint.columns)
    return f"{table.name}.unnamed_{columns}"


def _has_deterministic_key(table: Table) -> bool:
    if table.primary_key.columns:
        return True

    for constraint in table.constraints:
        if not isinstance(constraint, UniqueConstraint):
            continue
        columns = tuple(constraint.columns)
        if columns and all(not column.nullable for column in columns):
            return True

    return any(
        index.unique
        and tuple(index.columns)
        and all(not column.nullable for column in index.columns)
        for index in table.indexes
    )


def _has_tenant_leading_index(table: Table) -> bool:
    index_column_sets = [tuple(index.columns) for index in table.indexes]
    index_column_sets.extend(
        tuple(constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    )
    if table.primary_key.columns:
        index_column_sets.append(tuple(table.primary_key.columns))
    return any(columns and columns[0].name == ORGANISATION_COLUMN for columns in index_column_sets)


def _referred_table_names(constraint: ForeignKeyConstraint) -> set[str]:
    # target_fullname avoids forcing SQLAlchemy to resolve external public
    # tables that are intentionally absent from organisation-only metadata.
    return {
        element.target_fullname.rsplit(".", 1)[0].rsplit(".", 1)[-1]
        for element in constraint.elements
    }


def _is_tenant_scoped_foreign_key(constraint: ForeignKeyConstraint) -> bool:
    """Require the child tenant key to map to the parent tenant key first."""

    elements = tuple(constraint.elements)
    return bool(
        elements
        and elements[0].parent.name == ORGANISATION_COLUMN
        and elements[0].target_fullname.rsplit(".", 1)[-1] == ORGANISATION_COLUMN
    )


def audit_metadata(
    metadata: MetaData,
    *,
    tenant_tables: Iterable[str],
    global_tables: Iterable[str],
    global_unique_rules: Iterable[tuple[str, tuple[str, ...]]] = (),
) -> dict[str, Any]:
    """Audit explicit tenant/global classifications in SQLAlchemy metadata.

    A caller must classify every table. Global tables are counted and checked
    for a deterministic key, but tenant identity, relationships, and indexes
    apply only to tenant data.
    """

    tenant_names = set(tenant_tables)
    global_names = set(global_tables)
    reviewed_global_uniques = set(global_unique_rules)
    overlap = tenant_names & global_names
    if overlap:
        names = ", ".join(sorted(overlap))
        raise ValueError(f"tables cannot be both tenant and global: {names}")

    tables = sorted(metadata.tables.values(), key=lambda table: table.name)
    present_names = {table.name for table in tables}
    unknown = (tenant_names | global_names) - present_names
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"classified tables are missing from metadata: {names}")

    report = _empty_report(table_count=len(tables))
    report["unclassified"] = sorted(present_names - tenant_names - global_names)

    for table in tables:
        if table.name not in tenant_names:
            if table.name in global_names and not _has_deterministic_key(table):
                report["missing_deterministic_keys"].append(table.name)
            continue

        if not _has_deterministic_key(table):
            report["missing_deterministic_keys"].append(table.name)

        if ORGANISATION_COLUMN not in table.c:
            report["missing_organisation_id"].append(table.name)
        elif table.c[ORGANISATION_COLUMN].nullable:
            report["nullable_organisation_id"].append(table.name)

        if not _has_tenant_leading_index(table):
            report["missing_tenant_indexes"].append(table.name)

        for constraint in table.constraints:
            ordered_columns = tuple(column.name for column in constraint.columns)
            local_columns = set(ordered_columns)
            if (
                isinstance(constraint, UniqueConstraint)
                and ORGANISATION_COLUMN not in local_columns
                and (table.name, ordered_columns) not in reviewed_global_uniques
            ):
                report["unscoped_unique_rules"].append(_finding_name(table, constraint))
            elif (
                isinstance(constraint, ForeignKeyConstraint)
                and _referred_table_names(constraint) & tenant_names
                and not _is_tenant_scoped_foreign_key(constraint)
            ):
                report["unscoped_tenant_foreign_keys"].append(_finding_name(table, constraint))

        report["unscoped_unique_rules"].extend(
            f"{table.name}.{index.name or 'unnamed_unique_index'}"
            for index in table.indexes
            if index.unique
            and ORGANISATION_COLUMN not in {column.name for column in index.columns}
            and (
                table.name,
                tuple(column.name for column in index.columns),
            )
            not in reviewed_global_uniques
        )

    for field in REPORT_LIST_FIELDS:
        report[field] = sorted(set(report[field]))
    report["ok"] = not any(report[field] for field in REPORT_LIST_FIELDS)
    return report


def _all_sqlmodel_subclasses() -> Iterable[type[Any]]:
    pending = list(SQLModel.__subclasses__())
    seen: set[type[Any]] = set()
    while pending:
        model = pending.pop()
        if model in seen:
            continue
        seen.add(model)
        yield model
        pending.extend(model.__subclasses__())


def _organisation_tables() -> list[Table]:
    """Load every table declared by the organisation model package."""

    import src.domain.models.organisation_tables as organisation_models

    package_name = organisation_models.__name__
    for module in pkgutil.walk_packages(
        organisation_models.__path__,
        f"{package_name}.",
    ):
        importlib.import_module(module.name)

    tables = {
        model.__table__
        for model in _all_sqlmodel_subclasses()
        if model.__module__.startswith(package_name) and hasattr(model, "__table__")
    }
    return sorted(tables, key=lambda table: table.name)


def build_model_report() -> dict[str, Any]:
    """Audit every organisation model as tenant-owned data."""

    tables = _organisation_tables()
    metadata = MetaData()
    for table in tables:
        table.to_metadata(metadata)
    return audit_metadata(
        metadata,
        tenant_tables={table.name for table in tables},
        global_tables=set(),
        global_unique_rules=INTENTIONAL_GLOBAL_UNIQUE_RULES,
    )


def _merge_reports(*reports: Mapping[str, Any]) -> dict[str, Any]:
    merged = _empty_report(
        table_count=max((int(report["table_count"]) for report in reports), default=0)
    )
    for field in REPORT_LIST_FIELDS:
        merged[field] = sorted({finding for report in reports for finding in report.get(field, [])})
    merged["ok"] = not any(merged[field] for field in REPORT_LIST_FIELDS)
    return merged


def _catalog_security_report(
    rows: Sequence[Mapping[str, Any]],
    *,
    runtime_role: str,
) -> dict[str, Any]:
    report = _empty_report(table_count=len(rows))
    for row in rows:
        table_name = str(row["table_name"])
        if not row["rls_enabled"]:
            report["rls_not_enabled"].append(table_name)
        if not row["rls_forced"]:
            report["rls_not_forced"].append(table_name)
        if not (
            row["has_permissive_policy"]
            and row["has_restrictive_policy"]
            and row["policies_have_read_and_write_checks"]
        ):
            report["missing_policies"].append(table_name)
        if row["owner_name"] == runtime_role:
            report["unsafe_runtime_roles"].append(f"{runtime_role}: owns {table_name}")
        if not row.get("runtime_has_table_privileges", False):
            report["unsafe_runtime_roles"].append(
                f"{runtime_role}: missing table privileges on {table_name}"
            )

    for field in REPORT_LIST_FIELDS:
        report[field] = sorted(set(report[field]))
    report["ok"] = not any(report[field] for field in REPORT_LIST_FIELDS)
    return report


def _tenant_function_report(
    row: Mapping[str, Any] | None,
    *,
    schema: str,
) -> dict[str, Any]:
    report = _empty_report(table_count=0)
    required = (
        "returns_uuid",
        "is_stable",
        "is_security_invoker",
        "has_safe_search_path",
        "reads_tenant_setting",
        "runtime_can_execute",
        "runtime_has_schema_usage",
    )
    if (
        row is None
        or not all(bool(row[field]) for field in required)
        or bool(row["public_can_execute"])
    ):
        report["missing_policies"] = [f"{schema}.current_organisation_id"]
    report["ok"] = not report["missing_policies"]
    return report


def _runtime_role_report(
    row: Mapping[str, Any] | None,
    *,
    runtime_role: str,
    schema_owner: str | None,
) -> dict[str, Any]:
    report = _empty_report(table_count=0)
    if row is None:
        report["unsafe_runtime_roles"] = [f"{runtime_role}: role not found"]
    else:
        unsafe_attributes = [
            attribute
            for attribute in (
                "rolsuper",
                "rolbypassrls",
                "rolcreatedb",
                "rolcreaterole",
            )
            if row[attribute]
        ]
        if unsafe_attributes:
            report["unsafe_runtime_roles"].append(f"{runtime_role}: {','.join(unsafe_attributes)}")
        if schema_owner == runtime_role:
            report["unsafe_runtime_roles"].append(f"{runtime_role}: owns tenant schema")
    report["ok"] = not report["unsafe_runtime_roles"]
    return report


def build_database_report(
    *,
    schema: str,
    runtime_role: str,
    expected_tables: Iterable[str],
) -> dict[str, Any]:
    """Audit deployed tables, policies, ownership, and the runtime role.

    The transaction is explicitly read-only so a future query change cannot
    accidentally turn this deployment gate into a production mutation.
    """

    expected_names = set(expected_tables)
    with master_session_scope() as session, session.begin():
        session.execute(text("SET TRANSACTION READ ONLY"))
        schema_owner = session.execute(
            text(
                """
                SELECT pg_get_userbyid(n.nspowner)
                  FROM pg_namespace n
                 WHERE n.nspname = :schema
                """
            ),
            {"schema": schema},
        ).scalar_one_or_none()
        if schema_owner is None:
            report = _empty_report(table_count=0)
            report["unclassified"] = [f"{schema}: schema not found"]
            report["ok"] = False
            return report

        table_rows = (
            session.execute(
                text(
                    """
                SELECT c.relname AS table_name,
                       c.relrowsecurity AS rls_enabled,
                       c.relforcerowsecurity AS rls_forced,
                       pg_get_userbyid(c.relowner) AS owner_name,
                       has_table_privilege(
                           runtime_role.oid, c.oid, 'SELECT'
                       )
                       AND has_table_privilege(
                           runtime_role.oid, c.oid, 'INSERT'
                       )
                       AND has_table_privilege(
                           runtime_role.oid, c.oid, 'UPDATE'
                       )
                       AND has_table_privilege(
                           runtime_role.oid, c.oid, 'DELETE'
                       ) AS runtime_has_table_privileges,
                       coalesce(
                           bool_or(
                               (0::oid = ANY(p.polroles)
                                OR runtime_role.oid = ANY(p.polroles))
                               AND p.polpermissive
                               AND p.polcmd = '*'
                               AND p.polname = 'tenant_access'
                           ),
                           false
                       ) AS has_permissive_policy,
                       coalesce(
                           bool_or(
                               (0::oid = ANY(p.polroles)
                                OR runtime_role.oid = ANY(p.polroles))
                               AND NOT p.polpermissive
                               AND p.polcmd = '*'
                               AND p.polname = 'tenant_isolation'
                           ),
                           false
                       ) AS has_restrictive_policy,
                       coalesce(
                           count(p.oid) = 2
                           AND bool_and(
                               p.polqual IS NOT NULL
                               AND p.polwithcheck IS NOT NULL
                           ) FILTER (WHERE p.oid IS NOT NULL),
                           false
                       ) AS policies_have_read_and_write_checks
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
             LEFT JOIN pg_roles runtime_role
                    ON runtime_role.rolname = :runtime_role
             LEFT JOIN pg_policy p ON p.polrelid = c.oid
                 WHERE n.nspname = :schema
                   AND c.relkind IN ('r', 'p')
                   AND c.relname <> 'alembic_version'
              GROUP BY c.oid, c.relname, c.relrowsecurity,
                       c.relforcerowsecurity, c.relowner, runtime_role.oid
              ORDER BY c.relname
                """
                ),
                {"schema": schema, "runtime_role": runtime_role},
            )
            .mappings()
            .all()
        )

        connection = session.connection()
        reflected = MetaData()
        # pgvector is installed in `extensions`, so SQLAlchemy cannot map the
        # qualified type during reflection. The columns remain auditable; only
        # this exact third-party type-resolution warning is irrelevant.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"Did not recognize type 'extensions\.vector'.*",
                category=SAWarning,
            )
            reflected.reflect(bind=connection, schema=schema)
        for table in tuple(reflected.tables.values()):
            if table.schema != schema or table.name == "alembic_version":
                reflected.remove(table)
        live_names = {table.name for table in reflected.tables.values()}
        structural_report = audit_metadata(
            reflected,
            tenant_tables=live_names & expected_names,
            global_tables=set(),
            global_unique_rules=INTENTIONAL_GLOBAL_UNIQUE_RULES,
        )
        structural_report["unclassified"] = sorted(
            set(structural_report["unclassified"])
            | (live_names - expected_names)
            | {f"{schema}.{name}: table not found" for name in expected_names - live_names}
        )
        structural_report["ok"] = not any(structural_report[field] for field in REPORT_LIST_FIELDS)

        role_row = (
            session.execute(
                text(
                    """
                SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole
                  FROM pg_roles
                 WHERE rolname = :runtime_role
                """
                ),
                {"runtime_role": runtime_role},
            )
            .mappings()
            .one_or_none()
        )
        function_row = None
        if role_row is not None:
            function_row = (
                session.execute(
                    text(
                        """
                    SELECT pg_get_function_result(procedure_record.oid) = 'uuid'
                               AS returns_uuid,
                           procedure_record.provolatile = 's' AS is_stable,
                           NOT procedure_record.prosecdef AS is_security_invoker,
                           coalesce(
                               procedure_record.proconfig
                                   @> ARRAY['search_path=pg_catalog'],
                               false
                           ) AS has_safe_search_path,
                           procedure_record.prosrc
                               LIKE '%current_setting(''app.current_organisation_id'', true)%'
                               AS reads_tenant_setting,
                           has_function_privilege(
                               :runtime_role,
                               procedure_record.oid,
                               'EXECUTE'
                           ) AS runtime_can_execute,
                           has_schema_privilege(
                               :runtime_role,
                               namespace_record.oid,
                               'USAGE'
                           ) AS runtime_has_schema_usage,
                           coalesce(
                               bool_or(
                                   acl_record.grantee = 0
                                   AND acl_record.privilege_type = 'EXECUTE'
                               ),
                               false
                           ) AS public_can_execute
                      FROM pg_proc procedure_record
                      JOIN pg_namespace namespace_record
                        ON namespace_record.oid = procedure_record.pronamespace
                 LEFT JOIN LATERAL aclexplode(
                               coalesce(
                                   procedure_record.proacl,
                                   acldefault('f', procedure_record.proowner)
                               )
                           ) acl_record ON true
                     WHERE namespace_record.nspname = :schema
                       AND procedure_record.proname = 'current_organisation_id'
                       AND procedure_record.pronargs = 0
                  GROUP BY procedure_record.oid, namespace_record.oid
                    """
                    ),
                    {"schema": schema, "runtime_role": runtime_role},
                )
                .mappings()
                .one_or_none()
            )

        return _merge_reports(
            structural_report,
            _catalog_security_report(table_rows, runtime_role=runtime_role),
            _tenant_function_report(function_row, schema=schema),
            _runtime_role_report(
                role_row,
                runtime_role=runtime_role,
                schema_owner=schema_owner,
            ),
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit shared-row tenancy model and PostgreSQL invariants."
    )
    parser.add_argument("--models", action="store_true")
    parser.add_argument("--database", action="store_true")
    parser.add_argument("--schema", default=DEFAULT_SCHEMA)
    parser.add_argument("--runtime-role", default=DEFAULT_RUNTIME_ROLE)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run requested audits and return 0 ready, 1 findings, or 2 unavailable."""

    parser = _parser()
    args = parser.parse_args(argv)
    if not args.models and not args.database:
        parser.print_usage(sys.stderr)
        return 2

    try:
        model_report = build_model_report()
        report = model_report
        if args.database:
            database_report = build_database_report(
                schema=args.schema,
                runtime_role=args.runtime_role,
                expected_tables=_organisation_table_names(),
            )
            report = _merge_reports(model_report, database_report)
    except (SQLAlchemyError, OSError, RuntimeError, ValueError) as exc:
        print(f"readiness audit unavailable: {exc.__class__.__name__}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("ready" if report["ok"] else "not ready")
        for field in REPORT_LIST_FIELDS:
            for finding in report[field]:
                print(f"{field}: {finding}")
    return 0 if report["ok"] else 1


def _organisation_table_names() -> set[str]:
    return {table.name for table in _organisation_tables()}


if __name__ == "__main__":
    raise SystemExit(main())
