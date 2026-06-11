from __future__ import annotations

from uuid import uuid4

import pytest
import src.infra.database.provider_sync as provider_sync
from src.infra.database.tenant_search_path import (
    TENANT_SCHEMA_SESSION_KEY,
    TENANT_SEARCH_PATH_SESSION_KEY,
)
from src.infra.utils.resource_naming import get_schema_name


@pytest.fixture(autouse=True)
def reset_shared_tenant_config(monkeypatch):
    monkeypatch.setattr(provider_sync.database_config, "DATABASE_HOST", "localhost", raising=False)
    monkeypatch.setattr(provider_sync.database_config, "DATABASE_PORT", "5432", raising=False)
    monkeypatch.setattr(provider_sync.database_config, "DATABASE_NAME", "postgres", raising=False)
    monkeypatch.setattr(
        provider_sync.database_config,
        "USE_SHARED_TENANT_DATABASE",
        False,
        raising=False,
    )
    monkeypatch.setattr(provider_sync.database_config, "DATABASE_USER", "postgres", raising=False)
    monkeypatch.setattr(
        provider_sync.database_config,
        "TENANT_DATABASE_USER",
        "",
        raising=False,
    )
    monkeypatch.setattr(
        provider_sync.database_config,
        "TENANT_DATABASE_PASSWORD",
        "",
        raising=False,
    )
    monkeypatch.setattr(
        provider_sync.database_config,
        "TENANT_DATABASE_PASSWORD_SECRET_NAME",
        "",
        raising=False,
    )


def test_sync_tenant_runtime_uses_one_engine_for_multiple_orgs(monkeypatch) -> None:
    created_urls: list[str] = []

    def fake_create_engine(url, **kwargs):  # noqa: ANN001, ARG001
        created_urls.append(url)
        return object()

    monkeypatch.setattr(provider_sync, "create_engine", fake_create_engine)
    monkeypatch.setattr(
        provider_sync.database_config,
        "USE_SHARED_TENANT_DATABASE",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        provider_sync.database_config,
        "TENANT_DATABASE_USER",
        "app_tenant_runtime",
        raising=False,
    )
    monkeypatch.setattr(
        provider_sync.database_config,
        "TENANT_DATABASE_PASSWORD",
        "runtime-password",
        raising=False,
    )

    provider = provider_sync.SyncSqlAlchemyProvider()
    first = provider._get_tenant_engine(uuid4())
    second = provider._get_tenant_engine(uuid4())

    assert first is second
    assert len(created_urls) == 1
    assert created_urls[0].startswith("postgresql://app_tenant_runtime:runtime-password@")


def test_sync_tenant_runtime_rejects_master_user(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_sync.database_config,
        "USE_SHARED_TENANT_DATABASE",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        provider_sync.database_config,
        "TENANT_DATABASE_USER",
        "postgres",
        raising=False,
    )
    monkeypatch.setattr(
        provider_sync.database_config,
        "TENANT_DATABASE_PASSWORD",
        "runtime-password",
        raising=False,
    )

    provider = provider_sync.SyncSqlAlchemyProvider()

    with pytest.raises(RuntimeError, match="must not equal DATABASE_USER"):
        provider._get_tenant_engine(uuid4())


def test_sync_tenant_runtime_loads_password_through_sync_credential_helper(
    monkeypatch,
) -> None:
    calls: list[str] = []

    def fake_create_engine(url, **kwargs):  # noqa: ANN001, ARG001
        return {"url": url}

    def fake_get_secret(name: str) -> str:
        calls.append(name)
        return "key-vault-password"

    monkeypatch.setattr(provider_sync, "create_engine", fake_create_engine)
    monkeypatch.setattr(
        provider_sync,
        "_get_master_key_vault_secret_sync",
        fake_get_secret,
        raising=False,
    )
    monkeypatch.setattr(
        provider_sync.database_config,
        "USE_SHARED_TENANT_DATABASE",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        provider_sync.database_config,
        "TENANT_DATABASE_USER",
        "app_tenant_runtime",
        raising=False,
    )
    monkeypatch.setattr(
        provider_sync.database_config, "TENANT_DATABASE_PASSWORD", "", raising=False
    )
    monkeypatch.setattr(
        provider_sync.database_config,
        "TENANT_DATABASE_PASSWORD_SECRET_NAME",
        "shared-tenant-db-password",
        raising=False,
    )

    provider = provider_sync.SyncSqlAlchemyProvider()

    engine = provider._get_tenant_engine(uuid4())

    assert engine == {
        "url": "postgresql://app_tenant_runtime:key-vault-password@localhost:5432/postgres"
    }
    assert calls == ["shared-tenant-db-password"]


def test_sync_tenant_session_sets_schema_context(monkeypatch) -> None:
    org_id = uuid4()

    class FakeSession:
        def __init__(self) -> None:
            self.info: dict[str, str] = {}
            self.closed = False
            self.committed = False

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            raise AssertionError("rollback should not be called")

        def close(self) -> None:
            self.closed = True

    fake_session = FakeSession()

    def fake_get_tenant_engine(self, requested_org_id):  # noqa: ANN001
        assert requested_org_id == org_id
        return object()

    def fake_sessionmaker(*args, **kwargs):  # noqa: ANN002, ANN003
        return lambda: fake_session

    monkeypatch.setattr(
        provider_sync.SyncSqlAlchemyProvider,
        "_get_tenant_engine",
        fake_get_tenant_engine,
    )
    monkeypatch.setattr(provider_sync, "sessionmaker", fake_sessionmaker)

    provider = provider_sync.SyncSqlAlchemyProvider()

    with provider.tenant_session(org_id) as session:
        assert session is fake_session
        assert session.info[TENANT_SCHEMA_SESSION_KEY] == get_schema_name(org_id)
        assert session.info[TENANT_SEARCH_PATH_SESSION_KEY] == (
            get_schema_name(org_id),
            "extensions",
            "public",
        )

    assert fake_session.committed
    assert fake_session.closed


def test_shared_sync_tenant_session_omits_public_from_search_path(monkeypatch) -> None:
    org_id = uuid4()

    class FakeSession:
        def __init__(self) -> None:
            self.info: dict[str, tuple[str, ...] | str] = {}

        def commit(self) -> None:
            pass

        def rollback(self) -> None:
            raise AssertionError("rollback should not be called")

        def close(self) -> None:
            pass

    fake_session = FakeSession()

    def fake_get_tenant_engine(self, requested_org_id):  # noqa: ANN001
        assert requested_org_id == org_id
        return object()

    def fake_sessionmaker(*args, **kwargs):  # noqa: ANN002, ANN003
        return lambda: fake_session

    monkeypatch.setattr(provider_sync.database_config, "USE_SHARED_TENANT_DATABASE", True)
    monkeypatch.setattr(
        provider_sync.SyncSqlAlchemyProvider,
        "_get_tenant_engine",
        fake_get_tenant_engine,
    )
    monkeypatch.setattr(provider_sync, "sessionmaker", fake_sessionmaker)

    provider = provider_sync.SyncSqlAlchemyProvider()

    with provider.tenant_session(org_id) as session:
        assert session.info[TENANT_SEARCH_PATH_SESSION_KEY] == (
            get_schema_name(org_id),
            "extensions",
        )
