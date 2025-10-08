from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Optional, Tuple
import itertools
import os
import logging

from sqlalchemy import event

from src.infra.database.factory import get_sync_provider, get_async_provider
from src.infra.config.configs import pool_monitor_config
from src.infra.utils.logger import get_logger


logger = get_logger("db_pool_monitor")
# Allow env-based override of log level (e.g., DB_POOL_MONITOR_LOG_LEVEL=DEBUG)
_lvl = os.getenv("DB_POOL_MONITOR_LOG_LEVEL")
if _lvl:
    try:
        logger.setLevel(getattr(logging, _lvl.upper(), logging.INFO))
    except Exception:
        pass


@dataclass
class PoolMetrics:
    label: str
    pool_id: int
    kind: str  # "sync" | "async"
    size: Optional[int]
    checked_in: Optional[int]
    checked_out: Optional[int]
    current_overflow: Optional[int]
    max_overflow: Optional[int]
    long_running_checked_out: int
    longest_checkout_seconds: Optional[float]
    status: Optional[str]
    health: str  # derived: OK | WARN | CRITICAL | UNKNOWN


class SinglePoolMonitor:
    """
    Attaches checkout/checkin listeners to a SQLAlchemy Pool and tracks how long
    individual connections are checked out.
    """

    def __init__(self, pool: Any, label: str, kind: str, engine: Optional[Any] = None):
        self.pool = pool
        self.label = label
        self.kind = kind
        self.engine = engine  # underlying sync engine for optional probes
        self._lock = threading.Lock()
        # Maps id(connection_record) -> (checkout_time_monotonic, connection_record, dbapi_connection)
        self._checked_out: Dict[int, tuple[float, Any, Any]] = {}
        # Keep listener fns to remove later
        self._listeners_attached = False
        # Counters
        self._checkout_count = 0
        self._checkin_count = 0
        self._connect_count = 0
        self._invalidate_count = 0
        self._close_count = 0
        self._enforced_count = 0
        # Probe state
        self._last_probe_ts: Optional[float] = None
        self._last_probe_ok: Optional[bool] = None

    def attach(self) -> None:
        if self._listeners_attached:
            return

        def on_checkout(dbapi_con, con_record, *args):
            try:
                key = id(con_record)
            except Exception:
                return
            now = time.monotonic()
            with self._lock:
                self._checked_out[key] = (now, con_record, dbapi_con)
                self._checkout_count += 1

        def on_checkin(dbapi_con, con_record):
            try:
                key = id(con_record)
            except Exception:
                return
            with self._lock:
                self._checked_out.pop(key, None)
                self._checkin_count += 1

        def on_connect(dbapi_con, con_record):
            with self._lock:
                self._connect_count += 1

        def on_invalidate(dbapi_con, con_record, exception):
            with self._lock:
                self._invalidate_count += 1

        def on_close(dbapi_con, con_record):
            with self._lock:
                self._close_count += 1

        # Attach to the pool instance for precision
        event.listen(self.pool, "checkout", on_checkout)
        event.listen(self.pool, "checkin", on_checkin)
        event.listen(self.pool, "connect", on_connect)
        event.listen(self.pool, "invalidate", on_invalidate)
        event.listen(self.pool, "close", on_close)
        self._on_checkout = on_checkout  # type: ignore[attr-defined]
        self._on_checkin = on_checkin  # type: ignore[attr-defined]
        self._on_connect = on_connect  # type: ignore[attr-defined]
        self._on_invalidate = on_invalidate  # type: ignore[attr-defined]
        self._on_close = on_close  # type: ignore[attr-defined]
        self._listeners_attached = True

    def detach(self) -> None:
        if not self._listeners_attached:
            return
        try:
            event.remove(self.pool, "checkout", self._on_checkout)  # type: ignore[attr-defined]
            event.remove(self.pool, "checkin", self._on_checkin)  # type: ignore[attr-defined]
            event.remove(self.pool, "connect", self._on_connect)  # type: ignore[attr-defined]
            event.remove(self.pool, "invalidate", self._on_invalidate)  # type: ignore[attr-defined]
            event.remove(self.pool, "close", self._on_close)  # type: ignore[attr-defined]
        except Exception:
            pass
        self._listeners_attached = False

    def ensure_min_idle(self, min_idle: int = 1, max_new: int = 1) -> None:
        """Prime the pool with a minimal number of idle connections.

        Conservative: opens at most `max_new` connections per call, and only
        if fewer than `min_idle` are currently idle.
        """
        if self.engine is None or min_idle <= 0 or max_new <= 0:
            return
        try:
            checked_in = self._call_maybe("checkedin")
            current_idle = int(checked_in or 0)
        except Exception:
            return
        deficit = max(0, min_idle - current_idle)
        to_open = min(deficit, max_new)
        for _ in range(to_open):
            try:
                with self.engine.connect() as _:
                    pass
            except Exception:
                break

    def metrics(self, threshold_sec: float = 300.0) -> PoolMetrics:
        # pool API varies slightly; use getattr with defaults
        size = self._call_maybe("size")
        checked_in = self._call_maybe("checkedin")
        checked_out = self._call_maybe("checkedout")
        current_overflow = self._call_maybe("overflow")
        max_overflow = getattr(self.pool, "_max_overflow", None)
        status = self._call_maybe("status")
        configured_pool_size = None
        try:
            configured_pool_size = getattr(
                getattr(self.pool, "_pool", None), "maxsize", None
            )
        except Exception:
            configured_pool_size = None

        now = time.monotonic()
        with self._lock:
            long_running = [
                t for (t, _, _) in self._checked_out.values() if now - t > threshold_sec
            ]
            longest = max(
                ((now - t) for (t, _, _) in self._checked_out.values()), default=None
            )

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

    def enforce_checkout_limit(
        self, max_sec: float, *, force_close: bool = False
    ) -> int:
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
        for key, t, rec, dbc in to_handle:
            try:
                # Mark to be closed on check-in
                rec.invalidate()
                count += 1
                with self._lock:
                    self._enforced_count += 1
                if force_close and dbc is not None:
                    try:
                        dbc.close()
                    except Exception:
                        pass
            except Exception:
                # Ignore; keep observing
                pass
        return count

    def maybe_probe(self, min_interval_sec: float = 120.0) -> None:
        if self.engine is None:
            return
        now = time.monotonic()
        last = self._last_probe_ts or 0.0
        if now - last < min_interval_sec:
            return
        # Only probe when we have idle connections to avoid creating new ones
        checked_in = self._call_maybe("checkedin")
        try:
            if isinstance(checked_in, int) and checked_in <= 0:
                self._last_probe_ts = now
                self._last_probe_ok = None
                return
        except Exception:
            pass
        ok = False
        try:
            # Use underlying sync engine to benefit from pool_pre_ping
            with self.engine.connect() as _:
                ok = True
        except Exception:
            ok = False
        self._last_probe_ts = now
        self._last_probe_ok = ok

    def _call_maybe(self, name: str) -> Optional[int | str]:
        try:
            attr = getattr(self.pool, name, None)
            if callable(attr):
                return attr()
            return attr  # type: ignore[return-value]
        except Exception:
            return None

    @staticmethod
    def _derive_health(
        size: Optional[int], checked_out: Optional[int], max_overflow: Optional[int]
    ) -> str:
        try:
            sz = int(size or 0)
            co = int(checked_out or 0)
            mo = (
                max_overflow
                if (isinstance(max_overflow, int) and max_overflow >= 0)
                else 0
            )
            capacity = sz + mo if (sz + mo) > 0 else max(sz, 1)
            usage = co / capacity
            if co >= capacity:
                return "CRITICAL"
            if usage >= 0.8:
                return "WARN"
            return "OK"
        except Exception:
            return "UNKNOWN"


class DbPoolsMonitorService:
    """
    Periodically scans the master and tenant pools (sync and async providers),
    attaches listeners to track checkout durations, and logs aggregated metrics.
    """

    def __init__(
        self,
        interval_sec: float = 30.0,
        long_running_threshold_sec: float = 300.0,
        enforce_max_checkout_sec: Optional[float] = None,
        force_close: bool = False,
    ):
        self.interval_sec = interval_sec
        self.long_running_threshold_sec = long_running_threshold_sec
        self._enforce_max_checkout_sec = enforce_max_checkout_sec
        self._enforce_force_close = force_close
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()
        # Pool id -> monitor
        self._monitors: Dict[int, SinglePoolMonitor] = {}

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
            except asyncio.TimeoutError:
                self._task.cancel()
        for mon in self._monitors.values():
            try:
                mon.detach()
            except Exception:
                pass
        self._monitors.clear()

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                self._discover_and_attach()
                snapshot = self.collect_snapshot()
                self._log_snapshot(snapshot)
            except Exception as e:
                logger.error("DB pool monitor tick failed: %s", e)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.interval_sec)
            except asyncio.TimeoutError:
                pass

    def _discover_and_attach(self) -> None:
        # Warm up master engines (no connections opened) so their pools become discoverable
        try:
            sp = get_sync_provider()
            getme = getattr(sp, "_get_master_engine", None)
            if callable(getme):
                getme()
        except Exception:
            pass
        try:
            ap = get_async_provider()
            getme = getattr(ap, "_get_master_engine", None)
            if callable(getme):
                getme()
        except Exception:
            pass

        # Grab pools from sync and async providers without forcing tenant creation
        # Optional: warmup minimal idle
        try:
            warm_min_idle = int(os.getenv("DB_POOL_MONITOR_WARMUP_MIN_IDLE", "0"))
        except Exception:
            warm_min_idle = 0

        for pool, engine, label, kind in itertools.chain(
            self._iter_sync_pools(), self._iter_async_pools()
        ):
            pid = id(pool)
            if pid not in self._monitors:
                mon = SinglePoolMonitor(
                    pool=pool, label=label, kind=kind, engine=engine
                )
                try:
                    mon.attach()
                    self._monitors[pid] = mon
                    logger.info("Attached monitor to pool: %s (%s)", label, kind)
                except Exception as e:
                    logger.warning("Failed to attach monitor to pool %s: %s", label, e)
            if warm_min_idle > 0:
                try:
                    self._monitors[pid].ensure_min_idle(
                        min_idle=warm_min_idle, max_new=1
                    )
                except Exception:
                    pass
            # optional warmup and probe + enforcement
            try:
                self._monitors[pid].maybe_probe(
                    min_interval_sec=max(self.interval_sec * 4.0, 120.0)
                )
            except Exception:
                pass
            try:
                if (
                    self._enforce_max_checkout_sec
                    and self._enforce_max_checkout_sec > 0
                ):
                    n = self._monitors[pid].enforce_checkout_limit(
                        self._enforce_max_checkout_sec,
                        force_close=self._enforce_force_close,
                    )
                    if n:
                        logger.warning(
                            "Enforced max checkout on %s connections for pool %s",
                            n,
                            label,
                        )
            except Exception:
                pass

    def collect_snapshot(self) -> Dict[str, Any]:
        metrics: List[Dict[str, Any]] = []
        for mon in self._monitors.values():
            pm = mon.metrics(self.long_running_threshold_sec)
            d = asdict(pm)
            # augment fields that SinglePoolMonitor prepared via state
            # Pull configured size et al via a fresh call to keep in sync
            configured_pool_size = None
            try:
                configured_pool_size = getattr(
                    getattr(mon.pool, "_pool", None), "maxsize", None
                )
            except Exception:
                configured_pool_size = None
            checked_in = d.get("checked_in")
            checked_out = d.get("checked_out")
            max_overflow = d.get("max_overflow")
            capacity_total = (configured_pool_size or 0) + (
                max_overflow or 0 if isinstance(max_overflow, int) else 0
            )
            in_use = d.get("checked_out")
            d.update(
                {
                    "configured_pool_size": configured_pool_size,
                    "capacity_total": capacity_total,
                    "idle": checked_in,
                    "in_use": checked_out,
                    "total_open": (
                        None
                        if (checked_in is None or checked_out is None)
                        else int(checked_in) + int(checked_out)
                    ),
                    "usage_pct": (
                        None
                        if not capacity_total
                        else float(in_use or 0) / float(capacity_total)
                    ),
                    "counters": {
                        "checkout": mon._checkout_count,
                        "checkin": mon._checkin_count,
                        "connect": mon._connect_count,
                        "invalidate": mon._invalidate_count,
                        "close": mon._close_count,
                        "enforced": mon._enforced_count,
                    },
                    "alive_sample_ok": mon._last_probe_ok,
                    "last_probe_ts": mon._last_probe_ts,
                }
            )
            metrics.append(d)

        summary = {
            "total_pools": len(metrics),
            "sync_pools": sum(1 for m in metrics if m.get("kind") == "sync"),
            "async_pools": sum(1 for m in metrics if m.get("kind") == "async"),
            "total_checked_out": sum(int(m.get("checked_out") or 0) for m in metrics),
            "total_size": sum(int(m.get("size") or 0) for m in metrics),
            "total_idle": sum(int(m.get("checked_in") or 0) for m in metrics),
            "total_current_overflow": sum(
                int(m.get("current_overflow") or 0) for m in metrics
            ),
            "total_long_running": sum(
                int(m.get("long_running_checked_out") or 0) for m in metrics
            ),
            "total_enforced": sum(
                int((m.get("counters") or {}).get("enforced") or 0) for m in metrics
            ),
            "health": {
                "OK": sum(1 for m in metrics if m.get("health") == "OK"),
                "WARN": sum(1 for m in metrics if m.get("health") == "WARN"),
                "CRITICAL": sum(1 for m in metrics if m.get("health") == "CRITICAL"),
                "UNKNOWN": sum(1 for m in metrics if m.get("health") == "UNKNOWN"),
            },
        }

        return {"summary": summary, "pools": metrics}

    def _log_snapshot(self, snapshot: Dict[str, Any]) -> None:
        # Compact JSON for machine parsing, plus a human message
        try:
            health = snapshot["summary"]["health"]
            logger.info(
                "DB Pools: total=%s sync=%s async=%s checked_out=%s size=%s idle=%s long_running>%ss=%s enforced=%s health(OK=%s WARN=%s CRIT=%s)",
                snapshot["summary"]["total_pools"],
                snapshot["summary"]["sync_pools"],
                snapshot["summary"]["async_pools"],
                snapshot["summary"]["total_checked_out"],
                snapshot["summary"].get("total_size"),
                snapshot["summary"].get("total_idle"),
                int(self.long_running_threshold_sec),
                snapshot["summary"]["total_long_running"],
                snapshot["summary"].get("total_enforced"),
                health.get("OK"),
                health.get("WARN"),
                health.get("CRITICAL"),
            )
        except Exception:
            pass
        # Log notable per-pool lines at INFO so they are visible without DEBUG
        try:
            for p in snapshot.get("pools", []):
                in_use = int(
                    p.get("in_use")
                    if p.get("in_use") is not None
                    else (p.get("checked_out") or 0)
                )
                idle = int(
                    p.get("idle")
                    if p.get("idle") is not None
                    else (p.get("checked_in") or 0)
                )
                usage = p.get("usage_pct")
                long_running = int(p.get("long_running_checked_out") or 0)
                alive = p.get("alive_sample_ok")
                health = p.get("health")
                enforced = int((p.get("counters") or {}).get("enforced") or 0)
                if (
                    in_use > 0
                    or long_running > 0
                    or enforced > 0
                    or alive is False
                    or health in ("WARN", "CRITICAL")
                ):
                    logger.info(
                        "Pool %s: in_use=%s idle=%s size=%s cap=%s overflow=%s usage=%.2f health=%s alive=%s longest=%s enforced=%s",
                        p.get("label"),
                        in_use,
                        idle,
                        p.get("size"),
                        p.get("capacity_total"),
                        p.get("current_overflow"),
                        0.0 if not usage else float(usage),
                        health,
                        alive,
                        p.get("longest_checkout_seconds"),
                        enforced,
                    )
        except Exception:
            pass
        logger.debug("DB Pool snapshot: %s", json.dumps(snapshot))

    def _iter_sync_pools(self) -> Iterable[Tuple[Any, Any, str, str]]:
        """Yield (pool, engine, label, kind) for sync master + tenant engines."""
        try:
            provider = get_sync_provider()
        except Exception:
            return []

        pools: List[Tuple[Any, Any, str, str]] = []

        # Master engine (if created)
        try:
            eng = getattr(provider, "_master_engine", None)
            if eng is not None:
                pool = getattr(eng, "pool", None)
                if pool is not None:
                    pools.append((pool, eng, "master(sync)", "sync"))
        except Exception:
            pass

        # Tenant engines (if any)
        try:
            tdict: Dict[Any, Any] = getattr(provider, "_tenant_engines", {})
            for org_id, eng in tdict.items():
                pool = getattr(eng, "pool", None)
                if pool is not None:
                    pools.append((pool, eng, f"tenant(sync):{org_id}", "sync"))
        except Exception:
            pass

        return pools

    def _iter_async_pools(self) -> Iterable[Tuple[Any, Any, str, str]]:
        """Yield (pool, sync_engine, label, kind) for async master + tenant engines."""
        try:
            provider = get_async_provider()
        except Exception:
            return []

        pools: List[Tuple[Any, Any, str, str]] = []

        # Master async engine (if created)
        try:
            aeng = getattr(provider, "_master_engine", None)
            if aeng is not None:
                seng = getattr(aeng, "sync_engine", None)
                pool = getattr(seng, "pool", None)
                if pool is not None:
                    pools.append((pool, seng, "master(async)", "async"))
        except Exception:
            pass

        # Tenant async engines (if any)
        try:
            tdict: Dict[Any, Any] = getattr(provider, "_tenant_engines", {})
            for org_id, aeng in tdict.items():
                seng = getattr(aeng, "sync_engine", None)
                pool = getattr(seng, "pool", None)
                if pool is not None:
                    pools.append((pool, seng, f"tenant(async):{org_id}", "async"))
        except Exception:
            pass

        return pools


# Convenience helpers for lifespan integration
_global_monitor: Optional[DbPoolsMonitorService] = None


async def start_pool_monitor(
    interval_sec: float = 30.0,
    long_running_threshold_sec: float = 300.0,
    enforce_max_checkout_sec: Optional[float] = None,
    force_close: Optional[bool] = None,
) -> None:
    global _global_monitor
    # Resolve enforcement from environment with app/worker distinction
    ctx = os.getenv("DB_MONITOR_CONTEXT", "app").lower()

    def _getf(name: str) -> Optional[float]:
        v = os.getenv(name)
        try:
            return None if v is None or v == "" else float(v)
        except Exception:
            return None

    if enforce_max_checkout_sec is None:
        if ctx == "worker":
            enforce_max_checkout_sec = _getf("WORKER_DB_CONN_MAX_CHECKOUT_SEC")
            if enforce_max_checkout_sec is None:
                enforce_max_checkout_sec = float(
                    pool_monitor_config.WORKER_MAX_CHECKOUT_SEC
                )
        else:
            enforce_max_checkout_sec = _getf("APP_DB_CONN_MAX_CHECKOUT_SEC")
            if enforce_max_checkout_sec is None:
                enforce_max_checkout_sec = float(
                    pool_monitor_config.APP_MAX_CHECKOUT_SEC
                )
        if enforce_max_checkout_sec is None:
            enforce_max_checkout_sec = _getf("DB_POOL_ENFORCE_MAX_CHECKOUT_SEC")
    if force_close is None:
        if ctx == "worker":
            if os.getenv("WORKER_DB_CONN_FORCE_CLOSE") is not None:
                force_close = os.getenv(
                    "WORKER_DB_CONN_FORCE_CLOSE", "false"
                ).lower() in ("1", "true", "yes", "on")
            else:
                force_close = bool(pool_monitor_config.WORKER_FORCE_CLOSE)
        else:
            if os.getenv("APP_DB_CONN_FORCE_CLOSE") is not None:
                force_close = os.getenv("APP_DB_CONN_FORCE_CLOSE", "false").lower() in (
                    "1",
                    "true",
                    "yes",
                    "on",
                )
            else:
                force_close = bool(pool_monitor_config.APP_FORCE_CLOSE)
        if force_close is None:
            force_close = os.getenv("DB_POOL_ENFORCE_FORCE_CLOSE", "false").lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
    if _global_monitor is None:
        _global_monitor = DbPoolsMonitorService(
            interval_sec=(
                interval_sec
                if interval_sec is not None
                else float(pool_monitor_config.MONITOR_INTERVAL_SEC)
            ),
            long_running_threshold_sec=long_running_threshold_sec,
            enforce_max_checkout_sec=enforce_max_checkout_sec or 0,
            force_close=bool(force_close),
        )
    _global_monitor.start()
    # Yield once to the loop so the task can schedule
    await asyncio.sleep(0)


async def stop_pool_monitor() -> None:
    global _global_monitor
    if _global_monitor is not None:
        await _global_monitor.stop()
        _global_monitor = None


def get_pool_snapshot() -> Optional[Dict[str, Any]]:
    """Return the latest instantaneous snapshot from the running monitor.

    If the monitor is not running, returns None.
    """
    if _global_monitor is None:
        return None
    try:
        return _global_monitor.collect_snapshot()
    except Exception:
        return None
