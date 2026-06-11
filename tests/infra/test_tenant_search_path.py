from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from src.infra.database.tenant_search_path import (
    TENANT_SCHEMA_SESSION_KEY,
    TENANT_SEARCH_PATH_SESSION_KEY,
    TRUSTED_EXTENSION_SCHEMA,
    apply_tenant_transaction_context,
    install_tenant_transaction_context_listener,
)


@pytest.fixture
def sqlite_sessionmaker():
    engine = create_engine("sqlite:///:memory:")
    yield sessionmaker(bind=engine, class_=Session)
    engine.dispose()


def test_applies_set_local_search_path_at_transaction_begin() -> None:
    session = Session()
    session.info[TENANT_SCHEMA_SESSION_KEY] = "org_123"
    connection = MagicMock()

    apply_tenant_transaction_context(session, None, connection)

    assert connection.exec_driver_sql.call_args_list[0] == call(
        'SET LOCAL search_path TO "org_123", "extensions"'
    )
    assert TRUSTED_EXTENSION_SCHEMA == "extensions"


def test_does_nothing_for_master_session_without_tenant_schema() -> None:
    session = Session()
    connection = MagicMock()

    apply_tenant_transaction_context(session, None, connection)

    connection.exec_driver_sql.assert_not_called()


def test_uses_explicit_session_search_path_when_present() -> None:
    session = Session()
    session.info[TENANT_SCHEMA_SESSION_KEY] = "org_123"
    session.info[TENANT_SEARCH_PATH_SESSION_KEY] = ("org_123", "extensions", "public")
    connection = MagicMock()

    apply_tenant_transaction_context(session, None, connection)

    assert connection.exec_driver_sql.call_args_list[0] == call(
        'SET LOCAL search_path TO "org_123", "extensions", "public"'
    )


def test_reapplies_for_new_transaction_after_commit_boundary() -> None:
    session = Session()
    session.info[TENANT_SCHEMA_SESSION_KEY] = "org_123"
    connection = MagicMock()

    apply_tenant_transaction_context(session, None, connection)
    apply_tenant_transaction_context(session, None, connection)

    assert (
        connection.exec_driver_sql.call_args_list.count(
            call('SET LOCAL search_path TO "org_123", "extensions"')
        )
        == 2
    )


def test_real_session_event_reapplies_after_commit(sqlite_sessionmaker) -> None:
    session = sqlite_sessionmaker()
    session.info[TENANT_SCHEMA_SESSION_KEY] = "org_123"
    calls: list[str] = []

    def capture_sql(_conn, _cursor, statement, parameters, _context, _executemany):
        if statement.startswith("SET LOCAL"):
            calls.append(statement)
            return "SELECT 1", ()
        return statement, parameters

    event.listen(session.bind, "before_cursor_execute", capture_sql, retval=True)

    try:
        session.execute(text("SELECT 1"))
        session.commit()
        session.execute(text("SELECT 1"))
    finally:
        event.remove(session.bind, "before_cursor_execute", capture_sql)
        session.close()

    assert calls.count('SET LOCAL search_path TO "org_123", "extensions"') == 2


def test_listener_is_registered_once_for_sqlalchemy_sessions() -> None:
    install_tenant_transaction_context_listener()
    install_tenant_transaction_context_listener()

    assert event.contains(Session, "after_begin", apply_tenant_transaction_context)
