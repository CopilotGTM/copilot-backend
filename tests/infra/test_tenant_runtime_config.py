from __future__ import annotations

import pytest
from src.infra.database.tenant_runtime_config import (
    TenantRuntimeConfigError,
    validate_shared_tenant_runtime_startup,
    validate_tenant_runtime_config,
)


def test_rejects_master_database_user_for_tenant_runtime() -> None:
    with pytest.raises(TenantRuntimeConfigError, match="must not equal DATABASE_USER"):
        validate_tenant_runtime_config(
            database_user="postgres",
            tenant_database_user="postgres",
            tenant_password="tenant-password",
            tenant_password_secret_name="shared-tenant-db-password",
        )


def test_requires_password_or_secret_name() -> None:
    with pytest.raises(TenantRuntimeConfigError, match="password or Key Vault secret"):
        validate_tenant_runtime_config(
            database_user="postgres",
            tenant_database_user="app_tenant_runtime",
            tenant_password="",
            tenant_password_secret_name="",
        )


def test_accepts_key_vault_secret_without_inline_password() -> None:
    result = validate_tenant_runtime_config(
        database_user="postgres",
        tenant_database_user="app_tenant_runtime",
        tenant_password="",
        tenant_password_secret_name="shared-tenant-db-password",
    )

    assert result.user == "app_tenant_runtime"
    assert result.password == ""
    assert result.password_secret_name == "shared-tenant-db-password"


def test_startup_validation_rejects_legacy_database_mode() -> None:
    with pytest.raises(TenantRuntimeConfigError, match="must be enabled"):
        validate_shared_tenant_runtime_startup(
            use_shared_tenant_database=False,
            database_user="postgres",
            tenant_database_user="",
            tenant_password="",
            tenant_password_secret_name="",
        )


def test_startup_validation_fails_when_shared_mode_reuses_master_user() -> None:
    with pytest.raises(TenantRuntimeConfigError, match="must not equal DATABASE_USER"):
        validate_shared_tenant_runtime_startup(
            use_shared_tenant_database=True,
            database_user="postgres",
            tenant_database_user="postgres",
            tenant_password="",
            tenant_password_secret_name="shared-tenant-db-password",
        )
