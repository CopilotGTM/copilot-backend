"""Database-provider lifecycle contracts."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.infra.database import factory
from src.infra.database.provider_async import AsyncSqlAlchemyProvider
from src.infra.database.provider_sync import SyncSqlAlchemyProvider


def test_sync_provider_dispose_closes_both_current_engines() -> None:
    """Normal sync shutdown must close both privilege-bound pools."""

    provider = SyncSqlAlchemyProvider()
    master_engine = MagicMock()
    tenant_engine = MagicMock()
    provider._master_engine = master_engine
    provider._tenant_engine = tenant_engine

    provider.dispose()

    assert provider._master_engine is None
    assert provider._tenant_engine is None
    master_engine.dispose.assert_called_once_with()
    tenant_engine.dispose.assert_called_once_with()


def test_sync_provider_fork_reset_replaces_pools_without_closing_parent_connections() -> None:
    """A child must replace inherited pools without disrupting its parent."""

    provider = SyncSqlAlchemyProvider()
    master_engine = MagicMock()
    tenant_engine = MagicMock()
    provider._master_engine = master_engine
    provider._tenant_engine = tenant_engine

    provider.reset_after_fork()

    assert provider._master_engine is None
    assert provider._tenant_engine is None
    master_engine.dispose.assert_called_once_with(close=False)
    tenant_engine.dispose.assert_called_once_with(close=False)


@pytest.mark.asyncio
async def test_async_provider_dispose_closes_both_current_engines() -> None:
    """Normal async shutdown must await closure of both pools."""

    provider = AsyncSqlAlchemyProvider()
    master_engine = MagicMock()
    master_engine.dispose = AsyncMock()
    tenant_engine = MagicMock()
    tenant_engine.dispose = AsyncMock()
    provider._master_engine = master_engine
    provider._tenant_engine = tenant_engine

    await provider.dispose()

    assert provider._master_engine is None
    assert provider._tenant_engine is None
    master_engine.dispose.assert_awaited_once_with()
    tenant_engine.dispose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_async_dispose_waits_for_in_flight_tenant_engine_creation(
    monkeypatch,
) -> None:
    """Shutdown must not race with a slow Key Vault credential load."""

    provider = AsyncSqlAlchemyProvider()
    credential_load_started = asyncio.Event()
    release_credentials = asyncio.Event()
    engine = MagicMock()
    engine.dispose = AsyncMock()

    async def load_credentials() -> tuple[str, str]:
        """Hold lazy creation open until shutdown is waiting on its lock."""

        credential_load_started.set()
        await release_credentials.wait()
        return "app_tenant_runtime", "secret"

    monkeypatch.setattr(provider, "_require_shared_tenant_credentials", load_credentials)
    monkeypatch.setattr(
        provider,
        "_create_tenant_engine",
        lambda user, password: engine,
    )

    create_task = asyncio.create_task(provider._get_tenant_engine(MagicMock()))
    await credential_load_started.wait()
    dispose_task = asyncio.create_task(provider.dispose())
    await asyncio.sleep(0)

    assert not dispose_task.done()

    release_credentials.set()
    assert await create_task is engine
    await dispose_task

    assert provider._tenant_engine is None
    engine.dispose.assert_awaited_once_with()


def test_async_provider_fork_reset_replaces_pools_without_event_loop_io() -> None:
    """Fork reset must not await engines tied to the inherited event loop."""

    provider = AsyncSqlAlchemyProvider()
    master_engine = MagicMock()
    tenant_engine = MagicMock()
    provider._master_engine = master_engine
    provider._tenant_engine = tenant_engine

    provider.reset_after_fork()

    assert provider._master_engine is None
    assert provider._tenant_engine is None
    master_engine.sync_engine.dispose.assert_called_once_with(close=False)
    tenant_engine.sync_engine.dispose.assert_called_once_with(close=False)


def test_factory_fork_reset_detaches_singletons_without_closing_parent_connections(
    monkeypatch,
) -> None:
    """Factory reset must discard both provider singletons in a child."""

    sync_provider = MagicMock()
    async_provider = MagicMock()
    monkeypatch.setattr(factory, "_sync_instance", sync_provider)
    monkeypatch.setattr(factory, "_async_instance", async_provider)

    factory.reset_async_database_provider_after_fork()
    factory.reset_sync_database_provider_after_fork()

    assert factory._sync_instance is None
    assert factory._async_instance is None
    sync_provider.reset_after_fork.assert_called_once_with()
    async_provider.reset_after_fork.assert_called_once_with()


def test_factory_closes_sync_provider_after_parent_validation(monkeypatch) -> None:
    """The Celery parent must release pools opened only for readiness checks."""

    sync_provider = MagicMock()
    monkeypatch.setattr(factory, "_sync_instance", sync_provider)

    factory.close_sync_database_provider()

    assert factory._sync_instance is None
    sync_provider.dispose.assert_called_once_with()


@pytest.mark.asyncio
async def test_factory_actively_closes_only_the_async_provider(monkeypatch) -> None:
    """Loop recovery must await async disposal without disturbing sync pools."""

    sync_provider = MagicMock()
    async_provider = MagicMock()
    async_provider.dispose = AsyncMock()
    monkeypatch.setattr(factory, "_sync_instance", sync_provider)
    monkeypatch.setattr(factory, "_async_instance", async_provider)

    await factory.close_async_database_provider()

    assert factory._sync_instance is sync_provider
    assert factory._async_instance is None
    sync_provider.dispose.assert_not_called()
    async_provider.dispose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_factory_shutdown_detaches_and_closes_both_providers(monkeypatch) -> None:
    """API shutdown must close sync and async providers exactly once."""

    sync_provider = MagicMock()
    async_provider = MagicMock()
    async_provider.dispose = AsyncMock()
    monkeypatch.setattr(factory, "_sync_instance", sync_provider)
    monkeypatch.setattr(factory, "_async_instance", async_provider)

    await factory.close_database_providers()

    assert factory._sync_instance is None
    assert factory._async_instance is None
    sync_provider.dispose.assert_called_once_with()
    async_provider.dispose.assert_awaited_once_with()
