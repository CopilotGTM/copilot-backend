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
from src.infra.utils.logger import get_logger
import uuid

logger = get_logger("sync_db_provider")

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
        
        logger.debug(f"Attempting to get tenant engine for org_id={org_id}, secret_name={secret_name}")
        
        # Fetch tenant password with enhanced error handling
        password = None
        try:
            # Check if we're in an async context
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        
        # Define the secret fetching function with improved async handling
        def fetch_secret_with_fallback():
            """Fetch secret handling async/sync boundary properly."""
            try:
                # Always create a new event loop for secret fetching
                # This avoids issues with closed event loops
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
                try:
                    # First try normal get
                    password = loop.run_until_complete(secrets_manager.get(secret_name))
                    if password is not None:
                        return password
                    
                    # If that fails, try get_with_fallback for critical database operations
                    logger.warning(f"Normal secret fetch failed for '{secret_name}', trying fallback")
                    password = loop.run_until_complete(
                        secrets_manager.get_with_fallback(secret_name, fallback_ttl=7200)  # 2 hours fallback
                    )
                    
                    if password is not None:
                        logger.info(f"Successfully retrieved secret '{secret_name}' using fallback")
                        return password
                    
                    return None
                    
                except Exception as e:
                    logger.error(f"Error in event loop for secret '{secret_name}': {e}")
                    return None
                finally:
                    try:
                        # Clean up the event loop
                        loop.close()
                    except Exception:
                        pass
                    
            except Exception as e:
                logger.error(f"Failed to create event loop for secret '{secret_name}': {e}")
                return None
        
        if loop and loop.is_running():
            # Running inside async loop — use thread pool but with better isolation
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=1) as executor:
                try:
                    future = executor.submit(fetch_secret_with_fallback)
                    password = future.result(timeout=30)  # 30 second timeout
                except Exception as e:
                    logger.error(f"Thread pool execution failed for secret '{secret_name}': {e}")
                    password = None
        else:
            # Not in async context, safe to run directly
            password = fetch_secret_with_fallback()
        
        if password is None:
            # Enhanced error message with troubleshooting info
            metrics = secrets_manager.get_metrics()
            health = secrets_manager.health_check()
            
            error_msg = (
                f"Secret '{secret_name}' not found for tenant {org_uuid}. "
                f"Secret manager health: {health['healthy']}, "
                f"circuit breaker open: {health['circuit_breaker_open']}, "
                f"consecutive failures: {health['consecutive_failures']}, "
                f"cache hit rate: {metrics['cache_hit_rate']:.2%}"
            )
            logger.error(error_msg)
            
            # Check if this is a persistent issue and suggest actions
            if health['consecutive_failures'] >= 3:
                logger.error(
                    f"Multiple consecutive failures detected for secrets. "
                    f"This may indicate an Azure Key Vault connectivity issue. "
                    f"Consider checking network connectivity and authentication."
                )
            
            raise RuntimeError(error_msg)
        
        logger.info(f"Successfully retrieved tenant password for org_id={org_id}")
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
