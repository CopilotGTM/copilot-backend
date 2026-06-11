"""Independent async database provider (pgbouncer-aware).

Creates:
  - One master AsyncEngine (QueuePool — caches TCP connections to pgbouncer)
  - One shared tenant AsyncEngine when shared tenant DB mode is enabled
  - Per-tenant AsyncEngines cached in legacy mode to avoid repeated Key Vault lookups
    (QueuePool — pgbouncer handles backend connection pooling,
     app-side pool caches TCP connections to pgbouncer)

pgbouncer transaction-mode compatibility:
  - statement_cache_size=0 (asyncpg) — disables asyncpg's statement cache
  - prepared_statement_cache_size=0 (SQLAlchemy adapter) — disables SQLAlchemy's
    own prepared statement cache
  - prepared_statement_name_func=lambda: '' — forces unnamed (anonymous) prepared
    statements.  Required because asyncpg 0.30 changed semantics: name=None now
    means "auto-generate a name" (__asyncpg_stmt_N__), which collides across
    pgbouncer backend connections.  Empty string forces truly anonymous statements.
  - pool_pre_ping=True — detects stale connections safely
  - search_path set with SET LOCAL at each transaction start
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
from src.infra.config.configs import database_config
from src.infra.database.interface import AsyncDatabaseProvider
from src.infra.database.tenant_runtime_config import validate_tenant_runtime_config
from src.infra.database.tenant_runtime_credentials import load_tenant_runtime_password
from src.infra.database.tenant_search_path import (
    TENANT_SCHEMA_SESSION_KEY,
    TENANT_SEARCH_PATH_SESSION_KEY,
    TRUSTED_EXTENSION_SCHEMA,
    apply_tenant_transaction_context,
    install_tenant_transaction_context_listener,
)
from src.infra.secrets.manager import master_secrets_manager, secrets_manager
from src.infra.utils.logger import get_logger
from src.infra.utils.resource_naming import (
    get_key_vault_secret_name,
    get_role_name,
    get_schema_name,
)

logger = get_logger("async_db_provider")

# Pool settings for connections to pgbouncer (not to PostgreSQL directly).
# pgbouncer handles backend pooling; these just cache TCP connections.
_POOL_SIZE = int(database_config.DATABASE_POOL_SIZE)
_MAX_OVERFLOW = int(database_config.DATABASE_MAX_OVERFLOW)
_POOL_TIMEOUT = int(database_config.DATABASE_POOL_TIMEOUT_SEC)
_POOL_RECYCLE = int(database_config.POOL_RECYCLE_SEC)
install_tenant_transaction_context_listener()


def _unnamed_stmt() -> str:
    """Return empty string to force anonymous prepared statements.

    asyncpg 0.30 changed prepare() semantics: name=None now means
    "auto-generate a name" (__asyncpg_stmt_N__), which causes
    DuplicatePreparedStatementError with pgbouncer.  Empty string
    forces truly anonymous (unnamed) prepared statements.
    """
    return ""


class AsyncSqlAlchemyProvider(AsyncDatabaseProvider):
    """Async SQLAlchemy database provider (pgbouncer-aware, QueuePool)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tenant_locks: dict[UUID, asyncio.Lock] = {}

        self._master_engine: AsyncEngine | None = None
        self._shared_tenant_engine: AsyncEngine | None = None

        # Tenant engines are cached to avoid Key Vault fetches on every request.
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
                    pool_size=_POOL_SIZE,
                    max_overflow=_MAX_OVERFLOW,
                    pool_timeout=_POOL_TIMEOUT,
                    pool_recycle=_POOL_RECYCLE,
                    pool_pre_ping=True,
                    connect_args={
                        "ssl": (database_config.DATABASE_HOST != "localhost"),
                        "timeout": database_config.DATABASE_CONNECT_TIMEOUT_SEC,
                        "statement_cache_size": 0,
                        "prepared_statement_cache_size": 0,
                        "prepared_statement_name_func": _unnamed_stmt,
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
        user = urllib.parse.quote_plus(user)
        pw = urllib.parse.quote_plus(password)
        return f"postgresql+asyncpg://{user}:{pw}@{host}:{port}/{db}"

    def _create_tenant_engine(self, user: str, password: str) -> AsyncEngine:
        return create_async_engine(
            self._build_tenant_url(user, password),
            echo=False,
            pool_size=_POOL_SIZE,
            max_overflow=_MAX_OVERFLOW,
            pool_timeout=_POOL_TIMEOUT,
            pool_recycle=_POOL_RECYCLE,
            pool_pre_ping=True,
            connect_args={
                "ssl": (database_config.DATABASE_HOST != "localhost"),
                "timeout": database_config.DATABASE_CONNECT_TIMEOUT_SEC,
                "statement_cache_size": 0,
                "prepared_statement_cache_size": 0,
                "prepared_statement_name_func": _unnamed_stmt,
            },
        )

    async def _require_shared_tenant_credentials(self) -> tuple[str, str]:
        config = validate_tenant_runtime_config(
            database_user=database_config.DATABASE_USER,
            tenant_database_user=database_config.TENANT_DATABASE_USER,
            tenant_password=database_config.TENANT_DATABASE_PASSWORD,
            tenant_password_secret_name=database_config.TENANT_DATABASE_PASSWORD_SECRET_NAME,
        )
        password = await load_tenant_runtime_password(
            config,
            secrets_manager=master_secrets_manager,
        )
        return config.user, password

    # Tenant -----------------------------------------------------------
    async def _get_tenant_engine(self, org_id: UUID) -> AsyncEngine:
        """Get or create a tenant engine for the given org_id."""
        if database_config.USE_SHARED_TENANT_DATABASE:
            with self._lock:
                if self._shared_tenant_engine is not None:
                    return self._shared_tenant_engine

            async with self._get_tenant_lock(UUID(int=0)):
                with self._lock:
                    if self._shared_tenant_engine is not None:
                        return self._shared_tenant_engine

                user, password = await self._require_shared_tenant_credentials()
                logger.debug("Creating shared tenant engine for org_id: %s", org_id)
                engine = self._create_tenant_engine(user, password)

                with self._lock:
                    self._shared_tenant_engine = engine
                return engine

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
                logger.error(
                    "Tenant secret unavailable",
                    extra={
                        "event": "secret_fetch_failed",
                        "secret_name": secret_name,
                        "organisation_id": str(org_id),
                    },
                )
                raise RuntimeError(f"Tenant secret '{secret_name}' unavailable")

            engine = self._create_tenant_engine(role_name, password)

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
            schema = get_schema_name(org_id)
            session.sync_session.info[TENANT_SCHEMA_SESSION_KEY] = schema
            search_path = (
                (schema, TRUSTED_EXTENSION_SCHEMA)
                if database_config.USE_SHARED_TENANT_DATABASE
                else (schema, TRUSTED_EXTENSION_SCHEMA, "public")
            )
            session.sync_session.info[TENANT_SEARCH_PATH_SESSION_KEY] = search_path
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def invalidate_tenant(self, org_id: UUID) -> None:
        with self._lock:
            eng = self._tenant_engines.pop(org_id, None)
            shared_engine = self._shared_tenant_engine
            self._shared_tenant_engine = None
        if eng:
            await eng.dispose()
        if shared_engine:
            await shared_engine.dispose()


__all__ = ["AsyncSqlAlchemyProvider", "apply_tenant_transaction_context"]
