from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.orm import Session
from src.infra.config.configs import database_config

TENANT_SCHEMA_SESSION_KEY = "tenant_schema"
TENANT_SEARCH_PATH_SESSION_KEY = "tenant_search_path"
TRUSTED_EXTENSION_SCHEMA = "extensions"


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def apply_tenant_transaction_context(session: Session, _transaction, connection) -> None:
    schema = session.info.get(TENANT_SCHEMA_SESSION_KEY)
    search_path_schemas = session.info.get(TENANT_SEARCH_PATH_SESSION_KEY)
    if search_path_schemas is None:
        if not schema:
            return
        search_path_schemas = (schema, TRUSTED_EXTENSION_SCHEMA)
    if not search_path_schemas:
        return

    search_path = ", ".join(_quote_identifier(str(name)) for name in search_path_schemas)
    connection.exec_driver_sql(f"SET LOCAL search_path TO {search_path}")
    timeout_ms = int(database_config.DATABASE_STATEMENT_TIMEOUT_MS)
    if timeout_ms > 0:
        connection.exec_driver_sql(f"SET LOCAL statement_timeout = {timeout_ms}")


def install_tenant_transaction_context_listener() -> None:
    if not event.contains(Session, "after_begin", apply_tenant_transaction_context):
        event.listen(Session, "after_begin", apply_tenant_transaction_context)


install_tenant_transaction_context_listener()
