"""Independent async database provider (pgbouncer-aware).

Creates:
  - One master AsyncEngine (QueuePool — caches TCP connections to pgbouncer)
  - One tenant AsyncEngine shared by every organisation

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
import time
import urllib.parse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
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
    DATABASE_RUNTIME_SESSION_KEY,
    TENANT_DATA_SCHEMA,
    TENANT_ORGANISATION_ID_SESSION_KEY,
    TENANT_SCHEMA_SESSION_KEY,
    TENANT_SEARCH_PATH_SESSION_KEY,
    TRUSTED_EXTENSION_SCHEMA,
    apply_tenant_transaction_context,
    classify_tenant_database_failure,
    install_tenant_transaction_context_listener,
    parse_tenant_organisation_id,
)
from src.infra.secrets.manager import master_secrets_manager
from src.infra.utils.logger import get_logger

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
        self._tenant_engine_lock = asyncio.Lock()

        self._master_engine: AsyncEngine | None = None
        self._tenant_engine: AsyncEngine | None = None

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
                    hide_parameters=True,
                    pool_size=_POOL_SIZE,
                    max_overflow=_MAX_OVERFLOW,
                    pool_timeout=_POOL_TIMEOUT,
                    pool_recycle=_POOL_RECYCLE,
                    pool_pre_ping=True,
                    pool_use_lifo=True,
                    pool_reset_on_return="rollback",
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
            hide_parameters=True,
            pool_size=_POOL_SIZE,
            max_overflow=_MAX_OVERFLOW,
            pool_timeout=_POOL_TIMEOUT,
            pool_recycle=_POOL_RECYCLE,
            pool_pre_ping=True,
            pool_use_lifo=True,
            pool_reset_on_return="rollback",
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
    async def _get_tenant_engine(self, _org_id: UUID) -> AsyncEngine:
        """Get or create the one RLS-bound organisation engine."""
        with self._lock:
            if self._tenant_engine is not None:
                return self._tenant_engine

        async with self._tenant_engine_lock:
            with self._lock:
                if self._tenant_engine is not None:
                    return self._tenant_engine

            user, password = await self._require_shared_tenant_credentials()
            logger.debug("Creating shared tenant engine")
            engine = self._create_tenant_engine(user, password)
            with self._lock:
                self._tenant_engine = engine
            return engine

    # Public Provider API ---------------------------------------------
    @asynccontextmanager
    async def master_session(self) -> AsyncIterator[AsyncSession]:  # pylint: disable=invalid-overridden-method
        """Yield a control-plane session without tenant schema or RLS context."""

        master_engine = self._get_master_engine()
        factory = async_sessionmaker(master_engine, expire_on_commit=False)
        session: AsyncSession = factory()
        try:
            session.sync_session.info[DATABASE_RUNTIME_SESSION_KEY] = True
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    @asynccontextmanager
    async def tenant_session(self, org_id: UUID | str) -> AsyncIterator[AsyncSession]:  # pylint: disable=invalid-overridden-method
        """Yield an org-scoped session with canonical transaction identity."""

        org_id = parse_tenant_organisation_id(org_id)
        started = time.perf_counter()
        logger.debug("Acquiring tenant session for org_id: %s", org_id)
        engine = await self._get_tenant_engine(org_id)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        session: AsyncSession = factory()
        try:
            session.sync_session.info[TENANT_ORGANISATION_ID_SESSION_KEY] = org_id
            session.sync_session.info[TENANT_SCHEMA_SESSION_KEY] = TENANT_DATA_SCHEMA
            session.sync_session.info[TENANT_SEARCH_PATH_SESSION_KEY] = (
                TENANT_DATA_SCHEMA,
                TRUSTED_EXTENSION_SCHEMA,
            )
            yield session
        except Exception as exc:
            with suppress(Exception):
                await session.rollback()
            failure_kind, sqlstate = classify_tenant_database_failure(exc)
            logger.error(
                "Shared tenant database operation failed",
                extra={
                    "event": "shared_tenancy_database",
                    "operation": "tenant_session",
                    "outcome": "failed",
                    "organisation_id": str(org_id),
                    "failure_kind": failure_kind,
                    "sqlstate": sqlstate,
                    "error_type": type(exc).__name__,
                    "duration_ms": round(
                        (time.perf_counter() - started) * 1000,
                        3,
                    ),
                },
            )
            raise
        finally:
            await session.close()

    async def invalidate_tenant(self, _org_id: UUID) -> None:
        """Discard the shared tenant pool after credentials or connectivity change."""

        # Serialize with lazy creation so invalidation cannot finish immediately
        # before an in-flight credential load publishes a replacement engine.
        async with self._tenant_engine_lock:
            with self._lock:
                engine = self._tenant_engine
                self._tenant_engine = None
        if engine:
            await engine.dispose()

    async def dispose(self) -> None:
        """Close every pool after any in-flight tenant creation completes."""

        # Shutdown waits on lazy tenant creation; otherwise that coroutine could
        # publish a live engine after the provider singleton was already closed.
        async with self._tenant_engine_lock:
            with self._lock:
                engines = (self._master_engine, self._tenant_engine)
                self._master_engine = None
                self._tenant_engine = None
        for engine in engines:
            if engine is not None:
                with suppress(Exception):
                    await engine.dispose()

    def reset_after_fork(self) -> None:
        """Replace inherited pools without doing event-loop I/O in the child."""

        with self._lock:
            engines = (self._master_engine, self._tenant_engine)
            self._master_engine = None
            self._tenant_engine = None
        for engine in engines:
            if engine is not None:
                with suppress(Exception):
                    engine.sync_engine.dispose(close=False)


__all__ = ["AsyncSqlAlchemyProvider", "apply_tenant_transaction_context"]
