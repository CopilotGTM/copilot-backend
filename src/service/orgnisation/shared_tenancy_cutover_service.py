"""Exact-organisation cutover and legacy database cleanup for `tenant_data`.

The maintenance CLI owns session creation and immutable evidence output. This
service owns one PostgreSQL transaction: it selects the UUID-bound legacy
schema, proves source/target compatibility, copies in foreign-key order, and
validates the committed shape without exposing row payloads. Other populated
legacy schemas are left untouched. After runtime smoke evidence exists, it can
also remove only the selected source schema and role.
"""

# ruff: noqa: S608

from __future__ import annotations

import hashlib
import time
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from migrations.org_migrations.tenant_identity import (
    legacy_schema_organisation_id,
    validate_migration_schema_name,
)
from sqlalchemy import (
    ForeignKeyConstraint,
    MetaData,
    Table,
    and_,
    exists,
    func,
    select,
    text,
)
from sqlalchemy import (
    cast as sql_cast,
)
from sqlalchemy.exc import SAWarning, SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.util import topological
from src.infra.database.tenant_runtime_readiness import (
    TenantRuntimeReadinessError,
    validate_tenant_runtime_catalog,
)
from src.infra.utils.resource_naming import get_role_name, get_schema_name

CUTOVER_LOCK_NAMESPACE = "copilotgtm:shared-tenancy-cutover"
CUTOVER_TOOL_VERSION = "1.0.0"


class CutoverError(RuntimeError):
    """A fail-closed cutover refusal with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class _SchemaInventory:
    schema: str
    revision: str
    metadata: MetaData
    tables: dict[str, Table]


@dataclass(frozen=True)
class _Precheck:
    organisation_id: UUID
    source: _SchemaInventory
    destination: _SchemaInventory
    table_order: tuple[str, ...]
    deferred_foreign_keys: tuple[tuple[str, str], ...]
    source_counts: dict[str, int]


class SharedTenancyCutoverService:
    """Copy one selected organisation and remove only its exact database resources."""

    def __init__(
        self,
        session: Session,
        *,
        expected_revision: str,
        destination_schema: str = "tenant_data",
        runtime_role: str = "app_tenant_runtime",
    ) -> None:
        self._session = session
        self._expected_revision = expected_revision.strip()
        self._destination_schema = validate_migration_schema_name(destination_schema)
        self._runtime_role = validate_migration_schema_name(runtime_role)
        if not self._expected_revision:
            raise ValueError("expected_revision is required")

    def precheck(self, organisation_id: UUID) -> dict[str, Any]:
        """Run the complete discovery and compatibility audit without mutation."""

        started = time.perf_counter()
        try:
            with self._session.begin():
                self._session.execute(text("SET TRANSACTION READ ONLY"))
                # Large deterministic table digests need a bounded maintenance
                # ceiling above the normal runtime query timeout.
                self._session.execute(text("SET LOCAL statement_timeout = '30min'"))
                precheck = self._run_precheck(organisation_id)
                report = self._base_report(precheck, operation="precheck")
                report["tables"] = self._precheck_table_reports(precheck)
                report["mutation_performed"] = False
                report["validation"] = {
                    "source_identity": True,
                    "destination_empty": True,
                    "schema_compatibility": True,
                    "rls": True,
                }
                report["result"] = "ready"
                report["timings_ms"] = {
                    "total": _elapsed_ms(started),
                }
                return report
        except CutoverError:
            raise
        except SQLAlchemyError as exc:
            raise CutoverError(
                "precheck_failed",
                "PostgreSQL precheck failed without changing data",
            ) from exc

    def copy_and_validate(
        self,
        organisation_id: UUID,
        *,
        writers_stopped_evidence: str,
        backup_reference: str,
        apply: bool,
        replace_existing: bool = False,
    ) -> dict[str, Any]:
        """Copy and validate, optionally replacing this tenant's destination rows."""

        if not apply:
            raise CutoverError("apply_required", "--apply is required")
        writers_stopped_evidence = writers_stopped_evidence.strip()
        if not writers_stopped_evidence:
            raise CutoverError(
                "writers_stopped_evidence_required",
                "writer-shutdown evidence is required",
            )
        backup_reference = backup_reference.strip()
        if not backup_reference:
            raise CutoverError(
                "backup_reference_required",
                "a backup reference is required",
            )

        started = time.perf_counter()
        current_table: str | None = None
        try:
            with self._session.begin():
                if replace_existing:
                    # Predicate locking makes concurrent writes abort this
                    # destructive replacement instead of creating a mixed snapshot.
                    self._session.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
                # Full-row JSON hashing can exceed the API query timeout on a
                # large tenant; keep a bounded maintenance-only ceiling.
                self._session.execute(text("SET LOCAL statement_timeout = '30min'"))
                precheck_started = time.perf_counter()
                precheck = self._run_precheck(
                    organisation_id,
                    require_empty_destination=not replace_existing,
                )
                precheck_ms = _elapsed_ms(precheck_started)
                self._set_tenant_context(organisation_id)

                sentinel_before = self._sentinel_snapshot(precheck.destination, organisation_id)
                if replace_existing:
                    self._replace_destination_rows(precheck)
                copy_started = time.perf_counter()
                for current_table in precheck.table_order:
                    self._copy_table(
                        precheck.source.tables[current_table],
                        precheck.destination.tables[current_table],
                        organisation_id,
                        deferred_columns=self._deferred_columns(
                            precheck,
                            current_table,
                        ),
                    )
                self._restore_deferred_foreign_keys(precheck)
                copy_ms = _elapsed_ms(copy_started)

                validation_started = time.perf_counter()
                table_reports = self._validate_copied_tables(precheck)
                self._validate_foreign_keys(precheck.destination, organisation_id)
                self._validate_uniqueness(precheck.destination, organisation_id)
                self._validate_destination_rls()
                sentinel_after = self._sentinel_snapshot(
                    precheck.destination,
                    organisation_id,
                )
                if sentinel_before != sentinel_after:
                    raise CutoverError(
                        "sentinel_changed",
                        "rows belonging to another organisation changed",
                    )

                report = self._base_report(precheck, operation="copy-and-validate")
                report.update(
                    {
                        "writers_stopped_evidence": writers_stopped_evidence,
                        "backup_reference": backup_reference,
                        "mutation_performed": True,
                        "destination_replaced": replace_existing,
                        "tables": table_reports,
                        "sentinel": {
                            name: {
                                "row_count": snapshot[0],
                                "row_sha256": snapshot[1],
                            }
                            for name, snapshot in sentinel_after.items()
                        },
                        "validation": {
                            "counts": True,
                            "primary_key_sets": True,
                            "checksums": True,
                            "foreign_keys": True,
                            "uniqueness": True,
                            "rls": True,
                            "sentinel_unchanged": True,
                        },
                        "result": "copied_and_validated",
                        "timings_ms": {
                            "precheck": precheck_ms,
                            "copy": copy_ms,
                            "validation": _elapsed_ms(validation_started),
                            "total": _elapsed_ms(started),
                        },
                    }
                )
            return report
        except CutoverError:
            raise
        except SQLAlchemyError as exc:
            table_context = f" for table {current_table}" if current_table else ""
            raise CutoverError(
                "copy_failed",
                f"copy transaction failed{table_context}; destination changes rolled back",
            ) from exc

    def delete_source_database_resources(
        self,
        organisation_id: UUID,
        *,
        source_schema: str,
        source_role: str,
        apply: bool,
    ) -> dict[str, Any]:
        """Delete only the exact legacy schema and role bound to one organisation."""

        if not apply:
            raise CutoverError("apply_required", "--apply is required")
        if (
            not isinstance(organisation_id, UUID)
            or source_schema != get_schema_name(organisation_id)
            or source_role != get_role_name(organisation_id)
        ):
            raise CutoverError(
                "exact_source_target_required",
                "source schema and role must exactly match the organisation",
            )
        return self._delete_source_database_resources(
            organisation_id,
            source_schema=source_schema,
            source_role=source_role,
        )

    def _run_precheck(
        self,
        organisation_id: UUID,
        *,
        require_empty_destination: bool = True,
    ) -> _Precheck:
        if not isinstance(organisation_id, UUID):
            raise CutoverError("invalid_organisation_id", "organisation_id must be a UUID")
        self._acquire_lock(organisation_id)
        # Cutover must inspect malformed and other-tenant rows that RLS would hide.
        # PostgreSQL fails the later reads when the maintenance role cannot bypass RLS.
        self._session.execute(text("SET LOCAL row_security = off"))
        self._validate_master_organisation(organisation_id)

        source_schema = get_schema_name(organisation_id)
        if not self._schema_has_rows(source_schema):
            raise CutoverError(
                "source_identity_mismatch",
                "the requested organisation has no populated canonical source schema",
            )
        try:
            encoded_id = legacy_schema_organisation_id(source_schema)
        except ValueError as exc:
            raise CutoverError(
                "source_identity_mismatch",
                "source schema is not a canonical organisation schema",
            ) from exc
        if encoded_id != organisation_id:
            raise CutoverError(
                "source_identity_mismatch",
                "source schema UUID does not match the requested organisation",
            )

        source = self._inventory_schema(source_schema)
        destination = self._inventory_schema(self._destination_schema)
        self._validate_revisions(source, destination)
        self._validate_compatible_schemas(source, destination)
        self._validate_destination_rls()

        source_counts = self._validate_source_identity(source, organisation_id)
        if require_empty_destination:
            self._validate_destination_empty(destination, organisation_id)
        table_order, deferred_foreign_keys = self._copy_plan(destination)
        return _Precheck(
            organisation_id=organisation_id,
            source=source,
            destination=destination,
            table_order=table_order,
            deferred_foreign_keys=deferred_foreign_keys,
            source_counts=source_counts,
        )

    def _delete_source_database_resources(
        self,
        organisation_id: UUID,
        *,
        source_schema: str,
        source_role: str,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        schema_existed = False
        role_existed = False
        try:
            with self._session.begin():
                self._acquire_lock(organisation_id)
                self._session.execute(text("SET LOCAL lock_timeout = '5s'"))
                schema_existed = self._schema_exists(source_schema)
                role_existed = self._role_exists(source_role)

                if self._source_is_active(source_schema, source_role):
                    raise CutoverError(
                        "active_source_sessions",
                        "legacy source sessions or relation locks are still active",
                    )
                dependencies = self._external_source_dependencies(source_schema)
                if dependencies:
                    raise CutoverError(
                        "source_dependencies",
                        "objects outside the source schema still depend on it",
                    )

                # Schema first removes objects owned by the legacy role. Dropping
                # both in one transaction restores the schema if role deletion fails.
                if schema_existed:
                    self._session.execute(
                        text(f"DROP SCHEMA {_quote_identifier(source_schema)} CASCADE")
                    )
                if role_existed:
                    self._session.execute(text(f"DROP ROLE {_quote_identifier(source_role)}"))

                schema_absent = not self._schema_exists(source_schema)
                role_absent = not self._role_exists(source_role)
                if not schema_absent or not role_absent:
                    raise CutoverError(
                        "source_deletion_verification_failed",
                        "source schema or role remains after deletion",
                    )
            return {
                "organisation_id": str(organisation_id),
                "source_schema": source_schema,
                "source_role": source_role,
                "schema_existed": schema_existed,
                "role_existed": role_existed,
                "schema_absent": True,
                "role_absent": True,
                "timings_ms": {
                    "database_deletion": _elapsed_ms(started),
                },
            }
        except CutoverError:
            raise
        except SQLAlchemyError as exc:
            original = getattr(exc, "orig", None)
            error_code = getattr(original, "sqlstate", None) or getattr(
                original,
                "pgcode",
                None,
            )
            if error_code == "2BP01":
                raise CutoverError(
                    "source_dependencies",
                    "legacy role still owns or can access database resources",
                ) from exc
            if error_code == "55P03":
                raise CutoverError(
                    "active_source_sessions",
                    "legacy source objects could not be locked for deletion",
                ) from exc
            raise CutoverError(
                "source_database_deletion_failed",
                "database deletion failed and was rolled back",
            ) from exc

    def _schema_exists(self, schema_name: str) -> bool:
        return bool(
            self._session.execute(
                text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :schema_name)"),
                {"schema_name": schema_name},
            ).scalar_one()
        )

    def _role_exists(self, role_name: str) -> bool:
        return bool(
            self._session.execute(
                text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role_name)"),
                {"role_name": role_name},
            ).scalar_one()
        )

    def _source_is_active(self, schema_name: str, role_name: str) -> bool:
        return bool(
            self._session.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                          FROM pg_stat_activity activity
                         WHERE activity.pid <> pg_backend_pid()
                           AND activity.usename = :role_name
                        UNION ALL
                        SELECT 1
                          FROM pg_locks lock_record
                          JOIN pg_class relation_record
                            ON relation_record.oid = lock_record.relation
                          JOIN pg_namespace namespace_record
                            ON namespace_record.oid = relation_record.relnamespace
                         WHERE lock_record.pid <> pg_backend_pid()
                           AND lock_record.granted
                           AND namespace_record.nspname = :schema_name
                    )
                    """
                ),
                {
                    "schema_name": schema_name,
                    "role_name": role_name,
                },
            ).scalar_one()
        )

    def _external_source_dependencies(self, schema_name: str) -> tuple[str, ...]:
        dependencies = self._session.execute(
            text(
                """
                WITH source_namespace AS (
                    SELECT oid
                      FROM pg_namespace
                     WHERE nspname = :schema_name
                ),
                source_objects AS (
                    SELECT dependency.classid, dependency.objid
                      FROM pg_depend dependency
                      JOIN source_namespace
                        ON dependency.refobjid = source_namespace.oid
                     WHERE dependency.refclassid = 'pg_namespace'::regclass
                       AND dependency.deptype = 'n'
                )
                SELECT DISTINCT identified.identity
                  FROM pg_depend dependency
                  JOIN source_objects
                    ON dependency.refclassid = source_objects.classid
                   AND dependency.refobjid = source_objects.objid
                  LEFT JOIN pg_policy policy_record
                    ON dependency.classid = 'pg_policy'::regclass
                   AND dependency.objid = policy_record.oid
                  LEFT JOIN pg_class policy_table
                    ON policy_table.oid = policy_record.polrelid
                  LEFT JOIN pg_namespace policy_namespace
                    ON policy_namespace.oid = policy_table.relnamespace
                  LEFT JOIN pg_rewrite rewrite_record
                    ON dependency.classid = 'pg_rewrite'::regclass
                   AND dependency.objid = rewrite_record.oid
                  LEFT JOIN pg_class rewrite_relation
                    ON rewrite_relation.oid = rewrite_record.ev_class
                  LEFT JOIN pg_namespace rewrite_namespace
                    ON rewrite_namespace.oid = rewrite_relation.relnamespace
                 CROSS JOIN LATERAL pg_identify_object(
                     dependency.classid,
                     dependency.objid,
                     dependency.objsubid
                 ) AS identified
                 WHERE dependency.deptype NOT IN ('i', 'a')
                   AND COALESCE(
                           identified.schema,
                           policy_namespace.nspname,
                           rewrite_namespace.nspname
                       ) IS DISTINCT FROM :schema_name
                 ORDER BY identified.identity
                """
            ),
            {"schema_name": schema_name},
        ).scalars()
        return tuple(str(dependency) for dependency in dependencies)

    def _acquire_lock(self, organisation_id: UUID) -> None:
        acquired = self._session.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": (f"{CUTOVER_LOCK_NAMESPACE}:{organisation_id}")},
        ).scalar_one()
        if not acquired:
            raise CutoverError(
                "concurrent_cutover",
                "another cutover holds the organisation advisory lock",
            )

    def _validate_master_organisation(self, organisation_id: UUID) -> None:
        exists_in_master = self._session.execute(
            text(
                "SELECT EXISTS ("
                "SELECT 1 FROM public.organisations "
                "WHERE organisation_id = :organisation_id)"
            ),
            {"organisation_id": str(organisation_id)},
        ).scalar_one()
        if not exists_in_master:
            raise CutoverError(
                "master_organisation_missing",
                "requested organisation does not exist in the master schema",
            )

    def _schema_has_rows(self, schema_name: str) -> bool:
        validate_migration_schema_name(schema_name)
        table_names = self._schema_table_names(schema_name)
        for table_name in table_names:
            # Schema and catalog table names are validated before interpolation.
            if self._session.execute(
                text(
                    f"SELECT EXISTS (SELECT 1 FROM {_qualified(schema_name, table_name)} LIMIT 1)"  # nosec B608
                )
            ).scalar_one():
                return True
        return False

    def _schema_table_names(self, schema_name: str) -> tuple[str, ...]:
        rows = self._session.execute(
            text(
                """
                SELECT class_record.relname
                  FROM pg_class class_record
                  JOIN pg_namespace namespace_record
                    ON namespace_record.oid = class_record.relnamespace
                 WHERE namespace_record.nspname = :schema_name
                   AND class_record.relkind IN ('r', 'p')
                   AND class_record.relname <> 'alembic_version'
                 ORDER BY class_record.relname
                """
            ),
            {"schema_name": schema_name},
        ).scalars()
        return tuple(str(row) for row in rows)

    def _inventory_schema(self, schema_name: str) -> _SchemaInventory:
        # The schema identifier is validated by _qualified; the table name is fixed.
        revision_rows = (
            self._session.execute(
                text(
                    f"SELECT version_num FROM "  # nosec B608
                    f"{_qualified(schema_name, 'alembic_version')}"
                )
            )
            .scalars()
            .all()
        )
        if len(revision_rows) != 1:
            raise CutoverError(
                "migration_revision_missing",
                f"schema {schema_name} must have exactly one migration revision",
            )

        metadata = MetaData()
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"Did not recognize type 'extensions\.vector'.*",
                category=SAWarning,
            )
            metadata.reflect(bind=self._session.connection(), schema=schema_name)
        tables = {
            table.name: table
            for table in metadata.tables.values()
            if table.schema == schema_name and table.name != "alembic_version"
        }
        if not tables:
            raise CutoverError(
                "tenant_tables_missing",
                f"schema {schema_name} has no tenant tables",
            )
        return _SchemaInventory(
            schema=schema_name,
            revision=str(revision_rows[0]),
            metadata=metadata,
            tables=tables,
        )

    def _validate_revisions(
        self,
        source: _SchemaInventory,
        destination: _SchemaInventory,
    ) -> None:
        if (
            source.revision != self._expected_revision
            or destination.revision != self._expected_revision
        ):
            raise CutoverError(
                "migration_revision_mismatch",
                "source, destination, and code migration revisions must match",
            )

    def _validate_compatible_schemas(
        self,
        source: _SchemaInventory,
        destination: _SchemaInventory,
    ) -> None:
        if set(source.tables) != set(destination.tables):
            raise CutoverError(
                "schema_table_mismatch",
                "source and destination tenant table sets differ",
            )
        for name in sorted(source.tables):
            source_table = source.tables[name]
            destination_table = destination.tables[name]
            if _column_signatures(source_table) != _column_signatures(destination_table):
                raise CutoverError(
                    "schema_column_mismatch",
                    f"source and destination columns differ for table {name}",
                )
            source_pk = tuple(column.name for column in source_table.primary_key.columns)
            destination_pk = tuple(column.name for column in destination_table.primary_key.columns)
            if not source_pk or source_pk != destination_pk:
                raise CutoverError(
                    "primary_key_mismatch",
                    f"matching primary keys are required for table {name}",
                )
            if "organisation_id" not in source_table.c:
                raise CutoverError(
                    "organisation_id_missing",
                    f"table {name} has no organisation_id",
                )
            if any(column.identity is not None for column in destination_table.columns):
                raise CutoverError(
                    "identity_column_unsupported",
                    f"identity copy is not supported for table {name}",
                )
            if _foreign_key_signatures(
                source_table,
                source.schema,
            ) != _foreign_key_signatures(destination_table, destination.schema):
                raise CutoverError(
                    "foreign_key_mismatch",
                    f"source and destination foreign keys differ for table {name}",
                )

    def _validate_source_identity(
        self,
        source: _SchemaInventory,
        organisation_id: UUID,
    ) -> dict[str, int]:
        counts: dict[str, int] = {}
        for name, table in sorted(source.tables.items()):
            wrong_identity = self._session.scalar(
                select(func.count())
                .select_from(table)
                .where(table.c.organisation_id.is_distinct_from(organisation_id))
            )
            if wrong_identity:
                raise CutoverError(
                    "source_identity_mismatch",
                    f"source table {name} contains another organisation identity",
                )
            counts[name] = int(self._session.scalar(select(func.count()).select_from(table)) or 0)
        return counts

    def _validate_destination_empty(
        self,
        destination: _SchemaInventory,
        organisation_id: UUID,
    ) -> None:
        for name, table in sorted(destination.tables.items()):
            count = self._session.scalar(
                select(func.count())
                .select_from(table)
                .where(table.c.organisation_id == organisation_id)
            )
            if count:
                raise CutoverError(
                    "destination_not_empty",
                    f"destination table {name} already contains target rows",
                )

    def _replace_destination_rows(self, precheck: _Precheck) -> None:
        """Delete only the selected tenant in dependency-safe order."""

        for table_name in precheck.table_order:
            deferred_columns = self._deferred_columns(precheck, table_name)
            if deferred_columns:
                table = precheck.destination.tables[table_name]
                # Nullable cycle links are cleared first so reverse-order deletes
                # can preserve every non-target tenant and all FK enforcement.
                self._session.execute(
                    table.update()
                    .where(table.c.organisation_id == precheck.organisation_id)
                    .values(dict.fromkeys(deferred_columns))
                )

        for table_name in reversed(precheck.table_order):
            table = precheck.destination.tables[table_name]
            self._session.execute(
                table.delete().where(table.c.organisation_id == precheck.organisation_id)
            )
        self._validate_destination_empty(
            precheck.destination,
            precheck.organisation_id,
        )

    def _validate_destination_rls(self) -> None:
        try:
            validate_tenant_runtime_catalog(
                self._session,
                runtime_role=self._runtime_role,
                schema=self._destination_schema,
                set_read_only=False,
            )
        except TenantRuntimeReadinessError as exc:
            raise CutoverError(
                "destination_rls_invalid",
                "destination does not satisfy the permanent runtime RLS contract",
            ) from exc

    def _copy_plan(
        self,
        inventory: _SchemaInventory,
    ) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
        dependencies: dict[str, set[str]] = {name: set() for name in inventory.tables}
        constraints_by_edge: dict[
            tuple[str, str],
            list[ForeignKeyConstraint],
        ] = {}
        for name, table in inventory.tables.items():
            for constraint in table.foreign_key_constraints:
                referred = constraint.referred_table
                if (
                    referred.schema == inventory.schema
                    and referred.name != name
                    and referred.name in inventory.tables
                ):
                    dependencies[name].add(referred.name)
                    constraints_by_edge.setdefault(
                        (name, referred.name),
                        [],
                    ).append(constraint)

        ordered: list[str] = []
        remaining = {name: set(values) for name, values in dependencies.items()}
        deferred: list[tuple[str, str]] = []
        while remaining:
            ready = sorted(name for name, values in remaining.items() if not values)
            if not ready:
                cycle_nodes = topological.find_cycles(
                    (
                        (parent_name, child_name)
                        for child_name, parent_names in remaining.items()
                        for parent_name in parent_names
                    ),
                    remaining,
                )
                removable_edges = [
                    edge
                    for edge, constraints in constraints_by_edge.items()
                    if edge[0] in cycle_nodes
                    and edge[1] in cycle_nodes
                    and edge[1] in remaining[edge[0]]
                    and all(_can_defer_constraint(item) for item in constraints)
                ]
                if not removable_edges:
                    cycle = ", ".join(sorted(remaining))
                    raise CutoverError(
                        "foreign_key_cycle",
                        f"cannot derive a safe table order: {cycle}",
                    )
                for child_name, parent_name in sorted(removable_edges):
                    remaining[child_name].remove(parent_name)
                    deferred.extend(
                        (
                            child_name,
                            str(constraint.name) if constraint.name is not None else "",
                        )
                        for constraint in constraints_by_edge[(child_name, parent_name)]
                    )
                continue
            ordered.extend(ready)
            for name in ready:
                remaining.pop(name)
            for values in remaining.values():
                values.difference_update(ready)
        return tuple(ordered), tuple(sorted(deferred))

    def _set_tenant_context(self, organisation_id: UUID) -> None:
        self._session.execute(
            text("SELECT set_config('app.current_organisation_id', :organisation_id, true)"),
            {"organisation_id": str(organisation_id)},
        )

    def _copy_table(
        self,
        source: Table,
        destination: Table,
        organisation_id: UUID,
        *,
        deferred_columns: set[str],
    ) -> None:
        column_names = [column.name for column in destination.columns if column.computed is None]
        source_select = select(
            *(
                sql_cast(None, destination.c[name].type).label(name)
                if name in deferred_columns
                else source.c[name]
                for name in column_names
            )
        ).where(source.c.organisation_id == organisation_id)
        self._session.execute(destination.insert().from_select(column_names, source_select))

    def _deferred_columns(
        self,
        precheck: _Precheck,
        table_name: str,
    ) -> set[str]:
        constraint_names = {
            constraint_name
            for deferred_table, constraint_name in precheck.deferred_foreign_keys
            if deferred_table == table_name
        }
        table = precheck.destination.tables[table_name]
        return {
            element.parent.name
            for constraint in table.foreign_key_constraints
            if (constraint.name or "") in constraint_names
            for element in constraint.elements
            if element.parent.name != "organisation_id"
        }

    def _restore_deferred_foreign_keys(self, precheck: _Precheck) -> None:
        for table_name, constraint_name in precheck.deferred_foreign_keys:
            source = precheck.source.tables[table_name]
            destination = precheck.destination.tables[table_name]
            constraint = next(
                item
                for item in destination.foreign_key_constraints
                if (item.name or "") == constraint_name
            )
            restored_columns = [
                element.parent.name
                for element in constraint.elements
                if element.parent.name != "organisation_id"
            ]
            assignments = ", ".join(
                f"{_quote_identifier(name)} = source_row.{_quote_identifier(name)}"
                for name in restored_columns
            )
            primary_key_match = " AND ".join(
                "destination_row."
                f"{_quote_identifier(column.name)} = source_row."
                f"{_quote_identifier(column.name)}"
                for column in destination.primary_key.columns
            )
            self._session.execute(
                text(
                    f"UPDATE {_qualified(str(destination.schema), destination.name)} "  # nosec B608
                    f"AS destination_row SET {assignments} "
                    f"FROM {_qualified(str(source.schema), source.name)} AS source_row "
                    "WHERE destination_row.organisation_id = :organisation_id "
                    "AND source_row.organisation_id = :organisation_id "
                    f"AND {primary_key_match}"
                ),
                {"organisation_id": str(precheck.organisation_id)},
            )

    def _validate_copied_tables(
        self,
        precheck: _Precheck,
    ) -> dict[str, dict[str, Any]]:
        reports: dict[str, dict[str, Any]] = {}
        for name in precheck.table_order:
            source_table = precheck.source.tables[name]
            destination_table = precheck.destination.tables[name]
            source_digest = self._table_digest(
                source_table,
                precheck.organisation_id,
                include_target=True,
            )
            destination_digest = self._table_digest(
                destination_table,
                precheck.organisation_id,
                include_target=True,
            )
            if source_digest != destination_digest:
                raise CutoverError(
                    "copy_validation_mismatch",
                    f"count, key set, or checksum differs for table {name}",
                )
            reports[name] = {
                "primary_key_columns": [column.name for column in source_table.primary_key.columns],
                "source_count": source_digest[0],
                "destination_count": destination_digest[0],
                "source_primary_key_sha256": source_digest[1],
                "destination_primary_key_sha256": destination_digest[1],
                "source_row_sha256": source_digest[2],
                "destination_row_sha256": destination_digest[2],
            }
        return reports

    def _precheck_table_reports(
        self,
        precheck: _Precheck,
    ) -> dict[str, dict[str, Any]]:
        reports: dict[str, dict[str, Any]] = {}
        for name in precheck.table_order:
            source_digest = self._table_digest(
                precheck.source.tables[name],
                precheck.organisation_id,
                include_target=True,
            )
            destination_digest = self._table_digest(
                precheck.destination.tables[name],
                precheck.organisation_id,
                include_target=True,
            )
            reports[name] = {
                "source_count": source_digest[0],
                "source_primary_key_sha256": source_digest[1],
                "source_row_sha256": source_digest[2],
                "destination_count": destination_digest[0],
                "destination_primary_key_sha256": destination_digest[1],
                "destination_row_sha256": destination_digest[2],
            }
        return reports

    def _table_digest(
        self,
        table: Table,
        organisation_id: UUID,
        *,
        include_target: bool,
    ) -> tuple[int, str, str]:
        alias = "cutover_row"
        pk_columns = tuple(column.name for column in table.primary_key.columns)
        key_expression = ", ".join(f"{alias}.{_quote_identifier(name)}" for name in pk_columns)
        comparator = "=" if include_target else "IS DISTINCT FROM"
        # Reflected identifiers are validated; the organisation value remains bound.
        query = text(
            f"SELECT md5(jsonb_build_array({key_expression})::text) AS key_digest, "  # nosec B608
            f"md5(to_jsonb({alias})::text) AS row_digest "
            f"FROM {_qualified(str(table.schema), table.name)} AS {alias} "
            f"WHERE {alias}.organisation_id {comparator} :organisation_id "
            f"ORDER BY {key_expression}"
        )
        key_hash = hashlib.sha256()
        row_hash = hashlib.sha256()
        count = 0
        for row in self._session.execute(
            query,
            {"organisation_id": str(organisation_id)},
        ):
            _update_digest(key_hash, str(row.key_digest))
            _update_digest(row_hash, str(row.row_digest))
            count += 1
        return count, key_hash.hexdigest(), row_hash.hexdigest()

    def _sentinel_snapshot(
        self,
        destination: _SchemaInventory,
        organisation_id: UUID,
    ) -> dict[str, tuple[int, str]]:
        snapshot: dict[str, tuple[int, str]] = {}
        for name, table in sorted(destination.tables.items()):
            count, _key_hash, row_hash = self._table_digest(
                table,
                organisation_id,
                include_target=False,
            )
            snapshot[name] = (count, row_hash)
        return snapshot

    def _validate_foreign_keys(
        self,
        destination: _SchemaInventory,
        organisation_id: UUID,
    ) -> None:
        for table in destination.tables.values():
            for constraint in table.foreign_key_constraints:
                child = table.alias("child")
                parent = constraint.referred_table.alias("parent")
                pairs = tuple(constraint.elements)
                present = and_(*(child.c[element.parent.name].is_not(None) for element in pairs))
                parent_match = and_(
                    *(
                        parent.c[element.column.name] == child.c[element.parent.name]
                        for element in pairs
                    )
                )
                orphan_exists = self._session.scalar(
                    select(
                        exists(
                            select(1)
                            .select_from(child)
                            .where(
                                child.c.organisation_id == organisation_id,
                                present,
                                ~exists(select(1).select_from(parent).where(parent_match)),
                            )
                        )
                    )
                )
                if orphan_exists:
                    raise CutoverError(
                        "foreign_key_validation_failed",
                        f"orphan rows found for {table.name}.{constraint.name}",
                    )

    def _validate_uniqueness(
        self,
        destination: _SchemaInventory,
        organisation_id: UUID,
    ) -> None:
        index_rows = self._session.execute(
            text(
                """
                SELECT table_record.relname AS table_name,
                       index_record.relname AS index_name,
                       ARRAY(
                           SELECT pg_get_indexdef(
                                      index_catalog.indexrelid,
                                      key_position,
                                      true
                                  )
                             FROM generate_series(
                                      1,
                                      index_catalog.indnkeyatts
                                  ) AS key_position
                            ORDER BY key_position
                       ) AS key_expressions,
                       pg_get_expr(
                           index_catalog.indpred,
                           index_catalog.indrelid,
                           true
                       ) AS predicate,
                       COALESCE(
                           (to_jsonb(index_catalog)->>'indnullsnotdistinct')::boolean,
                           false
                       ) AS nulls_not_distinct,
                       index_catalog.indisvalid AS is_valid,
                       index_catalog.indisready AS is_ready
                  FROM pg_index index_catalog
                  JOIN pg_class table_record
                    ON table_record.oid = index_catalog.indrelid
                  JOIN pg_class index_record
                    ON index_record.oid = index_catalog.indexrelid
                  JOIN pg_namespace namespace_record
                    ON namespace_record.oid = table_record.relnamespace
                 WHERE namespace_record.nspname = :schema_name
                   AND index_catalog.indisunique
                 ORDER BY table_record.relname, index_record.relname
                """
            ),
            {"schema_name": destination.schema},
        ).mappings()
        for row in index_rows:
            table_name = str(row["table_name"])
            table = destination.tables.get(table_name)
            if table is None or "organisation_id" not in table.c:
                continue
            index_name = str(row["index_name"])
            if not row["is_valid"] or not row["is_ready"]:
                raise CutoverError(
                    "uniqueness_validation_failed",
                    f"unique index {table_name}.{index_name} is not valid and ready",
                )
            expressions = tuple(str(value) for value in row["key_expressions"])
            if not expressions:
                raise CutoverError(
                    "uniqueness_validation_failed",
                    f"unique index {table_name}.{index_name} has no keys",
                )
            filters = ["organisation_id = :organisation_id"]
            predicate = row["predicate"]
            if predicate:
                filters.append(f"({predicate})")
            if not row["nulls_not_distinct"]:
                filters.extend(f"({expression}) IS NOT NULL" for expression in expressions)
            # Index expressions come from pg_catalog; the organisation value stays bound.
            duplicate = self._session.execute(
                text(  # nosec B608
                    f"SELECT EXISTS (SELECT 1 "  # nosec B608
                    f"FROM {_qualified(destination.schema, table_name)} "
                    f"WHERE {' AND '.join(filters)} "
                    f"GROUP BY {', '.join(expressions)} "
                    "HAVING count(*) > 1)"
                ),
                {"organisation_id": str(organisation_id)},
            ).scalar_one()
            if duplicate:
                raise CutoverError(
                    "uniqueness_validation_failed",
                    f"duplicate rows found for {table_name}.{index_name}",
                )

    def _base_report(
        self,
        precheck: _Precheck,
        *,
        operation: str,
    ) -> dict[str, Any]:
        return {
            "report_id": str(uuid4()),
            "generated_at": datetime.now(UTC).isoformat(),
            "tool": "copilotgtm-shared-tenancy-cutover",
            "tool_version": CUTOVER_TOOL_VERSION,
            "operation": operation,
            "organisation_id": str(precheck.organisation_id),
            "source_schema": precheck.source.schema,
            "destination_schema": precheck.destination.schema,
            "expected_revision": self._expected_revision,
            "source_revision": precheck.source.revision,
            "destination_revision": precheck.destination.revision,
            "table_order": list(precheck.table_order),
            "deferred_foreign_keys": [
                {
                    "table": table_name,
                    "constraint": constraint_name,
                }
                for table_name, constraint_name in precheck.deferred_foreign_keys
            ],
            "source_counts": dict(precheck.source_counts),
        }


def _column_signatures(table: Table) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (
            column.name,
            str(column.type),
            column.nullable,
            column.computed is not None,
            column.identity is not None,
        )
        for column in table.columns
    )


def _foreign_key_signatures(
    table: Table,
    own_schema: str,
) -> tuple[tuple[Any, ...], ...]:
    signatures: list[tuple[Any, ...]] = []
    for constraint in table.foreign_key_constraints:
        target = constraint.referred_table
        target_schema = "$tenant" if target.schema == own_schema else target.schema
        signatures.append(
            (
                tuple(element.parent.name for element in constraint.elements),
                target_schema,
                target.name,
                tuple(element.column.name for element in constraint.elements),
                constraint.ondelete,
                constraint.onupdate,
                constraint.deferrable,
                constraint.initially,
            )
        )
    return tuple(sorted(signatures))


def _can_defer_constraint(constraint: ForeignKeyConstraint) -> bool:
    payload_columns = [
        element.parent
        for element in constraint.elements
        if element.parent.name != "organisation_id"
    ]
    return bool(payload_columns) and all(column.nullable for column in payload_columns)


def _quote_identifier(value: str) -> str:
    validate_migration_schema_name(value)
    return f'"{value}"'


def _qualified(schema: str, table: str) -> str:
    return f"{_quote_identifier(schema)}.{_quote_identifier(table)}"


def _update_digest(digest: Any, value: str) -> None:
    encoded = value.encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)
