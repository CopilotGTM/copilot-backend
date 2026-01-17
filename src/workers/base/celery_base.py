import logging
from json import dumps as json_dumps
from typing import Any

from celery import Celery
from celery.signals import task_failure, task_postrun, task_prerun

from src.infra.observability.context import load_from_headers, reset_context, set_celery_context
from src.infra.utils.logger import get_logger

_signals_registered = False


def _register_observability_signals() -> None:
    global _signals_registered
    if _signals_registered:
        return

    _signals_registered = True

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
            "args": args,
            "kwargs": kwargs,
            "exception": repr(exception) if exception else None,
            "traceback": traceback,
            "signal_extra": extra or None,
        }
        try:
            message = json_dumps(payload, ensure_ascii=False, default=str)
        except Exception:
            message = (
                f"celery_task_failed task_id={task_id} task_name={getattr(task, 'name', None)}"
            )

        if einfo:
            logger.error(message)
        else:
            logger.error(message)


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
        # Task execution settings
        task_track_started=True,
        task_time_limit=default_time_limit,
        task_soft_time_limit=default_soft_time_limit,
        # Worker settings
        worker_hijack_root_logger=False,
        worker_log_color=False,
    )

    return celery_app
