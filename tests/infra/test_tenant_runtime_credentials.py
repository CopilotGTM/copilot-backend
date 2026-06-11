from __future__ import annotations

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
async def test_startup_password_validation_fetches_key_vault_secret(monkeypatch) -> None:
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

    result = await credential_module.validate_current_shared_tenant_runtime_password(
        secrets_manager=secrets_manager
    )

    assert result == config
    secrets_manager.get.assert_awaited_once_with("shared-tenant-db-password")


def test_sync_startup_password_validation_fetches_key_vault_secret(monkeypatch) -> None:
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

    result = credential_module.validate_current_shared_tenant_runtime_password_sync(
        get_secret=get_secret
    )

    assert result == config
    get_secret.assert_called_once_with("shared-tenant-db-password")
