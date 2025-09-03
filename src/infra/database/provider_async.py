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

from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncEngine,
    async_sessionmaker,
    AsyncSession,
)

from src.infra.database.interface import AsyncDatabaseProvider
from src.infra.config.configs import database_config
from src.infra.secrets.manager import secrets_manager
from src.infra.utils.resource_naming import get_role_name, get_key_vault_secret_name


class AsyncSqlAlchemyProvider(AsyncDatabaseProvider):
    def __init__(self, *, tenant_pool_max: int = 100, tenant_pool_multiplier: float = 5.0):
        self._lock = threading.RLock()
        self._master_engine: AsyncEngine | None = None
        self._master_factory: async_sessionmaker[AsyncSession] | None = None
        self._tenant_engines: Dict[str, AsyncEngine] = {}
        self._tenant_pool_order: list[str] = []  # naive LRU ordering
        self._tenant_pool_max = tenant_pool_max
        self._pool_multiplier = tenant_pool_multiplier

    # Master -----------------------------------------------------------
    def _ensure_master(self):
        if self._master_engine is not None:
            return
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
                    connect_args={"sslmode": database_config.DATABASE_SSL_MODE},
                )
                self._master_factory = async_sessionmaker(self._master_engine, expire_on_commit=False)

    def _build_master_url(self) -> str:
        host = database_config.DATABASE_HOST
        port = database_config.DATABASE_PORT
        db = database_config.DATABASE_NAME
        user = urllib.parse.quote_plus(database_config.DATABASE_USER)
        pw = urllib.parse.quote_plus(database_config.DATABASE_PASSWORD)
        return f"postgresql+asyncpg://{user}:{pw}@{host}:{port}/{db}"

    # Tenant -----------------------------------------------------------
    def _get_tenant_engine(self, org_id: str) -> AsyncEngine:
        role_name = get_role_name(org_id)
        with self._lock:
            eng = self._tenant_engines.get(role_name)
            if eng:
                # LRU touch
                if role_name in self._tenant_pool_order:
                    self._tenant_pool_order.remove(role_name)
                self._tenant_pool_order.append(role_name)
                return eng
        # create outside lock partly (URL build + engine create is thread-safe)
        secret_name = get_key_vault_secret_name(role_name)
        password = secrets_manager.get(secret_name)  # sync shim allowed
        url = self._build_tenant_url(role_name, password)
        base_size = database_config.DATABASE_POOL_SIZE or 5
        overflow = database_config.DATABASE_MAX_OVERFLOW or 0
        pool_size = max(1, int(base_size * self._pool_multiplier))
        max_overflow = max(0, int(overflow * self._pool_multiplier))
        engine = create_async_engine(
            url,
            echo=False,
            pool_pre_ping=True,
            pool_recycle=database_config.POOL_RECYCLE_SEC,
            pool_size=pool_size,
            max_overflow=max_overflow,
            connect_args={"sslmode": database_config.DATABASE_SSL_MODE},
        )
        with self._lock:
            self._tenant_engines[role_name] = engine
            if role_name in self._tenant_pool_order:
                self._tenant_pool_order.remove(role_name)
            self._tenant_pool_order.append(role_name)
            while len(self._tenant_pool_order) > self._tenant_pool_max:
                oldest = self._tenant_pool_order.pop(0)
                old_engine = self._tenant_engines.pop(oldest, None)
                if old_engine:
                    asyncio.get_event_loop().create_task(old_engine.dispose())
        return engine

    def _build_tenant_url(self, user: str, password: str) -> str:
        host = database_config.DATABASE_HOST
        port = database_config.DATABASE_PORT
        db = database_config.DATABASE_NAME
        pw = urllib.parse.quote_plus(password)
        return f"postgresql+asyncpg://{user}:{pw}@{host}:{port}/{db}"

    # Public Provider API ---------------------------------------------
    @asynccontextmanager
    async def master_session(self) -> AsyncIterator[AsyncSession]:
        self._ensure_master()
        assert self._master_factory is not None
        session: AsyncSession = self._master_factory()  # type: ignore
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    @asynccontextmanager
    async def tenant_session(self, org_id: str) -> AsyncIterator[AsyncSession]:
        engine = self._get_tenant_engine(org_id)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        session: AsyncSession = factory()  # type: ignore
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def invalidate_tenant(self, org_id: str) -> None:
        role_name = get_role_name(org_id)
        with self._lock:
            eng = self._tenant_engines.pop(role_name, None)
            if role_name in self._tenant_pool_order:
                self._tenant_pool_order.remove(role_name)
        if eng:
            await eng.dispose()

__all__ = ["AsyncSqlAlchemyProvider"]
