"""Fail-closed startup validation for the shared tenant PostgreSQL boundary."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from src.infra.database.tenant_search_path import TENANT_DATA_SCHEMA
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class TenantRuntimeReadinessError(RuntimeError):
    """The deployed database cannot safely serve shared tenant traffic."""


_CATALOG_FAILURES_SQL = text(
    """
    WITH runtime_role AS (
        SELECT role_record.oid,
               role_record.rolname,
               role_record.rolsuper,
               role_record.rolbypassrls,
               role_record.rolcreatedb,
               role_record.rolcreaterole
          FROM pg_roles role_record
         WHERE role_record.rolname = :runtime_role
    ),
    tenant_schema AS (
        SELECT namespace_record.oid,
               namespace_record.nspowner
          FROM pg_namespace namespace_record
         WHERE namespace_record.nspname = :schema
    ),
    tenant_tables AS (
        SELECT class_record.oid,
               class_record.relname,
               class_record.relowner,
               class_record.relrowsecurity,
               class_record.relforcerowsecurity
          FROM pg_class class_record
          JOIN tenant_schema ON tenant_schema.oid = class_record.relnamespace
         WHERE class_record.relkind IN ('r', 'p')
           AND class_record.relname <> 'alembic_version'
    ),
    invalid_tables AS (
        SELECT tenant_table.relname,
               concat_ws(
                   ',',
                   CASE WHEN NOT EXISTS (
                       SELECT 1
                         FROM pg_attribute attribute_record
                        WHERE attribute_record.attrelid = tenant_table.oid
                          AND attribute_record.attname = 'organisation_id'
                          AND NOT attribute_record.attisdropped
                   ) THEN 'missing organisation_id' END,
                   CASE WHEN NOT tenant_table.relrowsecurity
                        THEN 'RLS disabled' END,
                   CASE WHEN NOT tenant_table.relforcerowsecurity
                        THEN 'RLS not forced' END,
                   CASE WHEN EXISTS (
                       SELECT 1
                         FROM runtime_role
                        WHERE pg_has_role(
                            runtime_role.oid,
                            tenant_table.relowner,
                            'MEMBER'
                        )
                   ) THEN 'runtime can assume owner' END,
                   CASE WHEN NOT EXISTS (
                        SELECT 1
                          FROM runtime_role
                         WHERE has_table_privilege(
                                   runtime_role.oid,
                                   tenant_table.oid,
                                   'SELECT'
                               )
                           AND has_table_privilege(
                                   runtime_role.oid,
                                   tenant_table.oid,
                                   'INSERT'
                               )
                           AND has_table_privilege(
                                   runtime_role.oid,
                                   tenant_table.oid,
                                   'UPDATE'
                               )
                           AND has_table_privilege(
                                   runtime_role.oid,
                                   tenant_table.oid,
                                   'DELETE'
                               )
                   ) THEN 'missing DML grants' END,
                   CASE WHEN NOT (
                       (SELECT count(*)
                          FROM pg_policy policy_record
                         WHERE policy_record.polrelid = tenant_table.oid) = 2
                       AND EXISTS (
                           SELECT 1
                             FROM pg_policy policy_record
                             JOIN runtime_role ON true
                            WHERE policy_record.polrelid = tenant_table.oid
                              AND policy_record.polname = 'tenant_access'
                              AND policy_record.polpermissive
                              AND policy_record.polcmd = '*'
                              AND policy_record.polroles
                                  = ARRAY[runtime_role.oid]
                              AND pg_get_expr(
                                      policy_record.polqual,
                                      policy_record.polrelid
                                  ) = format(
                                      '(organisation_id = %I.current_organisation_id())',
                                      :schema
                                  )
                              AND pg_get_expr(
                                      policy_record.polwithcheck,
                                      policy_record.polrelid
                                  ) = format(
                                      '(organisation_id = %I.current_organisation_id())',
                                      :schema
                                  )
                       )
                       AND EXISTS (
                           SELECT 1
                             FROM pg_policy policy_record
                             JOIN runtime_role ON true
                            WHERE policy_record.polrelid = tenant_table.oid
                              AND policy_record.polname = 'tenant_isolation'
                              AND NOT policy_record.polpermissive
                              AND policy_record.polcmd = '*'
                              AND policy_record.polroles
                                  = ARRAY[runtime_role.oid]
                              AND pg_get_expr(
                                      policy_record.polqual,
                                      policy_record.polrelid
                                  ) = format(
                                      '(organisation_id = %I.current_organisation_id())',
                                      :schema
                                  )
                              AND pg_get_expr(
                                      policy_record.polwithcheck,
                                      policy_record.polrelid
                                  ) = format(
                                      '(organisation_id = %I.current_organisation_id())',
                                      :schema
                                  )
                       )
                   ) THEN 'invalid tenant policies' END
               ) AS reasons
          FROM tenant_tables tenant_table
    ),
    invalid_sequences AS (
        SELECT class_record.relname
          FROM pg_class class_record
          JOIN tenant_schema ON tenant_schema.oid = class_record.relnamespace
         WHERE class_record.relkind = 'S'
           AND NOT EXISTS (
               SELECT 1
                 FROM runtime_role
                WHERE has_sequence_privilege(
                          runtime_role.oid,
                          class_record.oid,
                          'USAGE'
                      )
                  AND has_sequence_privilege(
                          runtime_role.oid,
                          class_record.oid,
                          'SELECT'
                      )
                  AND has_sequence_privilege(
                          runtime_role.oid,
                          class_record.oid,
                          'UPDATE'
                      )
           )
    ),
    context_function AS (
        SELECT procedure_record.oid,
               procedure_record.proowner,
               pg_get_function_result(procedure_record.oid) = 'uuid'
                   AS returns_uuid,
               procedure_record.provolatile = 's' AS is_stable,
               NOT procedure_record.prosecdef AS is_security_invoker,
               procedure_record.proparallel = 's' AS is_parallel_safe,
               coalesce(
                   procedure_record.proconfig
                       @> ARRAY['search_path=pg_catalog'],
                   false
               ) AS has_safe_search_path,
               procedure_record.prosrc LIKE
                   '%current_setting(''app.current_organisation_id'', true)%'
                   AS reads_tenant_setting,
               EXISTS (
                   SELECT 1
                     FROM aclexplode(
                         coalesce(
                             procedure_record.proacl,
                             acldefault('f', procedure_record.proowner)
                         )
                     ) acl_record
                    WHERE acl_record.grantee = 0
                      AND acl_record.privilege_type = 'EXECUTE'
               ) AS public_can_execute
          FROM pg_proc procedure_record
          JOIN tenant_schema
            ON tenant_schema.oid = procedure_record.pronamespace
         WHERE procedure_record.proname = 'current_organisation_id'
           AND procedure_record.pronargs = 0
    )
    SELECT 'schema: missing tenant_data' AS failure
     WHERE NOT EXISTS (SELECT 1 FROM tenant_schema)
    UNION ALL
    SELECT 'schema: no tenant tables'
     WHERE EXISTS (SELECT 1 FROM tenant_schema)
       AND NOT EXISTS (SELECT 1 FROM tenant_tables)
    UNION ALL
    SELECT 'runtime role: missing'
     WHERE NOT EXISTS (SELECT 1 FROM runtime_role)
    UNION ALL
    SELECT 'runtime role: unsafe attributes or inherited elevation'
      FROM runtime_role
     WHERE runtime_role.rolsuper
        OR runtime_role.rolbypassrls
        OR runtime_role.rolcreatedb
        OR runtime_role.rolcreaterole
        OR EXISTS (
            SELECT 1
              FROM pg_roles unsafe_role
             WHERE (
                       unsafe_role.rolsuper
                    OR unsafe_role.rolbypassrls
                    OR unsafe_role.rolcreatedb
                    OR unsafe_role.rolcreaterole
                   )
               AND pg_has_role(
                       runtime_role.oid,
                       unsafe_role.oid,
                       'MEMBER'
                   )
        )
    UNION ALL
    SELECT 'schema: runtime can assume owner'
      FROM runtime_role
      JOIN tenant_schema ON true
     WHERE pg_has_role(
               runtime_role.oid,
               tenant_schema.nspowner,
               'MEMBER'
           )
    UNION ALL
    SELECT 'schema: missing USAGE or runtime has CREATE'
      FROM runtime_role
      JOIN tenant_schema ON true
     WHERE NOT has_schema_privilege(
                   runtime_role.oid,
                   tenant_schema.oid,
                   'USAGE'
               )
        OR has_schema_privilege(
               runtime_role.oid,
               tenant_schema.oid,
               'CREATE'
           )
    UNION ALL
    SELECT format('table %I: %s', invalid_table.relname, invalid_table.reasons)
      FROM invalid_tables invalid_table
     WHERE invalid_table.reasons <> ''
    UNION ALL
    SELECT format(
               'sequence %I: missing USAGE, SELECT, or UPDATE',
               invalid_sequence.relname
           )
      FROM invalid_sequences invalid_sequence
    UNION ALL
    SELECT 'context function: missing or unsafe'
     WHERE (SELECT count(*) FROM context_function) <> 1
        OR EXISTS (
            SELECT 1
              FROM context_function
              JOIN runtime_role ON true
             WHERE NOT context_function.returns_uuid
                OR NOT context_function.is_stable
                OR NOT context_function.is_security_invoker
                OR NOT context_function.is_parallel_safe
                OR NOT context_function.has_safe_search_path
                OR NOT context_function.reads_tenant_setting
                OR context_function.public_can_execute
                OR pg_has_role(
                       runtime_role.oid,
                       context_function.proowner,
                       'MEMBER'
                   )
                OR NOT has_function_privilege(
                           runtime_role.oid,
                           context_function.oid,
                           'EXECUTE'
                       )
        )
    ORDER BY 1
    """
)


def _raise_for_failures(
    failures: Iterable[str],
    *,
    schema: str,
    runtime_role: str,
    started: float,
) -> None:
    findings = tuple(str(failure) for failure in failures)
    outcome = "failed" if findings else "passed"
    _log_catalog_outcome(
        schema=schema,
        runtime_role=runtime_role,
        started=started,
        outcome=outcome,
        findings=findings,
    )
    if findings:
        raise TenantRuntimeReadinessError(
            "shared tenant database is unsafe: " + "; ".join(findings)
        )


def _log_catalog_outcome(
    *,
    schema: str,
    runtime_role: str,
    started: float,
    outcome: str,
    findings: tuple[str, ...] = (),
    error_type: str | None = None,
) -> None:
    """Emit the bounded startup-gate record consumed by tenancy alerts."""

    logger.log(
        logging.ERROR if outcome == "failed" else logging.INFO,
        "Shared tenant startup catalog gate completed",
        extra={
            "event": "shared_tenancy_startup_gate",
            "operation": "validate_runtime_catalog",
            "outcome": outcome,
            "schema": schema,
            "runtime_role": runtime_role,
            "finding_count": len(findings) if error_type is None else -1,
            "findings": findings,
            "error_type": error_type,
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        },
    )


def validate_tenant_runtime_catalog(
    session: Session,
    *,
    runtime_role: str,
    schema: str = TENANT_DATA_SCHEMA,
    set_read_only: bool = True,
) -> None:
    """Reject startup unless the live tenant catalog enforces the RLS contract."""

    started = time.perf_counter()
    try:
        if set_read_only:
            session.execute(text("SET TRANSACTION READ ONLY"))
        # pg_get_expr output follows search_path, which pooled connections may
        # inherit from an earlier session. Keep this catalog comparison local.
        session.execute(text("SET LOCAL search_path TO pg_catalog"))
        failures = session.execute(
            _CATALOG_FAILURES_SQL,
            {"schema": schema, "runtime_role": runtime_role},
        ).scalars()
    except Exception as exc:
        _log_catalog_outcome(
            schema=schema,
            runtime_role=runtime_role,
            started=started,
            outcome="failed",
            error_type=type(exc).__name__,
        )
        raise
    _raise_for_failures(
        failures,
        schema=schema,
        runtime_role=runtime_role,
        started=started,
    )


async def validate_tenant_runtime_catalog_async(
    session: AsyncSession,
    *,
    runtime_role: str,
    schema: str = TENANT_DATA_SCHEMA,
    set_read_only: bool = True,
) -> None:
    """Async form of the same catalog gate used by API startup."""

    started = time.perf_counter()
    try:
        if set_read_only:
            await session.execute(text("SET TRANSACTION READ ONLY"))
        # Match the synchronous gate when PgBouncer reuses backend sessions.
        await session.execute(text("SET LOCAL search_path TO pg_catalog"))
        result = await session.execute(
            _CATALOG_FAILURES_SQL,
            {"schema": schema, "runtime_role": runtime_role},
        )
    except Exception as exc:
        _log_catalog_outcome(
            schema=schema,
            runtime_role=runtime_role,
            started=started,
            outcome="failed",
            error_type=type(exc).__name__,
        )
        raise
    _raise_for_failures(
        result.scalars(),
        schema=schema,
        runtime_role=runtime_role,
        started=started,
    )


def validate_current_tenant_runtime_catalog(*, runtime_role: str) -> None:
    """Open the existing master boundary and validate the tenant catalog."""

    from src.infra.database.sessions import master_session_scope

    with master_session_scope() as session, session.begin():
        validate_tenant_runtime_catalog(session, runtime_role=runtime_role)


async def validate_current_tenant_runtime_catalog_async(
    *,
    runtime_role: str,
) -> None:
    """Open the existing async master boundary and validate the tenant catalog."""

    from src.infra.database.sessions import master_async_session_scope

    async with master_async_session_scope() as session, session.begin():
        await validate_tenant_runtime_catalog_async(
            session,
            runtime_role=runtime_role,
        )
