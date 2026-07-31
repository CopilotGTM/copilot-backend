"""Credential resolution and live tenant-role authentication contracts."""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from unittest.mock import AsyncMock, Mock

import pytest
import src.infra.database.tenant_runtime_credentials as credential_module
from src.infra.database.tenant_runtime_config import TenantRuntimeConfig
from src.infra.database.tenant_runtime_credentials import (
    TenantRuntimeCredentialError,
    load_tenant_runtime_password,
    load_tenant_runtime_password_sync,
)


@pytest.mark.asyncio
async def test_uses_inline_password_without_key_vault_call() -> None:
    """An explicit local password must not trigger a secret-provider call."""

    secrets_manager = AsyncMock()
    config = TenantRuntimeConfig(
        user="app_tenant_runtime",
        password="inline-password",
        password_secret_name="shared-tenant-db-password",
    )

    password = await load_tenant_runtime_password(config, secrets_manager=secrets_manager)

    assert password == "inline-password"
    secrets_manager.get.assert_not_called()


@pytest.mark.asyncio
async def test_loads_password_from_key_vault_secret() -> None:
    """Production configuration resolves the named runtime secret once."""

    secrets_manager = AsyncMock()
    secrets_manager.get.return_value = "key-vault-password"
    config = TenantRuntimeConfig(
        user="app_tenant_runtime",
        password="",
        password_secret_name="shared-tenant-db-password",
    )

    password = await load_tenant_runtime_password(config, secrets_manager=secrets_manager)

    assert password == "key-vault-password"
    secrets_manager.get.assert_awaited_once_with("shared-tenant-db-password")


@pytest.mark.asyncio
async def test_raises_when_key_vault_secret_missing() -> None:
    """Startup fails closed when the configured runtime secret is unavailable."""

    secrets_manager = AsyncMock()
    secrets_manager.get.return_value = None
    config = TenantRuntimeConfig(
        user="app_tenant_runtime",
        password="",
        password_secret_name="shared-tenant-db-password",
    )

    with pytest.raises(TenantRuntimeCredentialError, match="unavailable"):
        await load_tenant_runtime_password(config, secrets_manager=secrets_manager)


def test_sync_uses_inline_password_without_key_vault_call() -> None:
    """Sync workers use an explicit local password without Key Vault."""

    get_secret = Mock()
    config = TenantRuntimeConfig(
        user="app_tenant_runtime",
        password="inline-password",
        password_secret_name="shared-tenant-db-password",
    )

    password = load_tenant_runtime_password_sync(config, get_secret=get_secret)

    assert password == "inline-password"
    get_secret.assert_not_called()


def test_sync_loads_password_from_same_secret_name() -> None:
    """Sync workers and the API resolve the same configured secret."""

    get_secret = Mock(return_value="key-vault-password")
    config = TenantRuntimeConfig(
        user="app_tenant_runtime",
        password="",
        password_secret_name="shared-tenant-db-password",
    )

    password = load_tenant_runtime_password_sync(config, get_secret=get_secret)

    assert password == "key-vault-password"
    get_secret.assert_called_once_with("shared-tenant-db-password")


@pytest.mark.asyncio
async def test_async_runtime_login_probe_uses_tenant_session_and_checks_role(
    monkeypatch,
) -> None:
    """The API probe must exercise its real tenant session boundary."""

    session = AsyncMock()
    session.scalar.return_value = "app_tenant_runtime"

    @asynccontextmanager
    async def tenant_session(_org_id):
        """Yield the fake session through the production context-manager shape."""

        yield session

    monkeypatch.setattr(
        "src.infra.database.sessions.org_async_session_scope",
        tenant_session,
    )

    await credential_module.validate_tenant_runtime_login_async(
        runtime_role="app_tenant_runtime",
    )

    session.scalar.assert_awaited_once()
    assert "current_user" in str(session.scalar.await_args.args[0])


def test_sync_runtime_login_probe_uses_tenant_session_and_checks_role(
    monkeypatch,
) -> None:
    """The worker probe must exercise its real tenant session boundary."""

    session = Mock()
    session.scalar.return_value = "app_tenant_runtime"

    @contextmanager
    def tenant_session(_org_id):
        """Yield the fake session through the production context-manager shape."""

        yield session

    monkeypatch.setattr(
        "src.infra.database.sessions.org_session_scope",
        tenant_session,
    )

    credential_module.validate_tenant_runtime_login(
        runtime_role="app_tenant_runtime",
    )

    session.scalar.assert_called_once()
    assert "current_user" in str(session.scalar.call_args.args[0])


@pytest.mark.asyncio
async def test_async_runtime_login_probe_rejects_unexpected_role(monkeypatch) -> None:
    """A proxy or credential resolving to a privileged role must fail startup."""

    session = AsyncMock()
    session.scalar.return_value = "postgres"

    @asynccontextmanager
    async def tenant_session(_org_id):
        """Yield a session reporting the unsafe effective role."""

        yield session

    monkeypatch.setattr(
        "src.infra.database.sessions.org_async_session_scope",
        tenant_session,
    )

    with pytest.raises(TenantRuntimeCredentialError, match="unexpected database role"):
        await credential_module.validate_tenant_runtime_login_async(
            runtime_role="app_tenant_runtime",
        )


@pytest.mark.asyncio
async def test_startup_password_validation_fetches_key_vault_secret(monkeypatch) -> None:
    """API startup must resolve, inspect, and authenticate the tenant role."""

    secrets_manager = AsyncMock()
    secrets_manager.get.return_value = "key-vault-password"
    config = TenantRuntimeConfig(
        user="app_tenant_runtime",
        password="",
        password_secret_name="shared-tenant-db-password",
    )
    monkeypatch.setattr(
        credential_module,
        "validate_current_shared_tenant_runtime_startup",
        lambda: config,
        raising=False,
    )
    validate_catalog = AsyncMock()
    monkeypatch.setattr(
        credential_module,
        "validate_current_tenant_runtime_catalog_async",
        validate_catalog,
    )
    validate_login = AsyncMock()
    monkeypatch.setattr(
        credential_module,
        "validate_tenant_runtime_login_async",
        validate_login,
    )

    result = await credential_module.validate_current_shared_tenant_runtime_password(
        secrets_manager=secrets_manager
    )

    assert result == config
    secrets_manager.get.assert_awaited_once_with("shared-tenant-db-password")
    validate_catalog.assert_awaited_once_with(runtime_role="app_tenant_runtime")
    validate_login.assert_awaited_once_with(runtime_role="app_tenant_runtime")


def test_sync_startup_password_validation_fetches_key_vault_secret(monkeypatch) -> None:
    """Worker startup must resolve, inspect, and authenticate the tenant role."""

    get_secret = Mock(return_value="key-vault-password")
    config = TenantRuntimeConfig(
        user="app_tenant_runtime",
        password="",
        password_secret_name="shared-tenant-db-password",
    )
    monkeypatch.setattr(
        credential_module,
        "validate_current_shared_tenant_runtime_startup",
        lambda: config,
        raising=False,
    )
    validate_catalog = Mock()
    monkeypatch.setattr(
        credential_module,
        "validate_current_tenant_runtime_catalog",
        validate_catalog,
    )
    validate_login = Mock()
    monkeypatch.setattr(
        credential_module,
        "validate_tenant_runtime_login",
        validate_login,
    )

    result = credential_module.validate_current_shared_tenant_runtime_password_sync(
        get_secret=get_secret
    )

    assert result == config
    get_secret.assert_called_once_with("shared-tenant-db-password")
    validate_catalog.assert_called_once_with(runtime_role="app_tenant_runtime")
    validate_login.assert_called_once_with(runtime_role="app_tenant_runtime")
