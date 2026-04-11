import logging
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

from src.infra.observability.context import load_from_headers, reset_context, set_celery_context
from src.infra.observability.sentry import init_sentry
from src.infra.utils.logger import get_logger, resolve_celery_component

init_sentry()

_signals_registered = False


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
        from src.infra.utils.async_runner import (
            _clear_async_db_provider_cache,
            _clear_sync_db_provider_cache,
        )

        _clear_async_db_provider_cache()
        _clear_sync_db_provider_cache()

    @worker_process_init.connect(weak=False)
    def _init_otel_after_fork(sender=None, **kwargs):  # noqa: ARG001
        from src.infra.observability.otel import init_otel

        init_otel(service_name_suffix=_resolve_runtime_service_name_suffix(sender))

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

        load_from_headers(headers)

        queue = None
        try:
            delivery_info = getattr(getattr(task, "request", None), "delivery_info", None) or {}
            queue = delivery_info.get("routing_key") or delivery_info.get("exchange")
        except Exception:
            queue = None

        set_celery_context(
            task_id=str(task_id) if task_id else None,
            task_name=getattr(task, "name", None),
            queue=str(queue) if queue else None,
        )

    @task_postrun.connect(weak=False)
    def _task_postrun_handler(task_id=None, task=None, **kwargs):  # noqa: ARG001
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

    celery_app = Celery(
        worker_name,
        broker=config.broker_url,
        backend=config.backend_url,
        include=[tasks_module],
    )

    # Reduce noisy success logs that include "errors" fields and trip dashboards.
    logging.getLogger("celery.app.trace").setLevel(logging.WARNING)

    # Apply SSL options if they are present in the config
    if config.ssl_config:
        celery_app.conf.broker_use_ssl = config.ssl_config
        celery_app.conf.redis_backend_use_ssl = config.ssl_config

    # Set worker-specific queue configurations
    if hasattr(config, "task_queues"):
        celery_app.conf.task_queues = config.task_queues

    if hasattr(config, "task_default_queue"):
        celery_app.conf.task_default_queue = config.task_default_queue

    # Set default exchange settings
    if hasattr(config, "task_default_exchange"):
        celery_app.conf.task_default_exchange = config.task_default_exchange

    if hasattr(config, "task_default_exchange_type"):
        celery_app.conf.task_default_exchange_type = config.task_default_exchange_type

    if hasattr(config, "task_default_routing_key"):
        celery_app.conf.task_default_routing_key = config.task_default_routing_key

    # Set task routing if defined in the worker's config
    if hasattr(config, "task_routes"):
        celery_app.conf.task_routes = config.task_routes

    # Set beat schedule if defined in the worker's config (for periodic tasks)
    if hasattr(config, "beat_schedule") and config.beat_schedule:
        celery_app.conf.beat_schedule = config.beat_schedule

    # Default time limits (can be overridden by worker-specific config)
    default_time_limit = 300  # 5 minutes
    default_soft_time_limit = 240  # 4 minutes

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
