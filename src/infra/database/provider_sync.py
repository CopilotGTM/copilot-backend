"""Independent synchronous database provider (pgbouncer-aware).

Creates and caches:
  - A single master engine (using DATABASE_USER / DATABASE_PASSWORD)
  - Per-tenant engines with per-tenant login/password (cached to avoid
    repeated Key Vault lookups)

All engines use QueuePool to cache TCP connections to pgbouncer.
pgbouncer handles backend PostgreSQL connection pooling.
Schema selection and statement_timeout are applied per-session via SET
commands (pgbouncer rejects startup parameters in transaction mode).
"""

from __future__ import annotations

import asyncio
import threading
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as SASession
from sqlalchemy.orm import sessionmaker
from src.infra.config.configs import database_config
from src.infra.database.interface import SyncDatabaseProvider
from src.infra.secrets.manager import secrets_manager
from src.infra.utils.logger import get_logger
from src.infra.utils.resource_naming import (
    get_key_vault_secret_name,
    get_role_name,
    get_schema_name,
)

logger = get_logger("sync_db_provider")

# Pool settings for connections to pgbouncer (not to PostgreSQL directly).
_POOL_SIZE = int(database_config.DATABASE_POOL_SIZE)
_MAX_OVERFLOW = int(database_config.DATABASE_MAX_OVERFLOW)
_POOL_RECYCLE = int(database_config.POOL_RECYCLE_SEC)


class SyncSqlAlchemyProvider(SyncDatabaseProvider):
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tenant_locks: dict[UUID, threading.Lock] = {}

        self._master_engine: Engine | None = None

        # Tenant engines are cached to avoid Key Vault fetches on every request.
        self._tenant_engines: dict[UUID, Engine] = {}

    def _get_tenant_lock(self, org_id: UUID) -> threading.Lock:
        with self._lock:
            lock = self._tenant_locks.get(org_id)
            if lock is None:
                lock = threading.Lock()
                self._tenant_locks[org_id] = lock
            return lock

    def _get_master_engine(self) -> Engine:
        if self._master_engine is not None:
            return self._master_engine
        with self._lock:
            if self._master_engine is None:
                url = self._build_master_url()
                self._master_engine = create_engine(
                    url,
                    echo=False,
                    pool_size=_POOL_SIZE,
                    max_overflow=_MAX_OVERFLOW,
                    pool_recycle=_POOL_RECYCLE,
                    pool_pre_ping=True,
                    connect_args={
                        "sslmode": database_config.DATABASE_SSL_MODE,
                        "connect_timeout": database_config.DATABASE_CONNECT_TIMEOUT_SEC,
                    },
                )
            return self._master_engine

    def _build_master_url(self) -> str:
        host = database_config.DATABASE_HOST
        port = database_config.DATABASE_PORT
        db = database_config.DATABASE_NAME
        user = urllib.parse.quote_plus(database_config.DATABASE_USER)
        pw = urllib.parse.quote_plus(database_config.DATABASE_PASSWORD)
        return f"postgresql://{user}:{pw}@{host}:{port}/{db}"

    # Tenant
    def _get_tenant_engine(self, org_id: UUID) -> Engine:
        """Get or create the SQLAlchemy engine for a tenant."""
        with self._lock:
            engine = self._tenant_engines.get(org_id)
            if engine:
                return engine

        # Serialize creation per tenant to prevent duplicate Key Vault calls
        lock = self._get_tenant_lock(org_id)
        with lock:
            with self._lock:
                engine = self._tenant_engines.get(org_id)
                if engine:
                    return engine

            logger.debug(f"Creating new engine for org_id: {org_id}")
            role_name = get_role_name(org_id)
            secret_name = get_key_vault_secret_name(org_id)

            try:
                password = asyncio.run(secrets_manager.get_with_fallback(secret_name))
                if not password:
                    password = asyncio.run(secrets_manager.get(secret_name))
            except Exception as e:
                logger.error("Secret fetch failed for '%s': %s", secret_name, e)
                password = None

            if not password:
                logger.error(f"Password secret '{secret_name}' unavailable for org_id {org_id}")
                raise RuntimeError(f"Tenant secret '{secret_name}' unavailable")

            eng_new = self._build_tenant_engine(role_name, password)

            with self._lock:
                self._tenant_engines[org_id] = eng_new
            return eng_new

    def _build_tenant_engine(self, role_name: str, password: str) -> Engine:
        host = database_config.DATABASE_HOST
        port = database_config.DATABASE_PORT
        db_name = database_config.DATABASE_NAME
        if isinstance(password, bytes):
            password = password.decode("utf-8")
        pw = urllib.parse.quote_plus(password)
        url = f"postgresql://{role_name}:{pw}@{host}:{port}/{db_name}"

        return create_engine(
            url,
            echo=False,
            pool_size=_POOL_SIZE,
            max_overflow=_MAX_OVERFLOW,
            pool_recycle=_POOL_RECYCLE,
            pool_pre_ping=True,
            connect_args={
                "sslmode": database_config.DATABASE_SSL_MODE,
                "connect_timeout": database_config.DATABASE_CONNECT_TIMEOUT_SEC,
            },
        )

    # Public Provider API ---------------------------------------------
    @contextmanager
    def master_session(self) -> Iterator[SASession]:
        master_engine = self._get_master_engine()
        session_local = sessionmaker(bind=master_engine, expire_on_commit=False, class_=SASession)
        session: SASession = session_local()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @contextmanager
    def tenant_session(self, org_id: UUID) -> Iterator[SASession]:
        engine = self._get_tenant_engine(org_id)
        session_local = sessionmaker(bind=engine, expire_on_commit=False, class_=SASession)
        session: SASession = session_local()
        try:
            schema = get_schema_name(org_id)
            session.execute(text(f'SET search_path TO "{schema}", public'))
            session.execute(
                text(f"SET statement_timeout = {database_config.DATABASE_STATEMENT_TIMEOUT_MS}")
            )
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def invalidate_tenant(self, org_id: UUID) -> None:
        with self._lock:
            eng = self._tenant_engines.pop(org_id, None)
        if eng:
            with suppress(Exception):
                eng.dispose()


__all__ = ["SyncSqlAlchemyProvider"]
