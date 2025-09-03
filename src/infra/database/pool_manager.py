"""Unified engine / pool management for multi-tenant Postgres.

This module extracts logic currently embedded in `infra.utils.database` into a
more testable + observable structure while keeping the legacy module intact for
backwards compatibility.

Goals:
- Cap total open connections (avoid N * pool_size explosion).
- Provide pluggable eviction policy (LRU, idle-time, none).
- Expose metrics-friendly counters (hooks only; metric emission done elsewhere).
- Support both sync `Engine` and future async `AsyncEngine` via strategy objects.

NOTE: Initial implementation wraps existing sync behaviour. Async version can
be layered later by mirroring the class with async engine factory functions.
"""
from __future__ import annotations

import threading
import time
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from sqlalchemy.engine import Engine
from sqlmodel import create_engine

from src.infra.config.configs import database_config

# Type alias for factory
EngineFactory = Callable[[str, str], Engine]

@dataclass
class PoolStats:
    master_engine: bool
    tenant_engines: int
    evictions: int
    open_estimated_connections: int

class LRUEvictionPolicy:
    """Simple size-bound eviction; tracks evictions for observability."""
    def __init__(self, max_items: int):
        self.max_items = max_items
        self.eviction_count = 0

    def touch_and_evict(self, mapping: "OrderedDict[str, Engine]"):
        while len(mapping) > self.max_items:
            mapping.popitem(last=False)
            self.eviction_count += 1

class TenantPoolManager:
    def __init__(
        self,
        *,
        max_tenant_engines: int,
        pool_multiplier: float,
    ):
        self._tenant_engines: "OrderedDict[str, Engine]" = OrderedDict()
        self._lock = threading.RLock()
        self._eviction = LRUEvictionPolicy(max_tenant_engines)
        self._pool_multiplier = pool_multiplier

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_or_create(self, role_name: str, password: str) -> Engine:
        with self._lock:
            existing = self._tenant_engines.get(role_name)
            if existing:
                # LRU touch
                self._tenant_engines.pop(role_name, None)
                self._tenant_engines[role_name] = existing
                return existing
            engine = self._build_engine(role_name, password)
            self._tenant_engines[role_name] = engine
            self._eviction.touch_and_evict(self._tenant_engines)
            return engine

    def invalidate(self, role_name: str):
        with self._lock:
            engine = self._tenant_engines.pop(role_name, None)
            if engine:
                try: engine.dispose()
                except Exception: pass

    def stats(self) -> PoolStats:
        with self._lock:
            base = database_config.DATABASE_POOL_SIZE or 5
            overflow = database_config.DATABASE_MAX_OVERFLOW or 0
            per_tenant = int(base * self._pool_multiplier) + int(overflow * self._pool_multiplier)
            open_connections = per_tenant * len(self._tenant_engines)
            return PoolStats(
                master_engine=False,
                tenant_engines=len(self._tenant_engines),
                evictions=self._eviction.eviction_count,
                open_estimated_connections=open_connections,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _build_engine(self, user: str, password: str) -> Engine:
        host = database_config.DATABASE_HOST
        port = database_config.DATABASE_PORT
        db_name = database_config.DATABASE_NAME
        if isinstance(password, bytes):
            password = password.decode('utf-8')
        pw = urllib.parse.quote_plus(password)
        url = f"postgresql://{user}:{pw}@{host}:{port}/{db_name}"
        base_size = database_config.DATABASE_POOL_SIZE or 5
        overflow = database_config.DATABASE_MAX_OVERFLOW or 0
        pool_size = max(1, int(base_size * self._pool_multiplier))
        max_overflow = max(0, int(overflow * self._pool_multiplier))
        print(f"[DB POOL] Creating engine for user={user} pool_size={pool_size} max_overflow={max_overflow} url={url}")
        return create_engine(
            url,
            echo=False,
            pool_pre_ping=True,
            pool_recycle=database_config.POOL_RECYCLE_SEC,
            pool_size=pool_size,
            max_overflow=max_overflow,
            connect_args={"sslmode": database_config.DATABASE_SSL_MODE},
        )

__all__ = [
    "TenantPoolManager",
    "PoolStats",
]
