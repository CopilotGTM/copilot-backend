"""Shared Celery app factory and runtime signal wiring for all workers.

Worker modules call `create_celery_app` to get consistent broker settings,
task routing, observability hooks, and prefork cleanup across API-adjacent
background jobs.
"""

import logging
import os
import time
from typing import Any

from celery import Celery, current_app
from celery.signals import (
    beat_embedded_init,
    beat_init,
    task_failure,
    task_postrun,
    task_prerun,
    worker_process_init,
    worker_ready,
)

from src.infra.database.tenant_runtime_config import (
    validate_current_shared_tenant_runtime_startup,
)
from src.infra.database.tenant_runtime_credentials import (
    validate_current_shared_tenant_runtime_password_sync,
)
from src.infra.observability.context import (
    generate_request_id,
    get_request_id,
    load_from_headers,
    reset_context,
    set_celery_context,
    set_request_id,
)
from src.infra.observability.metrics import record_celery_task_memory, record_celery_task_run
from src.infra.observability.process_memory import get_current_rss_mb
from src.infra.observability.sentry import init_sentry
from src.infra.secrets.master_key_vault_sync import get_master_key_vault_secret_sync
from src.infra.utils.logger import get_logger, resolve_celery_component

init_sentry()

_signals_registered = False

_DEFAULT_CELERY_TASK_TIME_LIMIT_SECONDS = 300
_DEFAULT_CELERY_TASK_SOFT_TIME_LIMIT_SECONDS = 240


def _validate_tenant_runtime_config_startup() -> None:
    validate_current_shared_tenant_runtime_startup()


def _validate_tenant_runtime_startup() -> None:
    _validate_tenant_runtime_config_startup()
    validate_current_shared_tenant_runtime_password_sync(
        get_secret=get_master_key_vault_secret_sync,
    )


def _resolve_signal_app_name(sender: Any | None = None) -> str | None:
    app = getattr(sender, "app", None)
    app_name = getattr(app, "main", None)
    if app_name:
        return str(app_name)

    try:
        current_app_name = getattr(current_app, "main", None)
    except Exception:
        current_app_name = None

    if current_app_name:
        return str(current_app_name)

    return None


def _resolve_runtime_service_name_suffix(sender: Any | None = None) -> str:
    app_name = _resolve_signal_app_name(sender)
    if not app_name:
        return ""
    return f"-{resolve_celery_component(app_name)}"


def _resolve_task_queue(task: Any | None) -> str | None:
    try:
        # Routing key is the most useful queue label in dashboards; exchange is a fallback.
        delivery_info = getattr(getattr(task, "request", None), "delivery_info", None) or {}
        queue = delivery_info.get("routing_key") or delivery_info.get("exchange")
    except Exception:
        queue = None
    return str(queue) if queue else None


def _is_task_memory_logging_enabled() -> bool:
    return os.getenv("CELERY_TASK_MEMORY_LOGGING_ENABLED", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _task_memory_min_delta_mb() -> float:
    try:
        return float(os.getenv("CELERY_TASK_MEMORY_LOGGING_MIN_DELTA_MB", "0"))
    except ValueError:
        return 0.0


def _should_emit_task_memory(rss_delta_mb: float | None) -> bool:
    min_delta_mb = _task_memory_min_delta_mb()
    if min_delta_mb <= 0:
        return True
    if rss_delta_mb is None:
        return True
    return abs(rss_delta_mb) >= min_delta_mb


def _register_observability_signals() -> None:
    global _signals_registered
    if _signals_registered:
        return

    _signals_registered = True

    @worker_process_init.connect(weak=False)
    def _reset_db_after_fork(**kwargs):  # noqa: ARG001
        """Reset DB provider singletons after prefork child process starts.

        Child processes inherit the parent's memory including lazily-created
        DB provider singletons that hold connections/pools bound to the
        parent's event loop. Dispose and clear them so fresh engines are
        created in the child.
        """
        from src.infra.utils.async_runner import reset_after_fork

        reset_after_fork()

    @worker_process_init.connect(weak=False)
    def _validate_tenant_runtime_after_fork(sender=None, **kwargs):  # noqa: ARG001
        _validate_tenant_runtime_config_startup()

    @worker_process_init.connect(weak=False)
    def _init_otel_after_fork(sender=None, **kwargs):  # noqa: ARG001
        from src.infra.observability.otel import init_otel

        init_otel(service_name_suffix=_resolve_runtime_service_name_suffix(sender))

    @worker_ready.connect(weak=False)
    def _validate_tenant_runtime_in_main_process(sender=None, **kwargs):  # noqa: ARG001
        _validate_tenant_runtime_startup()

    @worker_ready.connect(weak=False)
    def _init_otel_in_main_process(sender=None, **kwargs):  # noqa: ARG001
        from src.infra.observability.otel import init_otel

        init_otel(service_name_suffix=_resolve_runtime_service_name_suffix(sender))

    @beat_init.connect(weak=False)
    def _init_otel_for_beat(sender=None, **kwargs):  # noqa: ARG001
        from src.infra.observability.otel import init_otel

        init_otel(service_name_suffix=_resolve_runtime_service_name_suffix(sender))

    @beat_embedded_init.connect(weak=False)
    def _init_otel_for_embedded_beat(sender=None, **kwargs):  # noqa: ARG001
        from src.infra.observability.otel import init_otel

        init_otel(service_name_suffix=_resolve_runtime_service_name_suffix(sender))

    @task_prerun.connect(weak=False)
    def _task_prerun_handler(task_id=None, task=None, **kwargs):  # noqa: ARG001
        headers: dict[str, Any] = {}
        try:
            headers = getattr(getattr(task, "request", None), "headers", None) or {}
        except Exception:
            headers = {}

        reset_context()
        load_from_headers(headers)
        if not get_request_id():
            # Beat and other root producers often dispatch without headers; seed a
            # request id here so any fan-out tasks inherit a stable correlation id.
            set_request_id(generate_request_id())

        queue = _resolve_task_queue(task)

        set_celery_context(
            task_id=str(task_id) if task_id else None,
            task_name=getattr(task, "name", None),
            queue=queue,
        )

        request = getattr(task, "request", None)
        if request is not None:
            # Keep timing state on the Celery request object so postrun can emit duration reliably.
            request._observability_started_at = time.perf_counter()
            if _is_task_memory_logging_enabled():
                request._observability_rss_start_mb = get_current_rss_mb()

    @task_postrun.connect(weak=False)
    def _task_postrun_handler(task_id=None, task=None, **kwargs):  # noqa: ARG001
        request = getattr(task, "request", None)
        started_at = getattr(request, "_observability_started_at", None)
        duration_ms = (
            round((time.perf_counter() - started_at) * 1000)
            if isinstance(started_at, (int, float))
            else None
        )

        if request is not None and hasattr(request, "_observability_started_at"):
            del request._observability_started_at

        rss_start_mb = getattr(request, "_observability_rss_start_mb", None)
        if request is not None and hasattr(request, "_observability_rss_start_mb"):
            del request._observability_rss_start_mb

        task_name = getattr(task, "name", None)
        queue = _resolve_task_queue(task)
        status = str(kwargs.get("state") or "UNKNOWN")

        # Emit task metrics before resetting context so queue/task labels still resolve correctly.
        record_celery_task_run(
            task_name=task_name,
            queue=queue,
            status=status,
            duration_ms=duration_ms,
        )
        if _is_task_memory_logging_enabled():
            rss_end_mb = get_current_rss_mb()
            rss_delta_mb = (
                rss_end_mb - rss_start_mb
                if isinstance(rss_start_mb, (int, float)) and rss_end_mb is not None
                else None
            )
            if rss_end_mb is not None and _should_emit_task_memory(rss_delta_mb):
                record_celery_task_memory(
                    task_name=task_name,
                    queue=queue,
                    status=status,
                    rss_mb=rss_end_mb,
                    rss_delta_mb=rss_delta_mb,
                )
                get_logger("celery_task").info(
                    "Celery task memory usage",
                    extra={
                        "event": "celery_task_memory",
                        "task_name": task_name,
                        "queue": queue,
                        "status": status,
                        "duration_ms": duration_ms,
                        "rss_start_mb": rss_start_mb,
                        "rss_end_mb": rss_end_mb,
                        "rss_delta_mb": rss_delta_mb,
                        "process_id": os.getpid(),
                    },
                )
        reset_context()

    @task_failure.connect(weak=False)
    def _task_failure_handler(
        task_id=None,
        exception=None,
        args=None,
        kwargs=None,
        traceback=None,  # noqa: A002 - matches Celery signature
        einfo=None,
        task=None,
        **extra,
    ):  # noqa: ANN001, ARG001 - Celery signal signature
        logger = get_logger("celery_task")
        payload = {
            "event": "celery_task_failed",
            "task_id": str(task_id) if task_id else None,
            "task_name": getattr(task, "name", None),
            "task_args": args,
            "task_kwargs": kwargs,
            "task_exception": repr(exception) if exception else None,
            "task_traceback": traceback,
            "signal_extra": extra or None,
        }
        logger.error(
            "Celery task failed",
            exc_info=einfo if einfo else None,
            extra=payload,
        )


def create_celery_app(worker_name, config, tasks_module):
    """Factory function to create a Celery app for a specific worker."""
    _register_observability_signals()
    task_includes = [tasks_module] if isinstance(tasks_module, str) else list(tasks_module)

    celery_app = Celery(
        worker_name,
        broker=config.broker_url,
        backend=config.backend_url,
        include=task_includes,
    )

    # Reduce noisy success logs that include "errors" fields and trip dashboards.
    logging.getLogger("celery.app.trace").setLevel(logging.WARNING)

    # Apply SSL options if they are present in the config
    if config.ssl_config:
        celery_app.conf.broker_use_ssl = config.ssl_config
        celery_app.conf.redis_backend_use_ssl = config.ssl_config

    worker_config_updates: dict[str, Any] = {}

    if hasattr(config, "task_queues"):
        celery_app.conf.task_queues = config.task_queues

    if hasattr(config, "task_default_queue"):
        worker_config_updates["task_default_queue"] = config.task_default_queue

    if hasattr(config, "task_default_exchange"):
        worker_config_updates["task_default_exchange"] = config.task_default_exchange

    if hasattr(config, "task_default_exchange_type"):
        worker_config_updates["task_default_exchange_type"] = config.task_default_exchange_type

    if hasattr(config, "task_default_routing_key"):
        worker_config_updates["task_default_routing_key"] = config.task_default_routing_key

    if worker_config_updates:
        celery_app.conf.update(worker_config_updates)

    if hasattr(config, "task_routes"):
        celery_app.conf.task_routes = config.task_routes

    if hasattr(config, "beat_schedule") and config.beat_schedule:
        celery_app.conf.beat_schedule = config.beat_schedule

    # Default time limits (can be overridden by worker-specific config)
    default_time_limit = _DEFAULT_CELERY_TASK_TIME_LIMIT_SECONDS
    default_soft_time_limit = _DEFAULT_CELERY_TASK_SOFT_TIME_LIMIT_SECONDS

    # Allow worker-specific time limits
    if hasattr(config, "task_time_limit"):
        default_time_limit = config.task_time_limit
    if hasattr(config, "task_soft_time_limit"):
        default_soft_time_limit = config.task_soft_time_limit

    # Add essential Celery configuration for task consumption
    celery_app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        worker_prefetch_multiplier=1,  # Important for fair task distribution
        task_acks_late=True,  # Prevent task loss
        worker_disable_rate_limits=True,  # Disable rate limiting for better performance
        # Connection settings
        broker_connection_retry_on_startup=True,
        broker_connection_retry=True,
        broker_connection_max_retries=10,
        # Azure Redis can silently close idle TLS sockets. Keepalive + broker
        # health checks ensure Beat and workers detect stale sockets promptly
        # instead of hanging on a dead connection after the next scheduled tick.
        broker_transport_options={
            "socket_timeout": 30,
            "socket_connect_timeout": 30,
            "socket_keepalive": True,
            "retry_on_timeout": True,
            "health_check_interval": 30,
        },
        # Task execution settings
        task_track_started=True,
        task_time_limit=default_time_limit,
        task_soft_time_limit=default_soft_time_limit,
        # Worker settings
        worker_hijack_root_logger=False,
        worker_log_color=False,
    )

    return celery_app
