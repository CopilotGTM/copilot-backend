"""Resolve shared tenant credentials and run the live startup safety gate."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from sqlalchemy import text
from src.infra.database.tenant_runtime_config import (
    TenantRuntimeConfig,
    validate_current_shared_tenant_runtime_startup,
)
from src.infra.database.tenant_runtime_readiness import (
    validate_current_tenant_runtime_catalog,
    validate_current_tenant_runtime_catalog_async,
)


class _SecretsManager(Protocol):
    """Minimal Key Vault contract needed to resolve the runtime password."""

    async def get(self, name: str) -> str | None:
        """Return one secret without exposing provider-specific APIs."""

        ...


class TenantRuntimeCredentialError(RuntimeError):
    """The configured shared runtime credential could not be resolved."""


_RUNTIME_LOGIN_PROBE_ORGANISATION_ID = UUID(int=0)


async def load_tenant_runtime_password(
    config: TenantRuntimeConfig,
    *,
    secrets_manager: _SecretsManager,
) -> str:
    """Resolve the async runtime password without logging or persisting it."""

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
    """Resolve the sync runtime password without logging or persisting it."""

    if config.password:
        return config.password

    password = get_secret(config.password_secret_name)
    if not password:
        raise TenantRuntimeCredentialError(
            f"Tenant runtime password secret '{config.password_secret_name}' unavailable"
        )
    return password


async def validate_tenant_runtime_login_async(*, runtime_role: str) -> None:
    """Prove the async tenant pool authenticates as the restricted runtime role.

    The zero UUID supplies required transaction-local context, but the probe
    reads no tenant table and therefore cannot observe organisation data.
    """

    from src.infra.database.sessions import org_async_session_scope

    async with org_async_session_scope(_RUNTIME_LOGIN_PROBE_ORGANISATION_ID) as session:
        current_user = await session.scalar(text("SELECT current_user"))
    if current_user != runtime_role:
        raise TenantRuntimeCredentialError(
            "Tenant runtime connection authenticated as an unexpected database role"
        )


def validate_tenant_runtime_login(*, runtime_role: str) -> None:
    """Prove the sync tenant pool authenticates as the restricted runtime role.

    Workers use the same session path for jobs, so this also verifies PgBouncer
    connectivity and the transaction-local tenant context hook before work starts.
    """

    from src.infra.database.sessions import org_session_scope

    with org_session_scope(_RUNTIME_LOGIN_PROBE_ORGANISATION_ID) as session:
        current_user = session.scalar(text("SELECT current_user"))
    if current_user != runtime_role:
        raise TenantRuntimeCredentialError(
            "Tenant runtime connection authenticated as an unexpected database role"
        )


async def validate_current_shared_tenant_runtime_password(
    *,
    secrets_manager: _SecretsManager,
) -> TenantRuntimeConfig:
    """Validate credentials and the live catalog before the API accepts traffic."""

    config = validate_current_shared_tenant_runtime_startup()
    await load_tenant_runtime_password(config, secrets_manager=secrets_manager)
    await validate_current_tenant_runtime_catalog_async(runtime_role=config.user)
    await validate_tenant_runtime_login_async(runtime_role=config.user)
    return config


def validate_current_shared_tenant_runtime_password_sync(
    *,
    get_secret: Callable[[str], str | None],
) -> TenantRuntimeConfig:
    """Validate credentials and the live catalog before a worker accepts jobs."""

    config = validate_current_shared_tenant_runtime_startup()
    load_tenant_runtime_password_sync(config, get_secret=get_secret)
    validate_current_tenant_runtime_catalog(runtime_role=config.user)
    validate_tenant_runtime_login(runtime_role=config.user)
    return config
