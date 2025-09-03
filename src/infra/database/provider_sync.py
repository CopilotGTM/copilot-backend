"""Independent synchronous database provider (no legacy dependency).

Creates and caches:
  - A single master engine (using DATABASE_USER / DATABASE_PASSWORD)
  - Tenant engines via TenantPoolManager with per-tenant login/password

Responsibilities kept minimal: session lifecycle + engine invalidation.
Schema selection is assumed to be handled either via role default search_path
or future connect event hooks.
"""
from __future__ import annotations

import threading
import urllib.parse
import asyncio
from contextlib import contextmanager
from typing import Iterator, Dict

from sqlalchemy.engine import Engine
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from sqlmodel import create_engine, Session as SQLModelSession

from src.infra.database.interface import SyncDatabaseProvider
from src.infra.database.pool_manager import TenantPoolManager
from src.infra.config.configs import database_config
from src.infra.secrets.manager import secrets_manager
from src.infra.utils.resource_naming import (
    get_key_vault_secret_name,
    get_role_name,
    get_schema_name,
)
import uuid

class SyncSqlAlchemyProvider(SyncDatabaseProvider):
    def __init__(self, *, tenant_pool_max: int = 100, tenant_pool_multiplier: float = 5.0):
        self._lock = threading.RLock()
        self._master_engine: Engine | None = None
        self._SessionFactory: sessionmaker | None = None
        self._tenant_pools = TenantPoolManager(
            max_tenant_engines=tenant_pool_max,
            pool_multiplier=tenant_pool_multiplier,
        )

    # ------------------------------------------------------------------
    # Master
    # ------------------------------------------------------------------
    def _ensure_master(self):
        if self._master_engine is not None:
            return
        with self._lock:
            if self._master_engine is None:
                url = self._build_master_url()
                self._master_engine = create_engine(
                    url,
                    echo=False,
                    pool_pre_ping=True,
                    pool_recycle=database_config.POOL_RECYCLE_SEC,
                    pool_size=database_config.DATABASE_POOL_SIZE,
                    max_overflow=database_config.DATABASE_MAX_OVERFLOW,
                    connect_args={"sslmode": database_config.DATABASE_SSL_MODE},
                )
                # Use SQLModel Session for backward compatibility (.exec API)
                self._SessionFactory = sessionmaker(bind=self._master_engine, expire_on_commit=False, class_=SQLModelSession)

    def _build_master_url(self) -> str:
        host = database_config.DATABASE_HOST
        port = database_config.DATABASE_PORT
        db = database_config.DATABASE_NAME
        user = urllib.parse.quote_plus(database_config.DATABASE_USER)
        pw = urllib.parse.quote_plus(database_config.DATABASE_PASSWORD)
        return f"postgresql://{user}:{pw}@{host}:{port}/{db}"

    # ------------------------------------------------------------------
    # Tenant
    # ------------------------------------------------------------------
    def _get_tenant_engine(self, org_id: str) -> Engine:
        # Expect org_id as string UUID; convert for naming helpers
        try:
            org_uuid = uuid.UUID(org_id)
        except Exception:
            raise ValueError(f"Invalid organisation id: {org_id}")
        role_name = get_role_name(org_uuid)
        secret_name = get_key_vault_secret_name(org_uuid)
        # Fetch tenant password (async provider behind sync facade)
        # secrets_manager.get is async; invoke it safely in sync context
        password = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            # Running inside async loop — execute the async call in a new thread
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=1) as executor:
                def fetch_secret():
                    try:
                        return asyncio.run(secrets_manager.get(secret_name))  # type: ignore
                    except RuntimeError as e:
                        if "Event loop is closed" in str(e):
                            new_loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(new_loop)
                            try:
                                return new_loop.run_until_complete(secrets_manager.get(secret_name))  # type: ignore
                            finally:
                                new_loop.close()
                        raise
                future = executor.submit(fetch_secret)
                password = future.result()
        else:
            try:
                password = asyncio.run(secrets_manager.get(secret_name))  # type: ignore
            except RuntimeError as e:
                if "Event loop is closed" in str(e):
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        password = loop.run_until_complete(secrets_manager.get(secret_name))  # type: ignore
                    finally:
                        loop.close()
                else:
                    raise
        if password is None:
            raise RuntimeError(f"Secret '{secret_name}' not found for tenant {org_uuid}")
        return self._tenant_pools.get_or_create(role_name, password)

    # ------------------------------------------------------------------
    # Public Provider API
    # ------------------------------------------------------------------
    @contextmanager
    def master_session(self) -> Iterator[SQLModelSession]:
        self._ensure_master()
        assert self._SessionFactory is not None
        session: SQLModelSession = self._SessionFactory()  # type: ignore
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @contextmanager
    def tenant_session(self, org_id: str) -> Iterator[SQLModelSession]:
        engine = self._get_tenant_engine(org_id)
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=SQLModelSession)
        session: SQLModelSession = SessionLocal()  # type: ignore
        # Apply tenant schema search_path explicitly (role defaults not yet guaranteed in all envs).
        # Once provisioning ensures: ALTER ROLE <tenant_role> IN DATABASE <db> SET search_path = "<schema>", public;
        # this block can be removed for a tiny performance win.
        try:
            org_uuid = uuid.UUID(org_id)
            schema = get_schema_name(org_uuid)
        except Exception:
            schema = None
        if schema:
            try:
                session.execute(text(f'SET search_path TO "{schema}", public'))
            except Exception:
                pass
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            if schema:
                try:
                    session.execute(text('SET search_path TO public'))
                except Exception:
                    pass
            session.close()

    def invalidate_tenant(self, org_id: str) -> None:
        try:
            org_uuid = uuid.UUID(org_id)
        except Exception:
            return
        role_name = get_role_name(org_uuid)
        self._tenant_pools.invalidate(role_name)

__all__ = ["SyncSqlAlchemyProvider"]
