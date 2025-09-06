Database Pool Monitoring
========================

This module instruments SQLAlchemy connection pools for both the master and tenant databases (sync + async providers) and reports:

- Active pools: total, sync, async
- Usage: checked-out connections, pool size, current overflow
- Health: derived OK/WARN/CRITICAL per pool based on utilization
- Long-running checkouts: connections checked out for more than 5 minutes

How it works
- A background task runs via FastAPI lifespan, scanning provider caches for engines and attaching checkout/checkin event listeners to each pool.
- No active queries are executed; metrics are derived from pool state and events.

Files
- `scripts/monitoring/db_pool_monitor.py`: Monitoring service and lifespan helpers

Configuration
- Defaults: interval=30s, long-running threshold=300s.
- To adjust, change the call to `start_pool_monitor()` in `src/api/fastapi_app.py`.

Logs
- Summary line at INFO, now with `size` (open DBAPI connections) and `idle`.
- Per-pool INFO lines when activity/issues (in_use>0, WARN/CRITICAL, long_running>0, alive_sample_ok=False).
- Full JSON snapshot at DEBUG under logger `db_pool_monitor`.

Environment flags
- `DB_POOL_MONITOR_LOG_LEVEL=DEBUG` to see the full per-pool JSON snapshots.
- `DB_POOL_MONITOR_WARMUP_MIN_IDLE=1` to keep at least one idle connection per pool (very light priming for visibility).
Celery workers
- Each worker process has its own pools; start the same monitor inside workers.
- Already wired: worker apps import `scripts.monitoring.celery_integration` which hooks Celery signals to start/stop a lightweight monitor thread.
- Control via env (per pod):
  - `DB_POOL_MONITOR_ENABLE=true|false` (default true)
  - `DB_POOL_MONITOR_LOG_LEVEL=INFO|DEBUG`
  - `DB_POOL_MONITOR_WARMUP_MIN_IDLE=0|1`

Kubernetes note
- Since each Celery pod runs separate worker processes, you’ll see logs per pod/process. Use your log aggregator to group by pod and process.
Enforcement (max checkout age)
- Purpose: prevent leaked connections held for too long.
- Behavior: any connection checked out longer than the configured limit is invalidated (closed on next check‑in). Optionally, you can force‑close the DBAPI socket immediately (dangerous; off by default).
- Config (seconds):
  - Defaults live in `src/infra/config/configs.py:PoolMonitorConfig`
    - App default: 900s (15m)
    - Worker default: 1200s (20m)
  - API app override: `APP_DB_CONN_MAX_CHECKOUT_SEC`
  - Celery workers override: `WORKER_DB_CONN_MAX_CHECKOUT_SEC`
  - Fallback for both: `DB_POOL_ENFORCE_MAX_CHECKOUT_SEC`
  - Force close: `APP_DB_CONN_FORCE_CLOSE` / `WORKER_DB_CONN_FORCE_CLOSE` / fallback `DB_POOL_ENFORCE_FORCE_CLOSE` (true/false)
- Notes:
  - Immediate force-close can disrupt in-flight operations; prefer the default invalidate-on-checkin behavior.
  - Statement/transaction timeouts are still recommended at the DB level for true execution limits.
