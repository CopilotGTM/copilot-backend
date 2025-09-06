"""Celery worker integration for DB pool monitoring.

Starts the DB pool monitor in a lightweight asyncio loop running in a daemon
thread for each Celery worker process. Uses Celery signals to start/stop.

Control via env:
- DB_POOL_MONITOR_ENABLE=true|false (default: true)
- DB_POOL_MONITOR_LOG_LEVEL=INFO|DEBUG (optional)
- DB_POOL_MONITOR_WARMUP_MIN_IDLE=0|1 (optional)
"""

from __future__ import annotations

import asyncio
import os
import threading

from celery.signals import worker_ready, worker_shutdown

from src.infra.utils.logger import get_logger

try:
    from scripts.monitoring.db_pool_monitor import start_pool_monitor, stop_pool_monitor
except Exception:  # pragma: no cover - optional dependency at import time
    start_pool_monitor = None  # type: ignore
    stop_pool_monitor = None  # type: ignore


logger = get_logger("db_pool_monitor")

_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_started: bool = False


def _run_monitor_loop() -> None:
    global _loop
    loop = asyncio.new_event_loop()
    _loop = loop
    asyncio.set_event_loop(loop)

    async def _start():
        if start_pool_monitor is None:
            return
        try:
            # Mark context for worker-specific limits
            os.environ.setdefault("DB_MONITOR_CONTEXT", "worker")
            await start_pool_monitor()
        except Exception as e:  # pragma: no cover
            logger.warning("Celery monitor start failed: %s", e)

    loop.create_task(_start())
    try:
        loop.run_forever()
    finally:
        if stop_pool_monitor is not None:
            try:
                loop.run_until_complete(stop_pool_monitor())
            except Exception:  # pragma: no cover
                pass
        loop.close()


def _enabled() -> bool:
    return os.getenv("DB_POOL_MONITOR_ENABLE", "true").lower() in ("1", "true", "yes", "on")


@worker_ready.connect
def _on_worker_ready(sender=None, **kwargs):  # pragma: no cover - Celery signal
    global _thread, _started
    if not _enabled():
        return
    if _started:
        return
    _started = True
    _thread = threading.Thread(target=_run_monitor_loop, name="db-pool-monitor", daemon=True)
    _thread.start()
    logger.info("DB pool monitor thread started in Celery worker")


@worker_shutdown.connect
def _on_worker_shutdown(sender=None, **kwargs):  # pragma: no cover - Celery signal
    global _loop, _thread
    loop = _loop
    if loop is None:
        return
    try:
        if stop_pool_monitor is not None:
            fut = asyncio.run_coroutine_threadsafe(stop_pool_monitor(), loop)
            try:
                fut.result(timeout=5)
            except Exception:
                pass
        loop.call_soon_threadsafe(loop.stop)
    except Exception:
        pass
