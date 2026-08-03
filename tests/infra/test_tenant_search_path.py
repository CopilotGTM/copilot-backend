"""Transaction-local schema, tenant identity, and timeout regression tests."""

from __future__ import annotations

from unittest.mock import MagicMock, call
from uuid import uuid4

import pytest
import src.infra.database.tenant_search_path as tenant_search_path
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from src.infra.database.tenant_search_path import (
    TENANT_DATA_SCHEMA,
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


def test_master_session_applies_statement_timeout_without_tenant_schema(
    monkeypatch,
) -> None:
    """Blocked control-plane queries must not pin the master pool indefinitely."""

    session = Session()
    session.info["database_runtime_session"] = True
    connection = MagicMock()
    monkeypatch.setattr(
        tenant_search_path.database_config,
        "DATABASE_STATEMENT_TIMEOUT_MS",
        60_000,
        raising=False,
    )

    apply_tenant_transaction_context(session, None, connection)

    connection.exec_driver_sql.assert_called_once_with("SET LOCAL statement_timeout = 60000")
    connection.execute.assert_not_called()


def test_unmarked_session_without_tenant_schema_remains_untouched() -> None:
    """The global listener must not inject PostgreSQL SQL into unrelated sessions."""

    session = Session()
    connection = MagicMock()

    apply_tenant_transaction_context(session, None, connection)

    connection.exec_driver_sql.assert_not_called()
    connection.execute.assert_not_called()


def test_sets_transaction_local_organisation_context_with_bound_parameter() -> None:
    """The org UUID must reach PostgreSQL without becoming interpolated SQL."""

    organisation_id = uuid4()
    session = Session()
    session.info[TENANT_SCHEMA_SESSION_KEY] = "org_123"
    session.info["tenant_organisation_id"] = organisation_id
    connection = MagicMock()

    apply_tenant_transaction_context(session, None, connection)

    statement, parameters = connection.execute.call_args.args
    assert str(statement) == (
        "SELECT set_config('app.current_organisation_id', :organisation_id, true)"
    )
    assert parameters == {"organisation_id": str(organisation_id)}


def test_rejects_malformed_organisation_context_before_executing_sql() -> None:
    """Invalid tenant identity must fail before a transaction changes state."""

    session = Session()
    session.info[TENANT_SCHEMA_SESSION_KEY] = "org_123"
    session.info["tenant_organisation_id"] = "not-a-uuid"
    connection = MagicMock()

    with pytest.raises(ValueError):
        apply_tenant_transaction_context(session, None, connection)

    connection.exec_driver_sql.assert_not_called()
    connection.execute.assert_not_called()


def test_rejects_tenant_data_transaction_without_organisation_context() -> None:
    """Shared tenant transactions must not reach PostgreSQL without an org ID."""

    session = Session()
    session.info[TENANT_SCHEMA_SESSION_KEY] = TENANT_DATA_SCHEMA
    connection = MagicMock()

    with pytest.raises(RuntimeError, match="organisation context"):
        apply_tenant_transaction_context(session, None, connection)

    connection.exec_driver_sql.assert_not_called()
    connection.execute.assert_not_called()


def test_tenant_organisation_context_key_is_stable() -> None:
    """Providers and the transaction listener share one non-user-controlled key."""

    assert tenant_search_path.TENANT_ORGANISATION_ID_SESSION_KEY == "tenant_organisation_id"


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
