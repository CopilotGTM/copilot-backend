"""Independent async database provider (no legacy dependency).

Creates:
  - One master AsyncEngine
  - Async tenant engines cached with simple LRU (reuse sync TenantPoolManager logic not yet async aware)

Each tenant engine uses asyncpg driver.
"""

from __future__ import annotations

import asyncio
import threading
import urllib.parse
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict
from uuid import UUID

from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncEngine,
    async_sessionmaker,
    AsyncSession,
)

from src.infra.database.interface import AsyncDatabaseProvider
from src.infra.config.configs import database_config
from src.infra.secrets.manager import secrets_manager
from src.infra.utils.resource_naming import (
    get_key_vault_secret_name,
    get_role_name,
    get_schema_name,
)
from src.infra.utils.logger import get_logger

logger = get_logger("async_db_provider")


class AsyncSqlAlchemyProvider(AsyncDatabaseProvider):
    def __init__(
        self, *, tenant_pool_max: int = 100, tenant_pool_multiplier: float = 1.0
    ):

        # Global and per-tenant locks
        self._lock = threading.RLock()
        self._tenant_locks: Dict[UUID, asyncio.Lock] = {}

        # Master and tenant engines
        self._master_engine: AsyncEngine | None = None

        # Inline minimal tenant engine cache (LRU)
        self._tenant_engines: Dict[UUID, AsyncEngine] = {}
        self._tenant_pool_order: list[UUID] = []
        self._tenant_pool_max = tenant_pool_max
        self._pool_multiplier = tenant_pool_multiplier

    def _get_tenant_lock(self, org_id: UUID) -> asyncio.Lock:
        with self._lock:
            lock = self._tenant_locks.get(org_id)
            if lock is None:
                lock = asyncio.Lock()
                self._tenant_locks[org_id] = lock
            return lock

    # Master -----------------------------------------------------------
    def _get_master_engine(self):
        if self._master_engine is not None:
            return self._master_engine
        with self._lock:
            if self._master_engine is None:
                url = self._build_master_url()
                self._master_engine = create_async_engine(
                    url,
                    echo=False,
                    pool_pre_ping=True,
                    pool_recycle=database_config.POOL_RECYCLE_SEC,
                    pool_size=database_config.DATABASE_POOL_SIZE,
                    max_overflow=database_config.DATABASE_MAX_OVERFLOW,
                    connect_args={
                        "ssl": (
                            False
                            if database_config.DATABASE_HOST == "localhost"
                            else True
                        ),
                        "timeout": database_config.DATABASE_CONNECT_TIMEOUT_SEC,
                    },
                )
                return self._master_engine

    def _build_master_url(self) -> str:
        """Build the async database URL for the master connection."""
        host = database_config.DATABASE_HOST
        port = database_config.DATABASE_PORT
        db = database_config.DATABASE_NAME
        user = urllib.parse.quote_plus(database_config.DATABASE_USER)
        pw = urllib.parse.quote_plus(database_config.DATABASE_PASSWORD)
        return f"postgresql+asyncpg://{user}:{pw}@{host}:{port}/{db}"

    def _build_tenant_url(self, user: str, password: str) -> str:
        """Build the async database URL for a tenant."""
        host = database_config.DATABASE_HOST
        port = database_config.DATABASE_PORT
        db = database_config.DATABASE_NAME
        pw = urllib.parse.quote_plus(password)
        return f"postgresql+asyncpg://{user}:{pw}@{host}:{port}/{db}"

    # Tenant -----------------------------------------------------------
    async def _get_tenant_engine(self, org_id: UUID) -> AsyncEngine:
        """Get or create a tenant engine for the given org_id."""
        with self._lock:
            engine = self._tenant_engines.get(org_id)
            if engine:
                logger.debug(f"Reusing existing engine for org_id: {org_id}")
                if org_id in self._tenant_pool_order:
                    self._tenant_pool_order.remove(org_id)
                self._tenant_pool_order.append(org_id)
                return engine

        # Serialize creation for this tenant to prevent duplicate KV calls/engines
        lock = self._get_tenant_lock(org_id)
        async with lock:
            # Double-check after acquiring the per-tenant lock
            with self._lock:
                engine = self._tenant_engines.get(org_id)
                if engine:
                    logger.debug(
                        f"Reusing existing engine after lock for org_id: {org_id}"
                    )
                    if org_id in self._tenant_pool_order:
                        self._tenant_pool_order.remove(org_id)
                    self._tenant_pool_order.append(org_id)
                    return engine

            # Create new engine outside lock to avoid blocking other tenants
            logger.debug(f"Creating new engine for org_id: {org_id}")
            role_name = get_role_name(org_id)
            secret_name = get_key_vault_secret_name(org_id)

            # Single, simple fetch path (fallback is alias to get in simplified manager)
            password = await secrets_manager.get_with_fallback(secret_name)
            if not password:
                password = await secrets_manager.get(secret_name)

            if not password:
                logger.error(
                    f"Password secret '{secret_name}' unavailable for org_id {org_id}"
                )
                raise RuntimeError(f"Tenant secret '{secret_name}' unavailable")

            url = self._build_tenant_url(role_name, password)

            base_size = database_config.DATABASE_POOL_SIZE
            overflow = database_config.DATABASE_MAX_OVERFLOW
            pool_size = max(1, int(base_size * self._pool_multiplier))
            max_overflow = max(0, int(overflow * self._pool_multiplier))
            schema = get_schema_name(org_id)

            engine = create_async_engine(
                url,
                echo=False,
                pool_pre_ping=True,
                pool_recycle=database_config.POOL_RECYCLE_SEC,
                pool_size=pool_size,
                max_overflow=max_overflow,
                connect_args={
                    "ssl": (
                        False if database_config.DATABASE_HOST == "localhost" else True
                    ),
                    "timeout": database_config.DATABASE_CONNECT_TIMEOUT_SEC,
                    "server_settings": {"search_path": f'"{schema}",public'},
                },
            )

            # Add to cache and manage LRU eviction
            with self._lock:
                self._tenant_engines[org_id] = engine

                # Mark as recently used
                if org_id in self._tenant_pool_order:
                    self._tenant_pool_order.remove(org_id)
                self._tenant_pool_order.append(org_id)

            return engine

    # Public Provider API ---------------------------------------------
    @asynccontextmanager
    async def master_session(self) -> AsyncIterator[AsyncSession]:
        master_engine = self._get_master_engine()
        factory = async_sessionmaker(master_engine, expire_on_commit=False)
        session: AsyncSession = factory()
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    @asynccontextmanager
    async def tenant_session(self, org_id: UUID) -> AsyncIterator[AsyncSession]:
        logger.debug(f"Acquiring tenant session for org_id: {org_id}")
        engine = await self._get_tenant_engine(org_id)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        session: AsyncSession = factory()
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def invalidate_tenant(self, org_id: UUID) -> None:
        with self._lock:
            eng = self._tenant_engines.pop(org_id, None)
            if org_id in self._tenant_pool_order:
                self._tenant_pool_order.remove(org_id)
        if eng:
            await eng.dispose()


__all__ = ["AsyncSqlAlchemyProvider"]
