"""Independent synchronous database provider (no legacy dependency).

Creates and caches:
  - A single master engine (using DATABASE_USER / DATABASE_PASSWORD)
  - Per-tenant engines with per-tenant login/password

Responsibilities kept minimal: session lifecycle + engine invalidation.
Schema selection is applied at connection creation time via search_path.
"""

from __future__ import annotations

import asyncio
import threading
import urllib.parse
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from uuid import UUID

from sqlalchemy import create_engine
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


class SyncSqlAlchemyProvider(SyncDatabaseProvider):
    def __init__(self, *, tenant_pool_max: int = 100, tenant_pool_multiplier: float = 5.0):
        # Global lock for master and tenant engine management
        self._lock = threading.RLock()
        self._tenant_locks: dict[UUID, threading.Lock] = {}

        # Master and tenant engines
        self._master_engine: Engine | None = None
        self._SessionFactory: sessionmaker | None = None

        # Inline minimal tenant engine cache (LRU)
        self._tenant_engines: dict[UUID, Engine] = {}
        self._tenant_pool_order: list[UUID] = []
        self._tenant_pool_max = tenant_pool_max
        self._pool_multiplier = tenant_pool_multiplier

    def _get_tenant_lock(self, org_id: UUID) -> threading.Lock:
        with self._lock:
            lock = self._tenant_locks.get(org_id)
            if lock is None:
                lock = threading.Lock()
                self._tenant_locks[org_id] = lock
            return lock

    def _get_master_engine(self):
        if self._master_engine is not None:
            return self._master_engine
        with self._lock:
            if self._master_engine is None:
                url = self._build_master_url()
                self._master_engine = create_engine(
                    url,
                    echo=False,
                    pool_pre_ping=True,
                    pool_recycle=database_config.POOL_RECYCLE_SEC,
                    pool_timeout=database_config.DATABASE_POOL_TIMEOUT_SEC,
                    pool_size=database_config.DATABASE_POOL_SIZE,
                    max_overflow=database_config.DATABASE_MAX_OVERFLOW,
                    connect_args={
                        "sslmode": database_config.DATABASE_SSL_MODE,
                        "connect_timeout": database_config.DATABASE_CONNECT_TIMEOUT_SEC,
                        "keepalives": 1,
                        "keepalives_idle": 30,
                        "keepalives_interval": 10,
                        "keepalives_count": 5,
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
                if org_id in self._tenant_pool_order:
                    self._tenant_pool_order.remove(org_id)
                self._tenant_pool_order.append(org_id)
                return engine

        # Create new engine
        lock = self._get_tenant_lock(org_id)
        with lock:
            with self._lock:
                engine = self._tenant_engines.get(org_id)
                if engine:
                    if org_id in self._tenant_pool_order:
                        self._tenant_pool_order.remove(org_id)
                    self._tenant_pool_order.append(org_id)
                    return engine

            logger.debug(f"Creating new engine for org_id: {org_id}")
            role_name = get_role_name(org_id)
            secret_name = get_key_vault_secret_name(org_id)

            # Single, simple fetch path (fallback is alias to get in simplified manager)
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

            eng_new = self._build_tenant_engine(org_id, role_name, password)

            # Cache + LRU
            with self._lock:
                self._tenant_engines[org_id] = eng_new
                if org_id in self._tenant_pool_order:
                    self._tenant_pool_order.remove(org_id)
                self._tenant_pool_order.append(org_id)
                while len(self._tenant_engines) > self._tenant_pool_max:
                    evict_id = self._tenant_pool_order.pop(0)
                    old = self._tenant_engines.pop(evict_id, None)
                    try:
                        if old:
                            old.dispose()
                    except Exception:
                        pass
            return eng_new

    def _build_tenant_engine(self, org_uuid: UUID, role_name: str, password: str) -> Engine:
        host = database_config.DATABASE_HOST
        port = database_config.DATABASE_PORT
        db_name = database_config.DATABASE_NAME
        if isinstance(password, bytes):
            password = password.decode("utf-8")
        pw = urllib.parse.quote_plus(password)
        url = f"postgresql://{role_name}:{pw}@{host}:{port}/{db_name}"

        base_size = database_config.DATABASE_POOL_SIZE or 5
        overflow = database_config.DATABASE_MAX_OVERFLOW or 0
        pool_size = max(1, int(base_size * self._pool_multiplier))
        max_overflow = max(0, int(overflow * self._pool_multiplier))

        schema = get_schema_name(org_uuid)
        options_parts = [f"-c statement_timeout={database_config.DATABASE_STATEMENT_TIMEOUT_MS}"]
        if schema:
            options_parts.insert(0, f'-c search_path="{schema}",public ')
        options_str = "".join(options_parts)

        return create_engine(
            url,
            echo=False,
            pool_pre_ping=True,
            pool_recycle=database_config.POOL_RECYCLE_SEC,
            pool_timeout=database_config.DATABASE_POOL_TIMEOUT_SEC,
            pool_size=pool_size,
            max_overflow=max_overflow,
            connect_args={
                "sslmode": database_config.DATABASE_SSL_MODE,
                "connect_timeout": database_config.DATABASE_CONNECT_TIMEOUT_SEC,
                "options": options_str,
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 5,
            },
        )

    def _parse_tenant_info(self, org_id: str) -> tuple[uuid.UUID, str, str]:
        """Parse org_id and generate tenant-specific names."""
        try:
            org_uuid = uuid.UUID(org_id)
        except Exception as e:
            raise ValueError(f"Invalid organisation id: {org_id}") from e

        role_name = get_role_name(org_uuid)
        secret_name = get_key_vault_secret_name(org_uuid)
        return org_uuid, role_name, secret_name

    def _fetch_tenant_password(self, secret_name: str) -> str:
        """Fetch tenant password synchronously via the simplified secrets manager."""
        try:
            value = asyncio.run(secrets_manager.get(secret_name))
        except Exception as e:
            logger.error("Secret fetch failed for '%s': %s", secret_name, e)
            value = None
        if not value:
            raise RuntimeError(f"Password secret '{secret_name}' unavailable")
        return value

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
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def invalidate_tenant(self, org_id: str) -> None:
        try:
            org_uuid = uuid.UUID(org_id)
        except Exception:
            return
        with self._lock:
            eng = self._tenant_engines.pop(org_uuid, None)
            if org_uuid in self._tenant_pool_order:
                self._tenant_pool_order.remove(org_uuid)
        if eng:
            with suppress(Exception):
                eng.dispose()


__all__ = ["SyncSqlAlchemyProvider"]
