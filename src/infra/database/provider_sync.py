"""Independent synchronous database provider (pgbouncer-aware).

Creates and caches:
  - A single master engine (using DATABASE_USER / DATABASE_PASSWORD)
  - One tenant engine shared by every organisation

All engines use QueuePool to cache TCP connections to pgbouncer.
pgbouncer handles backend PostgreSQL connection pooling.
Schema selection and statement_timeout are applied with SET LOCAL when each
transaction starts (pgbouncer rejects startup parameters in transaction mode).
"""

from __future__ import annotations

import threading
import time
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as SASession
from sqlalchemy.orm import sessionmaker
from src.infra.config.configs import database_config
from src.infra.database.interface import SyncDatabaseProvider
from src.infra.database.tenant_runtime_config import validate_tenant_runtime_config
from src.infra.database.tenant_runtime_credentials import load_tenant_runtime_password_sync
from src.infra.database.tenant_search_path import (
    DATABASE_RUNTIME_SESSION_KEY,
    TENANT_DATA_SCHEMA,
    TENANT_ORGANISATION_ID_SESSION_KEY,
    TENANT_SCHEMA_SESSION_KEY,
    TENANT_SEARCH_PATH_SESSION_KEY,
    TRUSTED_EXTENSION_SCHEMA,
    classify_tenant_database_failure,
    install_tenant_transaction_context_listener,
    parse_tenant_organisation_id,
)
from src.infra.secrets.master_key_vault_sync import (
    get_master_key_vault_secret_sync as _get_master_key_vault_secret_sync,
)
from src.infra.utils.logger import get_logger

logger = get_logger("sync_db_provider")

# Pool settings for connections to pgbouncer (not to PostgreSQL directly).
_POOL_SIZE = int(database_config.DATABASE_POOL_SIZE)
_MAX_OVERFLOW = int(database_config.DATABASE_MAX_OVERFLOW)
_POOL_TIMEOUT = int(database_config.DATABASE_POOL_TIMEOUT_SEC)
_POOL_RECYCLE = int(database_config.POOL_RECYCLE_SEC)
install_tenant_transaction_context_listener()


class SyncSqlAlchemyProvider(SyncDatabaseProvider):
    """Provide master sessions and one shared, RLS-bound tenant connection pool."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

        self._master_engine: Engine | None = None
        self._tenant_engine: Engine | None = None

    def _get_master_engine(self) -> Engine:
        if self._master_engine is not None:
            return self._master_engine
        with self._lock:
            if self._master_engine is None:
                url = self._build_master_url()
                self._master_engine = create_engine(
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
    def _get_tenant_engine(self, _org_id: UUID) -> Engine:
        """Get or create the one RLS-bound organisation engine."""
        with self._lock:
            if self._tenant_engine is None:
                user, password = self._require_shared_tenant_credentials()
                logger.debug("Creating shared tenant engine")
                self._tenant_engine = self._build_tenant_engine(user, password)
            return self._tenant_engine

    def _require_shared_tenant_credentials(self) -> tuple[str, str]:
        config = validate_tenant_runtime_config(
            database_user=database_config.DATABASE_USER,
            tenant_database_user=database_config.TENANT_DATABASE_USER,
            tenant_password=database_config.TENANT_DATABASE_PASSWORD,
            tenant_password_secret_name=database_config.TENANT_DATABASE_PASSWORD_SECRET_NAME,
        )
        password = load_tenant_runtime_password_sync(
            config,
            get_secret=_get_master_key_vault_secret_sync,
        )
        return config.user, password

    def _build_tenant_engine(self, role_name: str, password: str) -> Engine:
        host = database_config.DATABASE_HOST
        port = database_config.DATABASE_PORT
        db_name = database_config.DATABASE_NAME
        if isinstance(password, bytes):
            password = password.decode("utf-8")
        role_name = urllib.parse.quote_plus(role_name)
        pw = urllib.parse.quote_plus(password)
        url = f"postgresql://{role_name}:{pw}@{host}:{port}/{db_name}"

        return create_engine(
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
                "sslmode": database_config.DATABASE_SSL_MODE,
                "connect_timeout": database_config.DATABASE_CONNECT_TIMEOUT_SEC,
            },
        )

    # Public Provider API ---------------------------------------------
    @contextmanager
    def master_session(self) -> Iterator[SASession]:
        """Yield a control-plane session without tenant schema or RLS context."""

        master_engine = self._get_master_engine()
        session_local = sessionmaker(bind=master_engine, expire_on_commit=False, class_=SASession)
        session: SASession = session_local()
        try:
            session.info[DATABASE_RUNTIME_SESSION_KEY] = True
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @contextmanager
    def tenant_session(self, org_id: UUID | str) -> Iterator[SASession]:
        """Yield an org-scoped session with canonical transaction identity."""

        org_id = parse_tenant_organisation_id(org_id)
        started = time.perf_counter()
        engine = self._get_tenant_engine(org_id)
        session_local = sessionmaker(bind=engine, expire_on_commit=False, class_=SASession)
        session: SASession = session_local()
        try:
            session.info[TENANT_ORGANISATION_ID_SESSION_KEY] = org_id
            session.info[TENANT_SCHEMA_SESSION_KEY] = TENANT_DATA_SCHEMA
            session.info[TENANT_SEARCH_PATH_SESSION_KEY] = (
                TENANT_DATA_SCHEMA,
                TRUSTED_EXTENSION_SCHEMA,
            )
            yield session
            session.commit()
        except Exception as exc:
            with suppress(Exception):
                session.rollback()
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
            session.close()

    def invalidate_tenant(self, _org_id: UUID) -> None:
        """Discard the shared tenant pool after credentials or connectivity change."""

        with self._lock:
            engine = self._tenant_engine
            self._tenant_engine = None
        if engine:
            with suppress(Exception):
                engine.dispose()

    def dispose(self) -> None:
        """Close and forget every pool owned by this provider."""

        with self._lock:
            engines = (self._master_engine, self._tenant_engine)
            self._master_engine = None
            self._tenant_engine = None
        for engine in engines:
            if engine is not None:
                with suppress(Exception):
                    engine.dispose()

    def reset_after_fork(self) -> None:
        """Replace inherited pools without touching parent-process connections."""

        with self._lock:
            engines = (self._master_engine, self._tenant_engine)
            self._master_engine = None
            self._tenant_engine = None
        for engine in engines:
            if engine is not None:
                with suppress(Exception):
                    engine.dispose(close=False)


__all__ = ["SyncSqlAlchemyProvider"]
