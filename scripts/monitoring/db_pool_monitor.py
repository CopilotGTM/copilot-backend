from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import threading
import time
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import asdict, dataclass
from typing import Any
from weakref import WeakValueDictionary

from sqlalchemy import event, text
from src.infra.config.configs import pool_monitor_config
from src.infra.database.factory import get_async_provider, get_sync_provider
from src.infra.utils.logger import get_logger

logger = get_logger("db_pool_monitor")
# Allow env-based override of log level (e.g., DB_POOL_MONITOR_LOG_LEVEL=DEBUG)
_lvl = os.getenv("DB_POOL_MONITOR_LOG_LEVEL")
if _lvl:
    with suppress(Exception):
        logger.setLevel(getattr(logging, _lvl.upper(), logging.INFO))


_MONITOR_REGISTRY: WeakValueDictionary[int, SinglePoolMonitor] = WeakValueDictionary()
_MONITOR_REGISTRY_LOCK = threading.RLock()


def _register_monitor(monitor: SinglePoolMonitor) -> None:
    with _MONITOR_REGISTRY_LOCK:
        _MONITOR_REGISTRY[id(monitor.pool)] = monitor


def _unregister_monitor(pool: Any) -> None:
    with _MONITOR_REGISTRY_LOCK:
        _MONITOR_REGISTRY.pop(id(pool), None)


def _lookup_monitor(connection_record: Any) -> SinglePoolMonitor | None:
    pool = getattr(connection_record, "pool", None) or getattr(connection_record, "_pool", None)
    if pool is None:
        return None
    with _MONITOR_REGISTRY_LOCK:
        monitor = _MONITOR_REGISTRY.get(id(pool))
    return monitor


@dataclass
class PoolMetrics:
    label: str
    pool_id: int
    kind: str  # "sync" | "async"
    size: int | None
    checked_in: int | None
    checked_out: int | None
    current_overflow: int | None
    max_overflow: int | None
    long_running_checked_out: int
    longest_checkout_seconds: float | None
    status: str | None
    health: str  # derived: OK | WARN | CRITICAL | UNKNOWN


class SinglePoolMonitor:
    """
    Attaches checkout/checkin listeners to a SQLAlchemy Pool and tracks how long
    individual connections are checked out.
    """

    def __init__(self, pool: Any, label: str, kind: str, engine: Any | None = None):
        self.pool = pool
        self.label = label
        self.kind = kind
        self.engine = engine  # underlying sync engine for optional probes
        self._lock = threading.Lock()
        # Maps id(connection_record) -> (checkout_time_monotonic, connection_record, dbapi_connection)
        self._checked_out: dict[int, tuple[float, Any, Any]] = {}
        # Track whether this monitor is registered for global event dispatch
        self._listeners_attached = False
        # Counters
        self._checkout_count = 0
        self._checkin_count = 0
        self._connect_count = 0
        self._invalidate_count = 0
        self._close_count = 0
        self._enforced_count = 0
        # Probe state
        self._last_probe_ts: float | None = None
        self._last_probe_ok: bool | None = None

    def attach(self) -> None:
        if self._listeners_attached:
            return
        # Attach event listeners only to this pool instance.
        # We catch RuntimeError ("deque mutated during iteration") which can
        # occur if attachment races with connection creation on another thread.
        # Caller should retry on next monitor tick.
        try:
            event.listen(self.pool, "checkout", _dispatch_checkout)
            event.listen(self.pool, "checkin", _dispatch_checkin)
            event.listen(self.pool, "connect", _dispatch_connect)
            event.listen(self.pool, "invalidate", _dispatch_invalidate)
            event.listen(self.pool, "close", _dispatch_close)
        except RuntimeError as e:
            if "deque mutated" in str(e):
                # Race condition - pool is actively being used; skip this tick
                raise
            raise
        _register_monitor(self)
        self._listeners_attached = True

    def detach(self) -> None:
        if not self._listeners_attached:
            return
        # Remove event listeners from this pool instance
        event.remove(self.pool, "checkout", _dispatch_checkout)
        event.remove(self.pool, "checkin", _dispatch_checkin)
        event.remove(self.pool, "connect", _dispatch_connect)
        event.remove(self.pool, "invalidate", _dispatch_invalidate)
        event.remove(self.pool, "close", _dispatch_close)
        _unregister_monitor(self.pool)
        self._listeners_attached = False

    def on_checkout(self, dbapi_con: Any, con_record: Any, *_args: Any) -> None:
        try:
            key = id(con_record)
        except Exception:
            return
        now = time.monotonic()
        with self._lock:
            self._checked_out[key] = (now, con_record, dbapi_con)
            self._checkout_count += 1

    def on_checkin(self, _dbapi_con: Any, con_record: Any) -> None:
        try:
            key = id(con_record)
        except Exception:
            return
        with self._lock:
            self._checked_out.pop(key, None)
            self._checkin_count += 1

    def on_connect(self, *_: Any) -> None:
        with self._lock:
            self._connect_count += 1

    def on_invalidate(self, *_: Any) -> None:
        with self._lock:
            self._invalidate_count += 1

    def on_close(self, *_: Any) -> None:
        with self._lock:
            self._close_count += 1

    def ensure_min_idle(self, min_idle: int = 1, max_new: int = 1) -> None:
        """Prime the pool with a minimal number of idle connections.

        Conservative: opens at most `max_new` connections per call, and only
        if fewer than `min_idle` are currently idle.
        """
        if self.engine is None or min_idle <= 0 or max_new <= 0:
            return
        try:
            checked_in = self._call_int("checkedin")
            current_idle = int(checked_in or 0)
        except Exception:
            return
        deficit = max(0, min_idle - current_idle)
        to_open = min(deficit, max_new)
        for _ in range(to_open):
            try:
                # Open and immediately ping the connection to prime the pool
                with self.engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
            except Exception:
                break

    def metrics(self, threshold_sec: float = 300.0) -> PoolMetrics:
        # pool API varies slightly; use getattr with defaults
        size = self._call_int("size")
        checked_in = self._call_int("checkedin")
        checked_out = self._call_int("checkedout")
        current_overflow = self._call_int("overflow")
        max_overflow = getattr(self.pool, "_max_overflow", None)
        status = self._call_str("status")
        now = time.monotonic()
        with self._lock:
            long_running = [
                t for (t, _, _) in self._checked_out.values() if now - t > threshold_sec
            ]
            longest = max(((now - t) for (t, _, _) in self._checked_out.values()), default=None)

        health = self._derive_health(size, checked_out, max_overflow)

        pm = PoolMetrics(
            label=self.label,
            pool_id=id(self.pool),
            kind=self.kind,
            size=size,
            checked_in=checked_in,
            checked_out=checked_out,
            current_overflow=current_overflow,
            max_overflow=max_overflow,
            long_running_checked_out=len(long_running),
            longest_checkout_seconds=longest,
            status=status,
            health=health,
        )
        # Keep it minimal here; enrichment happens in collect_snapshot()
        return pm

    def enforce_checkout_limit(self, max_sec: float, *, force_close: bool = False) -> int:
        """Invalidate connections checked out longer than max_sec.

        Returns number of connections marked for close. If force_close is True,
        it attempts to close the DBAPI connection immediately (risky). Safer
        default is to invalidate so it closes on check-in.
        """
        count = 0
        if max_sec <= 0:
            return count
        now = time.monotonic()
        with self._lock:
            to_handle = [
                (key, t, rec, dbc)
                for key, (t, rec, dbc) in self._checked_out.items()
                if now - t > max_sec
            ]
        for _key, _t, rec, dbc in to_handle:
            try:
                # Mark to be closed on check-in
                rec.invalidate()
            except Exception as exc:
                logger.debug("Failed to invalidate checked-out connection: %s", exc)
                continue

            count += 1
            with self._lock:
                self._enforced_count += 1
            if force_close and dbc is not None:
                with suppress(Exception):
                    dbc.close()
        return count

    def maybe_probe(self, min_interval_sec: float = 120.0) -> None:
        if self.engine is None:
            return
        now = time.monotonic()
        last = self._last_probe_ts or 0.0
        if now - last < min_interval_sec:
            return
        # Only probe when we have idle connections to avoid creating new ones
        checked_in = self._call_int("checkedin")
        if isinstance(checked_in, int) and checked_in <= 0:
            self._last_probe_ts = now
            self._last_probe_ok = None
            return
        ok = False
        try:
            # Use underlying sync engine to benefit from pool_pre_ping
            with self.engine.connect() as _:
                ok = True
        except Exception:
            ok = False
        self._last_probe_ts = now
        self._last_probe_ok = ok

    def _call_int(self, name: str) -> int | None:
        """Best-effort: read an integer metric from the pool.

        Pool implementations differ: attributes can be callables or properties.
        We only accept real ints (excluding bool) and return None otherwise.
        """
        try:
            attr = getattr(self.pool, name, None)
            value = attr() if callable(attr) else attr
            if isinstance(value, bool):
                return None
            if isinstance(value, int):
                return value
            return None
        except Exception:
            return None

    def _call_str(self, name: str) -> str | None:
        """Best-effort: read a string metric from the pool."""
        try:
            attr = getattr(self.pool, name, None)
            value = attr() if callable(attr) else attr
            return value if isinstance(value, str) else None
        except Exception:
            return None

    @staticmethod
    def _derive_health(size: int | None, checked_out: int | None, max_overflow: int | None) -> str:
        try:
            sz = int(size or 0)
            co = int(checked_out or 0)
            mo = max_overflow if (isinstance(max_overflow, int) and max_overflow >= 0) else 0
            capacity = sz + mo if (sz + mo) > 0 else max(sz, 1)
            usage = co / capacity
            if co >= capacity:
                return "CRITICAL"
            if usage >= 0.8:
                return "WARN"
            return "OK"
        except Exception:
            return "UNKNOWN"


def _dispatch_checkout(dbapi_con: Any, connection_record: Any, *args: Any) -> None:
    monitor = _lookup_monitor(connection_record)
    if monitor is not None:
        monitor.on_checkout(dbapi_con, connection_record, *args)


def _dispatch_checkin(dbapi_con: Any, connection_record: Any) -> None:
    monitor = _lookup_monitor(connection_record)
    if monitor is not None:
        monitor.on_checkin(dbapi_con, connection_record)


def _dispatch_connect(dbapi_con: Any, connection_record: Any) -> None:
    monitor = _lookup_monitor(connection_record)
    if monitor is not None:
        monitor.on_connect(dbapi_con, connection_record)


def _dispatch_invalidate(dbapi_con: Any, connection_record: Any, exception: Any) -> None:
    monitor = _lookup_monitor(connection_record)
    if monitor is not None:
        monitor.on_invalidate(dbapi_con, connection_record, exception)


def _dispatch_close(dbapi_con: Any, connection_record: Any) -> None:
    monitor = _lookup_monitor(connection_record)
    if monitor is not None:
        monitor.on_close(dbapi_con, connection_record)


# Remove global event listeners on Pool class to avoid performance impact.
# Instead, listeners are attached to specific pool instances in SinglePoolMonitor.attach().


class DbPoolsMonitorService:
    """
    Periodically scans the master and tenant pools (sync and async providers),
    attaches listeners to track checkout durations, and logs aggregated metrics.
    """

    def __init__(
        self,
        interval_sec: float = 30.0,
        long_running_threshold_sec: float = 300.0,
        enforce_max_checkout_sec: float | None = None,
        force_close: bool = False,
    ):
        self.interval_sec = interval_sec
        self.long_running_threshold_sec = long_running_threshold_sec
        self._enforce_max_checkout_sec = enforce_max_checkout_sec
        self._enforce_force_close = force_close
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        # Pool id -> monitor
        self._monitors: dict[int, SinglePoolMonitor] = {}

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        logger.info(
            "Starting DB pool monitor (interval=%ss, long_running>%ss)",
            self.interval_sec,
            self.long_running_threshold_sec,
        )
        self._stopping.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        logger.info("Stopping DB pool monitor...")
        self._stopping.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=self.interval_sec + 2.0)
            except TimeoutError:
                self._task.cancel()
        for mon in self._monitors.values():
            with suppress(Exception):
                mon.detach()
        self._monitors.clear()

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                # IMPORTANT: do not run blocking DB/pool operations on the main
                # asyncio event loop thread, otherwise the webserver can stop
                # responding (including /health) and get restarted by probes.
                await asyncio.to_thread(self._tick_once)
            except Exception as e:
                logger.error("DB pool monitor tick failed: %s", e)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self.interval_sec)

    def _tick_once(self) -> None:
        self._discover_and_attach()
        snapshot = self.collect_snapshot()
        self._log_snapshot(snapshot)

    def _discover_and_attach(self) -> None:
        self._warmup_master_engines()
        warm_min_idle = self._get_warm_min_idle()
        for pool, engine, label, kind in self._iter_all_pools():
            self._handle_pool(pool, engine, label, kind, warm_min_idle)

    def _warmup_master_engines(self) -> None:
        for provider_getter in (get_sync_provider, get_async_provider):
            try:
                provider = provider_getter()
            except Exception as exc:
                logger.debug("Provider getter failed during warmup: %s", exc)
                continue
            getme = getattr(provider, "_get_master_engine", None)
            if callable(getme):
                with suppress(Exception):
                    getme()

    def _get_warm_min_idle(self) -> int:
        try:
            return int(os.getenv("DB_POOL_MONITOR_WARMUP_MIN_IDLE", "0"))
        except Exception:
            return 0

    def _iter_all_pools(self) -> Iterable[tuple[Any, Any, str, str]]:
        return itertools.chain(self._iter_sync_pools(), self._iter_async_pools())

    def _handle_pool(
        self,
        pool: Any,
        engine: Any,
        label: str,
        kind: str,
        warm_min_idle: int,
    ) -> None:
        monitor = self._monitors.get(id(pool))
        if monitor is None:
            monitor = self._create_monitor(pool, engine, label, kind)
            if monitor is None:
                return
        self._maybe_prime_pool(monitor, warm_min_idle)
        self._maybe_probe_pool(monitor)
        self._maybe_enforce_checkout(monitor, label)

    def _create_monitor(
        self, pool: Any, engine: Any, label: str, kind: str
    ) -> SinglePoolMonitor | None:
        monitor = SinglePoolMonitor(pool=pool, label=label, kind=kind, engine=engine)
        try:
            monitor.attach()
        except RuntimeError as exc:
            # Transient race condition during pool creation; will retry next tick
            if "deque mutated" in str(exc):
                logger.debug(
                    "Deferred monitor attach for pool %s (pool busy): %s", label, exc
                )
                return None
            logger.warning("Failed to attach monitor to pool %s: %s", label, exc)
            return None
        except Exception as exc:
            logger.warning("Failed to attach monitor to pool %s: %s", label, exc)
            return None
        self._monitors[id(pool)] = monitor
        logger.info("Attached monitor to pool: %s (%s)", label, kind)
        return monitor

    def _maybe_prime_pool(self, monitor: SinglePoolMonitor, warm_min_idle: int) -> None:
        if warm_min_idle <= 0:
            return
        with suppress(Exception):
            monitor.ensure_min_idle(min_idle=warm_min_idle, max_new=1)

    def _maybe_probe_pool(self, monitor: SinglePoolMonitor) -> None:
        with suppress(Exception):
            monitor.maybe_probe(min_interval_sec=max(self.interval_sec * 4.0, 120.0))

    def _maybe_enforce_checkout(self, monitor: SinglePoolMonitor, label: str) -> None:
        if not self._enforce_max_checkout_sec or self._enforce_max_checkout_sec <= 0:
            return
        try:
            n = monitor.enforce_checkout_limit(
                self._enforce_max_checkout_sec,
                force_close=self._enforce_force_close,
            )
        except Exception:
            return
        if n:
            logger.warning("Enforced max checkout on %s connections for pool %s", n, label)

    def collect_snapshot(self) -> dict[str, Any]:
        metrics = [self._build_metric_dict(mon) for mon in self._monitors.values()]
        summary = self._summarize_metrics(metrics)
        return {"summary": summary, "pools": metrics}

    def _build_metric_dict(self, monitor: SinglePoolMonitor) -> dict[str, Any]:
        metric = asdict(monitor.metrics(self.long_running_threshold_sec))
        configured_pool_size = self._safe_getattr(monitor.pool, "_pool", "maxsize")
        checked_in = metric.get("checked_in")
        checked_out = metric.get("checked_out")
        max_overflow = metric.get("max_overflow")
        capacity_total = (configured_pool_size or 0) + (
            max_overflow or 0 if isinstance(max_overflow, int) else 0
        )
        total_open = (
            None
            if (checked_in is None or checked_out is None)
            else int(checked_in) + int(checked_out)
        )
        counters = {
            "checkout": monitor._checkout_count,
            "checkin": monitor._checkin_count,
            "connect": monitor._connect_count,
            "invalidate": monitor._invalidate_count,
            "close": monitor._close_count,
            "enforced": monitor._enforced_count,
        }
        metric.update(
            {
                "configured_pool_size": configured_pool_size,
                "capacity_total": capacity_total,
                "idle": checked_in,
                "in_use": checked_out,
                "total_open": total_open,
                "usage_pct": (
                    None if not capacity_total else float(checked_out or 0) / float(capacity_total)
                ),
                "counters": counters,
                "alive_sample_ok": monitor._last_probe_ok,
                "last_probe_ts": monitor._last_probe_ts,
            }
        )
        return metric

    @staticmethod
    def _safe_getattr(obj: Any, intermediate_attr: str, final_attr: str) -> int | None:
        try:
            intermediate = getattr(obj, intermediate_attr, None)
            return getattr(intermediate, final_attr, None)
        except Exception:
            return None

    def _summarize_metrics(self, metrics: list[dict[str, Any]]) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "total_pools": len(metrics),
            "sync_pools": sum(1 for m in metrics if m.get("kind") == "sync"),
            "async_pools": sum(1 for m in metrics if m.get("kind") == "async"),
            "total_checked_out": sum(int(m.get("checked_out") or 0) for m in metrics),
            "total_size": sum(int(m.get("size") or 0) for m in metrics),
            "total_idle": sum(int(m.get("checked_in") or 0) for m in metrics),
            "total_current_overflow": sum(int(m.get("current_overflow") or 0) for m in metrics),
            "total_long_running": sum(int(m.get("long_running_checked_out") or 0) for m in metrics),
            "total_enforced": sum(
                int((m.get("counters") or {}).get("enforced") or 0) for m in metrics
            ),
        }
        summary["health"] = {
            key: sum(1 for m in metrics if m.get("health") == key)
            for key in ("OK", "WARN", "CRITICAL", "UNKNOWN")
        }
        return summary

    def _log_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._log_summary(snapshot)
        self._log_pool_details(snapshot)
        logger.debug("DB Pool snapshot: %s", json.dumps(snapshot))

    def _log_summary(self, snapshot: dict[str, Any]) -> None:
        with suppress(Exception):
            summary = snapshot["summary"]
            health = summary["health"]
            logger.info(
                "DB Pools: total=%s sync=%s async=%s checked_out=%s size=%s idle=%s long_running>%ss=%s enforced=%s health(OK=%s WARN=%s CRIT=%s)",
                summary["total_pools"],
                summary["sync_pools"],
                summary["async_pools"],
                summary["total_checked_out"],
                summary.get("total_size"),
                summary.get("total_idle"),
                int(self.long_running_threshold_sec),
                summary["total_long_running"],
                summary.get("total_enforced"),
                health.get("OK"),
                health.get("WARN"),
                health.get("CRITICAL"),
            )

    def _log_pool_details(self, snapshot: dict[str, Any]) -> None:
        with suppress(Exception):
            for pool_metric in snapshot.get("pools", []):
                self._log_pool_metric(pool_metric)

    def _log_pool_metric(self, metric: dict[str, Any]) -> None:
        in_use = int(metric.get("in_use") or metric.get("checked_out") or 0)
        idle = int(metric.get("idle") or metric.get("checked_in") or 0)
        usage = metric.get("usage_pct")
        long_running = int(metric.get("long_running_checked_out") or 0)
        alive = metric.get("alive_sample_ok")
        health = metric.get("health")
        enforced = int((metric.get("counters") or {}).get("enforced") or 0)
        if not (
            in_use > 0
            or long_running > 0
            or enforced > 0
            or alive is False
            or health in ("WARN", "CRITICAL")
        ):
            return
        logger.info(
            "Pool %s: in_use=%s idle=%s size=%s cap=%s overflow=%s usage=%.2f health=%s alive=%s longest=%s enforced=%s",
            metric.get("label"),
            in_use,
            idle,
            metric.get("size"),
            metric.get("capacity_total"),
            metric.get("current_overflow"),
            0.0 if not usage else float(usage),
            health,
            alive,
            metric.get("longest_checkout_seconds"),
            enforced,
        )

    def _iter_sync_pools(self) -> Iterable[tuple[Any, Any, str, str]]:
        """Yield (pool, engine, label, kind) for sync master + tenant engines."""
        try:
            provider = get_sync_provider()
        except Exception:
            return []

        pools: list[tuple[Any, Any, str, str]] = []

        # Master engine (if created)
        with suppress(Exception):
            eng = getattr(provider, "_master_engine", None)
            if eng is not None:
                pool = getattr(eng, "pool", None)
                if pool is not None:
                    pools.append((pool, eng, "master(sync)", "sync"))

        # Tenant engines (if any)
        with suppress(Exception):
            tdict: dict[Any, Any] = getattr(provider, "_tenant_engines", {})
            for org_id, eng in tdict.items():
                pool = getattr(eng, "pool", None)
                if pool is not None:
                    pools.append((pool, eng, f"tenant(sync):{org_id}", "sync"))

        return pools

    def _iter_async_pools(self) -> Iterable[tuple[Any, Any, str, str]]:
        """Yield (pool, sync_engine, label, kind) for async master + tenant engines."""
        try:
            provider = get_async_provider()
        except Exception:
            return []

        pools: list[tuple[Any, Any, str, str]] = []

        # Master async engine (if created)
        with suppress(Exception):
            aeng = getattr(provider, "_master_engine", None)
            if aeng is not None:
                seng = getattr(aeng, "sync_engine", None)
                pool = getattr(seng, "pool", None)
                if pool is not None:
                    pools.append((pool, seng, "master(async)", "async"))

        # Tenant async engines (if any)
        with suppress(Exception):
            tdict: dict[Any, Any] = getattr(provider, "_tenant_engines", {})
            for org_id, aeng in tdict.items():
                seng = getattr(aeng, "sync_engine", None)
                pool = getattr(seng, "pool", None)
                if pool is not None:
                    pools.append((pool, seng, f"tenant(async):{org_id}", "async"))

        return pools


# Convenience helpers for lifespan integration
_global_monitor: DbPoolsMonitorService | None = None


def _env_float(name: str) -> float | None:
    value = os.getenv(name)
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _env_bool(name: str) -> bool | None:
    value = os.getenv(name)
    if value is None:
        return None
    return value.lower() in ("1", "true", "yes", "on")


def _resolve_max_checkout(ctx: str, explicit: float | None) -> float | None:
    if explicit is not None:
        return explicit
    if ctx == "worker":
        value = _env_float("WORKER_DB_CONN_MAX_CHECKOUT_SEC")
        if value is not None:
            return value
        return float(pool_monitor_config.WORKER_MAX_CHECKOUT_SEC)
    value = _env_float("APP_DB_CONN_MAX_CHECKOUT_SEC")
    if value is not None:
        return value
    configured = float(pool_monitor_config.APP_MAX_CHECKOUT_SEC)
    fallback = _env_float("DB_POOL_ENFORCE_MAX_CHECKOUT_SEC")
    return fallback if fallback is not None else configured


def _resolve_force_close(ctx: str, explicit: bool | None) -> bool:
    if explicit is not None:
        return bool(explicit)
    if ctx == "worker":
        env_value = _env_bool("WORKER_DB_CONN_FORCE_CLOSE")
        if env_value is not None:
            return env_value
        return bool(pool_monitor_config.WORKER_FORCE_CLOSE)
    env_value = _env_bool("APP_DB_CONN_FORCE_CLOSE")
    if env_value is not None:
        return env_value
    fallback = _env_bool("DB_POOL_ENFORCE_FORCE_CLOSE")
    if fallback is not None:
        return fallback
    return bool(pool_monitor_config.APP_FORCE_CLOSE)


async def start_pool_monitor(
    interval_sec: float = 300.0,
    long_running_threshold_sec: float = 300.0,
    enforce_max_checkout_sec: float | None = None,
    force_close: bool | None = None,
) -> None:
    global _global_monitor
    # Resolve enforcement from environment with app/worker distinction
    ctx = os.getenv("DB_MONITOR_CONTEXT", "app").lower()
    resolved_max_checkout = _resolve_max_checkout(ctx, enforce_max_checkout_sec)
    resolved_force_close = _resolve_force_close(ctx, force_close)
    if _global_monitor is None:
        _global_monitor = DbPoolsMonitorService(
            interval_sec=(
                interval_sec
                if interval_sec is not None
                else float(pool_monitor_config.MONITOR_INTERVAL_SEC)
            ),
            long_running_threshold_sec=long_running_threshold_sec,
            enforce_max_checkout_sec=resolved_max_checkout or 0,
            force_close=resolved_force_close,
        )
    _global_monitor.start()
    # Yield once to the loop so the task can schedule
    await asyncio.sleep(0)


async def stop_pool_monitor() -> None:
    global _global_monitor
    if _global_monitor is not None:
        await _global_monitor.stop()
        _global_monitor = None


def get_pool_snapshot() -> dict[str, Any] | None:
    """Return the latest instantaneous snapshot from the running monitor.

    If the monitor is not running, returns None.
    """
    if _global_monitor is None:
        return None
    try:
        return _global_monitor.collect_snapshot()
    except Exception:
        return None
