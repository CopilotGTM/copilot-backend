"""Asynchronous master and shared-tenant provider contracts."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call
from uuid import uuid4

import pytest
import src.infra.database.provider_async as provider_async
import src.infra.database.tenant_search_path as tenant_search_path
from sqlalchemy.orm import Session as SASession
from src.infra.database.tenant_search_path import (
    TENANT_SCHEMA_SESSION_KEY,
    TENANT_SEARCH_PATH_SESSION_KEY,
)


@pytest.fixture(autouse=True)
def reset_shared_tenant_config(monkeypatch):
    monkeypatch.setattr(
        provider_async.database_config,
        "USE_SHARED_TENANT_DATABASE",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        provider_async.database_config,
        "TENANT_DATABASE_USER",
        "",
        raising=False,
    )
    monkeypatch.setattr(
        provider_async.database_config,
        "TENANT_DATABASE_PASSWORD",
        "",
        raising=False,
    )
    monkeypatch.setattr(
        provider_async.database_config,
        "TENANT_DATABASE_PASSWORD_SECRET_NAME",
        "",
        raising=False,
    )


def test_master_engine_uses_explicit_pool_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_create_async_engine(url, **kwargs):  # noqa: ANN001
        captured["url"] = url
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(
        provider_async.database_config,
        "DATABASE_POOL_TIMEOUT_SEC",
        60,
        raising=False,
    )
    monkeypatch.setattr(provider_async, "create_async_engine", fake_create_async_engine)

    provider = provider_async.AsyncSqlAlchemyProvider()
    provider._get_master_engine()

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["pool_timeout"] == 60
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["pool_recycle"] == 1800
    assert kwargs["pool_use_lifo"] is True
    assert kwargs["pool_reset_on_return"] == "rollback"


@pytest.mark.asyncio
async def test_tenant_engine_uses_explicit_pool_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_create_async_engine(url, **kwargs):  # noqa: ANN001
        captured["url"] = url
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(
        provider_async.database_config,
        "DATABASE_POOL_TIMEOUT_SEC",
        60,
        raising=False,
    )
    monkeypatch.setattr(provider_async, "create_async_engine", fake_create_async_engine)
    monkeypatch.setattr(
        provider_async.database_config,
        "TENANT_DATABASE_USER",
        "app_tenant_runtime",
        raising=False,
    )
    monkeypatch.setattr(
        provider_async.database_config,
        "TENANT_DATABASE_PASSWORD",
        "tenant-secret",
        raising=False,
    )

    provider = provider_async.AsyncSqlAlchemyProvider()
    await provider._get_tenant_engine(uuid4())

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["pool_timeout"] == 60
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["pool_recycle"] == 1800
    assert kwargs["pool_use_lifo"] is True
    assert kwargs["pool_reset_on_return"] == "rollback"


@pytest.mark.asyncio
async def test_tenant_runtime_reuses_one_engine_for_all_orgs(monkeypatch) -> None:
    created_urls: list[str] = []

    def fake_create_async_engine(url, **kwargs):  # noqa: ANN001, ARG001
        created_urls.append(url)
        return object()

    monkeypatch.setattr(provider_async, "create_async_engine", fake_create_async_engine)
    monkeypatch.setattr(
        provider_async.database_config,
        "TENANT_DATABASE_USER",
        "app_tenant_runtime",
        raising=False,
    )
    monkeypatch.setattr(
        provider_async.database_config,
        "TENANT_DATABASE_PASSWORD",
        "shared-secret",
        raising=False,
    )

    provider = provider_async.AsyncSqlAlchemyProvider()
    first_engine = await provider._get_tenant_engine(uuid4())
    second_engine = await provider._get_tenant_engine(uuid4())

    assert first_engine is second_engine
    assert len(created_urls) == 1
    assert created_urls[0].startswith("postgresql+asyncpg://app_tenant_runtime:shared-secret@")
    assert not hasattr(provider, "_tenant_engines")


@pytest.mark.asyncio
async def test_async_tenant_runtime_has_no_per_organisation_credential_branch(
    monkeypatch,
) -> None:
    created_urls: list[str] = []

    def fake_create_async_engine(url, **kwargs):  # noqa: ANN001, ARG001
        created_urls.append(url)
        return object()

    monkeypatch.setattr(provider_async, "create_async_engine", fake_create_async_engine)
    monkeypatch.setattr(
        provider_async.database_config,
        "USE_SHARED_TENANT_DATABASE",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        provider_async.database_config,
        "TENANT_DATABASE_USER",
        "app_tenant_runtime",
        raising=False,
    )
    monkeypatch.setattr(
        provider_async.database_config,
        "TENANT_DATABASE_PASSWORD",
        "shared-secret",
        raising=False,
    )

    provider = provider_async.AsyncSqlAlchemyProvider()
    first = await provider._get_tenant_engine(uuid4())
    second = await provider._get_tenant_engine(uuid4())

    assert first is second
    assert len(created_urls) == 1


@pytest.mark.asyncio
async def test_tenant_runtime_rejects_master_user(monkeypatch) -> None:
    monkeypatch.setattr(provider_async.database_config, "DATABASE_USER", "postgres", raising=False)
    monkeypatch.setattr(
        provider_async.database_config,
        "TENANT_DATABASE_USER",
        "postgres",
        raising=False,
    )
    monkeypatch.setattr(
        provider_async.database_config,
        "TENANT_DATABASE_PASSWORD",
        "runtime-password",
        raising=False,
    )

    provider = provider_async.AsyncSqlAlchemyProvider()

    with pytest.raises(RuntimeError, match="must not equal DATABASE_USER"):
        await provider._get_tenant_engine(uuid4())


@pytest.mark.asyncio
async def test_shared_tenant_mode_requires_credentials(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_async.database_config,
        "USE_SHARED_TENANT_DATABASE",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        provider_async.database_config,
        "TENANT_DATABASE_USER",
        "app_tenant_runtime",
        raising=False,
    )
    monkeypatch.setattr(
        provider_async.database_config,
        "TENANT_DATABASE_PASSWORD",
        "",
        raising=False,
    )

    provider = provider_async.AsyncSqlAlchemyProvider()

    with pytest.raises(RuntimeError, match="TENANT_DATABASE_PASSWORD"):
        await provider._get_tenant_engine(uuid4())


@pytest.mark.asyncio
async def test_shared_tenant_mode_can_load_password_from_key_vault(monkeypatch) -> None:
    created_urls: list[str] = []

    def fake_create_async_engine(url, **kwargs):  # noqa: ANN001, ARG001
        created_urls.append(url)
        return object()

    secret_fetch = AsyncMock(return_value="kv-shared-secret")

    monkeypatch.setattr(provider_async, "create_async_engine", fake_create_async_engine)
    monkeypatch.setattr(provider_async.master_secrets_manager, "get", secret_fetch)
    monkeypatch.setattr(
        provider_async.database_config,
        "USE_SHARED_TENANT_DATABASE",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        provider_async.database_config,
        "TENANT_DATABASE_USER",
        "app_tenant_runtime",
        raising=False,
    )
    monkeypatch.setattr(
        provider_async.database_config,
        "TENANT_DATABASE_PASSWORD",
        "",
        raising=False,
    )
    monkeypatch.setattr(
        provider_async.database_config,
        "TENANT_DATABASE_PASSWORD_SECRET_NAME",
        "shared-tenant-db-password",
        raising=False,
    )

    provider = provider_async.AsyncSqlAlchemyProvider()
    await provider._get_tenant_engine(uuid4())

    secret_fetch.assert_awaited_once_with("shared-tenant-db-password")
    assert created_urls[0].startswith("postgresql+asyncpg://app_tenant_runtime:kv-shared-secret@")


@pytest.mark.asyncio
async def test_async_master_session_marks_runtime_context(monkeypatch) -> None:
    """Async master sessions must opt into transaction-local statement timeouts."""

    class FakeAsyncSession:
        def __init__(self) -> None:
            self.sync_session = type("SyncSession", (), {"info": {}})()

        async def rollback(self) -> None:
            raise AssertionError("rollback should not be called")

        async def close(self) -> None:
            return None

    fake_session = FakeAsyncSession()
    monkeypatch.setattr(
        provider_async.AsyncSqlAlchemyProvider,
        "_get_master_engine",
        lambda self: object(),
    )
    monkeypatch.setattr(
        provider_async,
        "async_sessionmaker",
        lambda *args, **kwargs: lambda: fake_session,
    )

    async with provider_async.AsyncSqlAlchemyProvider().master_session() as session:
        assert session.sync_session.info["database_runtime_session"] is True


@pytest.mark.asyncio
async def test_async_tenant_session_sets_schema_context(monkeypatch) -> None:
    org_id = uuid4()

    class FakeAsyncSession:
        def __init__(self) -> None:
            self.sync_session = type("SyncSession", (), {"info": {}})()
            self.closed = False

        async def rollback(self) -> None:
            raise AssertionError("rollback should not be called")

        async def close(self) -> None:
            self.closed = True

    fake_session = FakeAsyncSession()

    async def fake_get_tenant_engine(self, requested_org_id):  # noqa: ANN001
        assert requested_org_id == org_id
        return object()

    def fake_sessionmaker(*args, **kwargs):  # noqa: ANN002, ANN003
        return lambda: fake_session

    monkeypatch.setattr(
        provider_async.AsyncSqlAlchemyProvider,
        "_get_tenant_engine",
        fake_get_tenant_engine,
    )
    monkeypatch.setattr(provider_async, "async_sessionmaker", fake_sessionmaker)

    provider = provider_async.AsyncSqlAlchemyProvider()

    async with provider.tenant_session(org_id) as session:
        assert session is fake_session
        assert (
            session.sync_session.info[tenant_search_path.TENANT_ORGANISATION_ID_SESSION_KEY]
            == org_id
        )
        assert session.sync_session.info[TENANT_SCHEMA_SESSION_KEY] == "tenant_data"
        assert session.sync_session.info[TENANT_SEARCH_PATH_SESSION_KEY] == (
            "tenant_data",
            "extensions",
        )

    assert fake_session.closed


@pytest.mark.asyncio
async def test_async_tenant_session_normalizes_string_uuid_before_engine_lookup(
    monkeypatch,
) -> None:
    """Async callers receive the same canonical UUID boundary as sync callers."""

    org_id = uuid4()

    class FakeAsyncSession:
        def __init__(self) -> None:
            self.sync_session = type("SyncSession", (), {"info": {}})()

        async def rollback(self) -> None:
            raise AssertionError("rollback should not be called")

        async def close(self) -> None:
            pass

    async def fake_get_tenant_engine(self, requested_org_id):  # noqa: ANN001
        assert requested_org_id == org_id
        return object()

    monkeypatch.setattr(
        provider_async.AsyncSqlAlchemyProvider,
        "_get_tenant_engine",
        fake_get_tenant_engine,
    )
    monkeypatch.setattr(
        provider_async,
        "async_sessionmaker",
        lambda *args, **kwargs: FakeAsyncSession,
    )

    provider = provider_async.AsyncSqlAlchemyProvider()

    async with provider.tenant_session(str(org_id)) as session:
        assert (
            session.sync_session.info[tenant_search_path.TENANT_ORGANISATION_ID_SESSION_KEY]
            == org_id
        )


@pytest.mark.asyncio
async def test_async_tenant_session_rejects_bad_uuid_before_engine_lookup(
    monkeypatch,
) -> None:
    """Malformed async context must not trigger credential or pool work."""

    engine_lookup_called = False

    async def fake_get_tenant_engine(self, requested_org_id):  # noqa: ANN001
        nonlocal engine_lookup_called
        engine_lookup_called = True
        return object()

    monkeypatch.setattr(
        provider_async.AsyncSqlAlchemyProvider,
        "_get_tenant_engine",
        fake_get_tenant_engine,
    )

    provider = provider_async.AsyncSqlAlchemyProvider()

    with pytest.raises(ValueError):
        async with provider.tenant_session("not-a-uuid"):
            pass

    assert engine_lookup_called is False


@pytest.mark.asyncio
async def test_async_tenant_session_always_uses_shared_search_path(monkeypatch) -> None:
    org_id = uuid4()

    class FakeAsyncSession:
        def __init__(self) -> None:
            self.sync_session = type("SyncSession", (), {"info": {}})()

        async def rollback(self) -> None:
            raise AssertionError("rollback should not be called")

        async def close(self) -> None:
            pass

    fake_session = FakeAsyncSession()

    async def fake_get_tenant_engine(self, requested_org_id):  # noqa: ANN001
        assert requested_org_id == org_id
        return object()

    def fake_sessionmaker(*args, **kwargs):  # noqa: ANN002, ANN003
        return lambda: fake_session

    monkeypatch.setattr(
        provider_async.AsyncSqlAlchemyProvider,
        "_get_tenant_engine",
        fake_get_tenant_engine,
    )
    monkeypatch.setattr(provider_async, "async_sessionmaker", fake_sessionmaker)

    provider = provider_async.AsyncSqlAlchemyProvider()

    async with provider.tenant_session(org_id) as session:
        assert session.sync_session.info[TENANT_SEARCH_PATH_SESSION_KEY] == (
            "tenant_data",
            "extensions",
        )


def test_tenant_transaction_hook_uses_set_local_search_path() -> None:
    session = SASession()
    session.info["tenant_schema"] = "org_123"
    connection = MagicMock()

    provider_async.apply_tenant_transaction_context(session, None, connection)

    assert connection.exec_driver_sql.call_args_list[0] == call(
        'SET LOCAL search_path TO "org_123", "extensions"'
    )


def test_tenant_transaction_hook_reapplies_for_each_transaction() -> None:
    session = SASession()
    session.info["tenant_schema"] = "org_123"
    connection = MagicMock()

    provider_async.apply_tenant_transaction_context(session, None, connection)
    provider_async.apply_tenant_transaction_context(session, None, connection)

    search_path_calls = [
        sql_call
        for sql_call in connection.exec_driver_sql.call_args_list
        if sql_call == call('SET LOCAL search_path TO "org_123", "extensions"')
    ]
    assert len(search_path_calls) == 2
