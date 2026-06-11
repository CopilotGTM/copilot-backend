from __future__ import annotations

from dataclasses import dataclass

from src.infra.config.configs import database_config


class TenantRuntimeConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class TenantRuntimeConfig:
    user: str
    password: str
    password_secret_name: str


def validate_tenant_runtime_config(
    *,
    database_user: str,
    tenant_database_user: str,
    tenant_password: str,
    tenant_password_secret_name: str,
) -> TenantRuntimeConfig:
    if not tenant_database_user:
        raise TenantRuntimeConfigError("TENANT_DATABASE_USER is required")
    if tenant_database_user == database_user:
        raise TenantRuntimeConfigError("TENANT_DATABASE_USER must not equal DATABASE_USER")
    if not tenant_password and not tenant_password_secret_name:
        raise TenantRuntimeConfigError(
            "TENANT_DATABASE_PASSWORD or Key Vault secret name is required; "
            "tenant runtime password or Key Vault secret must be configured"
        )
    return TenantRuntimeConfig(
        user=tenant_database_user,
        password=tenant_password,
        password_secret_name=tenant_password_secret_name,
    )


def validate_shared_tenant_runtime_startup(
    *,
    use_shared_tenant_database: bool,
    database_user: str,
    tenant_database_user: str,
    tenant_password: str,
    tenant_password_secret_name: str,
) -> TenantRuntimeConfig | None:
    if not use_shared_tenant_database:
        return None
    return validate_tenant_runtime_config(
        database_user=database_user,
        tenant_database_user=tenant_database_user,
        tenant_password=tenant_password,
        tenant_password_secret_name=tenant_password_secret_name,
    )


def validate_current_shared_tenant_runtime_startup() -> TenantRuntimeConfig | None:
    return validate_shared_tenant_runtime_startup(
        use_shared_tenant_database=database_config.USE_SHARED_TENANT_DATABASE,
        database_user=database_config.DATABASE_USER,
        tenant_database_user=database_config.TENANT_DATABASE_USER,
        tenant_password=database_config.TENANT_DATABASE_PASSWORD,
        tenant_password_secret_name=database_config.TENANT_DATABASE_PASSWORD_SECRET_NAME,
    )
