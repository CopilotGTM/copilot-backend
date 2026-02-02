"""Independent async database provider (pgbouncer-aware).

Creates:
  - One master AsyncEngine (NullPool — pgbouncer handles pooling)
  - Per-tenant AsyncEngines cached to avoid repeated Key Vault lookups
    (NullPool — no app-side connection pool, pgbouncer handles pooling)

Each engine uses asyncpg driver with statement_cache_size=0 for
pgbouncer transaction-mode compatibility.
"""

from __future__ import annotations

import asyncio
import threading
import urllib.parse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from src.infra.config.configs import database_config
from src.infra.database.interface import AsyncDatabaseProvider
from src.infra.secrets.manager import secrets_manager
from src.infra.utils.logger import get_logger
from src.infra.utils.resource_naming import (
    get_key_vault_secret_name,
    get_role_name,
    get_schema_name,
)

logger = get_logger("async_db_provider")


class AsyncSqlAlchemyProvider(AsyncDatabaseProvider):
    """Async SQLAlchemy database provider (pgbouncer-aware, NullPool)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tenant_locks: dict[UUID, asyncio.Lock] = {}

        self._master_engine: AsyncEngine | None = None

        # Tenant engines are cached to avoid Key Vault fetches on every request.
        # NullPool means these hold no connections — just connection config.
        self._tenant_engines: dict[UUID, AsyncEngine] = {}

    def _get_tenant_lock(self, org_id: UUID) -> asyncio.Lock:
        with self._lock:
            lock = self._tenant_locks.get(org_id)
            if lock is None:
                lock = asyncio.Lock()
                self._tenant_locks[org_id] = lock
            return lock

    # Master -----------------------------------------------------------
    def _get_master_engine(self) -> AsyncEngine:
        if self._master_engine is not None:
            return self._master_engine
        with self._lock:
            if self._master_engine is None:
                url = self._build_master_url()
                self._master_engine = create_async_engine(
                    url,
                    echo=False,
                    poolclass=NullPool,
                    connect_args={
                        "ssl": (database_config.DATABASE_HOST != "localhost"),
                        "timeout": database_config.DATABASE_CONNECT_TIMEOUT_SEC,
                        "statement_cache_size": 0,
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
                return engine

        # Serialize creation per tenant to prevent duplicate Key Vault calls
        lock = self._get_tenant_lock(org_id)
        async with lock:
            # Double-check after acquiring the per-tenant lock
            with self._lock:
                engine = self._tenant_engines.get(org_id)
                if engine:
                    return engine

            logger.debug("Creating new engine for org_id: %s", org_id)
            role_name = get_role_name(org_id)
            secret_name = get_key_vault_secret_name(org_id)

            password = await secrets_manager.get_with_fallback(secret_name)
            if not password:
                password = await secrets_manager.get(secret_name)

            if not password:
                logger.error("Password secret '%s' unavailable for org_id %s", secret_name, org_id)
                raise RuntimeError(f"Tenant secret '{secret_name}' unavailable")

            url = self._build_tenant_url(role_name, password)
            schema = get_schema_name(org_id)

            engine = create_async_engine(
                url,
                echo=False,
                poolclass=NullPool,
                connect_args={
                    "ssl": (database_config.DATABASE_HOST != "localhost"),
                    "timeout": database_config.DATABASE_CONNECT_TIMEOUT_SEC,
                    "statement_cache_size": 0,
                    "server_settings": {"search_path": f'"{schema}",public'},
                },
            )

            with self._lock:
                self._tenant_engines[org_id] = engine

            return engine

    # Public Provider API ---------------------------------------------
    @asynccontextmanager
    async def master_session(self) -> AsyncIterator[AsyncSession]:  # pylint: disable=invalid-overridden-method
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
    async def tenant_session(self, org_id: UUID) -> AsyncIterator[AsyncSession]:  # pylint: disable=invalid-overridden-method
        logger.debug("Acquiring tenant session for org_id: %s", org_id)
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
        if eng:
            await eng.dispose()


__all__ = ["AsyncSqlAlchemyProvider"]
