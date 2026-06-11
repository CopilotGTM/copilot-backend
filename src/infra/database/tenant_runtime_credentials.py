from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from src.infra.database.tenant_runtime_config import (
    TenantRuntimeConfig,
    validate_current_shared_tenant_runtime_startup,
)


class _SecretsManager(Protocol):
    async def get(self, name: str) -> str | None: ...


class TenantRuntimeCredentialError(RuntimeError):
    pass


async def load_tenant_runtime_password(
    config: TenantRuntimeConfig,
    *,
    secrets_manager: _SecretsManager,
) -> str:
    if config.password:
        return config.password

    password = await secrets_manager.get(config.password_secret_name)
    if not password:
        raise TenantRuntimeCredentialError(
            f"Tenant runtime password secret '{config.password_secret_name}' unavailable"
        )
    return password


def load_tenant_runtime_password_sync(
    config: TenantRuntimeConfig,
    *,
    get_secret: Callable[[str], str | None],
) -> str:
    if config.password:
        return config.password

    password = get_secret(config.password_secret_name)
    if not password:
        raise TenantRuntimeCredentialError(
            f"Tenant runtime password secret '{config.password_secret_name}' unavailable"
        )
    return password


async def validate_current_shared_tenant_runtime_password(
    *,
    secrets_manager: _SecretsManager,
) -> TenantRuntimeConfig | None:
    config = validate_current_shared_tenant_runtime_startup()
    if config is None:
        return None
    await load_tenant_runtime_password(config, secrets_manager=secrets_manager)
    return config


def validate_current_shared_tenant_runtime_password_sync(
    *,
    get_secret: Callable[[str], str | None],
) -> TenantRuntimeConfig | None:
    config = validate_current_shared_tenant_runtime_startup()
    if config is None:
        return None
    load_tenant_runtime_password_sync(config, get_secret=get_secret)
    return config
