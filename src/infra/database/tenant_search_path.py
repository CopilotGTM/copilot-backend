"""Transaction-local database context for existing organisation sessions.

The sync and async providers attach trusted schema and organisation metadata
to SQLAlchemy sessions. This shared listener reapplies both values whenever a
transaction begins, which is required when PgBouncer reuses backend
connections in transaction-pooling mode.
"""

from __future__ import annotations

import time
from uuid import UUID

from sqlalchemy import event, text
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import Session
from src.infra.config.configs import database_config
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)

DATABASE_RUNTIME_SESSION_KEY = "database_runtime_session"
TENANT_ORGANISATION_ID_SESSION_KEY = "tenant_organisation_id"
TENANT_SCHEMA_SESSION_KEY = "tenant_schema"
TENANT_SEARCH_PATH_SESSION_KEY = "tenant_search_path"
TENANT_DATA_SCHEMA = "tenant_data"
TRUSTED_EXTENSION_SCHEMA = "extensions"


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def parse_tenant_organisation_id(value: UUID | str) -> UUID:
    """Return the canonical UUID accepted by the organisation session boundary."""

    started = time.perf_counter()
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except ValueError:
        logger.error(
            "Tenant organisation context rejected",
            extra={
                "event": "shared_tenancy_context",
                "operation": "parse_organisation_id",
                "outcome": "failed",
                "error_type": "ValueError",
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            },
        )
        raise


def classify_tenant_database_failure(error: Exception) -> tuple[str, str | None]:
    """Return a bounded alert category and SQLSTATE without exception payloads."""

    raw_sqlstate = getattr(error, "sqlstate", None)
    if raw_sqlstate is None:
        original = getattr(error, "orig", None)
        raw_sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    sqlstate = raw_sqlstate if isinstance(raw_sqlstate, str) else None
    if isinstance(error, SQLAlchemyTimeoutError):
        return "pool_timeout", sqlstate
    if sqlstate is None:
        return "database_error", None
    return {
        "42501": "rls_violation",
        "40P01": "deadlock",
        "55P03": "lock_timeout",
        "57014": "statement_timeout",
    }.get(sqlstate, "database_error"), sqlstate


def apply_tenant_transaction_context(session: Session, _transaction, connection) -> None:
    """Apply schema, tenant identity, and timeout to a new org transaction.

    Tenant identity is parsed before any SQL runs and written with a bound
    parameter. `set_config(..., true)` scopes it to the current transaction so
    a pooled connection cannot carry one organisation into the next request.
    """

    raw_organisation_id = session.info.get(TENANT_ORGANISATION_ID_SESSION_KEY)
    organisation_id = (
        parse_tenant_organisation_id(raw_organisation_id)
        if raw_organisation_id is not None
        else None
    )
    schema = session.info.get(TENANT_SCHEMA_SESSION_KEY)
    search_path_schemas = session.info.get(TENANT_SEARCH_PATH_SESSION_KEY)
    if search_path_schemas is None:
        if not schema:
            if session.info.get(DATABASE_RUNTIME_SESSION_KEY):
                timeout_ms = int(database_config.DATABASE_STATEMENT_TIMEOUT_MS)
                if timeout_ms > 0:
                    connection.exec_driver_sql(f"SET LOCAL statement_timeout = {timeout_ms}")
            return
        search_path_schemas = (schema, TRUSTED_EXTENSION_SCHEMA)
    if not search_path_schemas:
        return

    search_path = ", ".join(_quote_identifier(str(name)) for name in search_path_schemas)
    connection.exec_driver_sql(f"SET LOCAL search_path TO {search_path}")
    if organisation_id is not None:
        connection.execute(
            text("SELECT set_config('app.current_organisation_id', :organisation_id, true)"),
            {"organisation_id": str(organisation_id)},
        )
    timeout_ms = int(database_config.DATABASE_STATEMENT_TIMEOUT_MS)
    if timeout_ms > 0:
        connection.exec_driver_sql(f"SET LOCAL statement_timeout = {timeout_ms}")


def install_tenant_transaction_context_listener() -> None:
    """Install the shared SQLAlchemy transaction listener exactly once."""

    if not event.contains(Session, "after_begin", apply_tenant_transaction_context):
        event.listen(Session, "after_begin", apply_tenant_transaction_context)


install_tenant_transaction_context_listener()
