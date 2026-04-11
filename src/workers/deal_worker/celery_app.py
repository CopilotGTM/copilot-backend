from celery.signals import (
    worker_ready,
    worker_shutdown,
)
from dotenv import load_dotenv

# Load environment variables
load_dotenv(override=True)

from src.infra.utils.logger import configure_logging, resolve_celery_component

configure_logging(component=resolve_celery_component("deal_worker"))

from src.infra.messaging.redis_client import close_redis_pool, initialize_redis_pool
from src.infra.utils.logger import get_logger
from src.workers.base.celery_base import create_celery_app
from src.workers.deal_worker.config import DealWorkerConfig

logger = get_logger("deal_worker")

# Initialize configuration
config = DealWorkerConfig()

# Define tasks module path
tasks_module = "src.workers.deal_worker.tasks"

# Create Celery app
app = create_celery_app("deal_worker", config, tasks_module)

# Add worker-specific configuration
app.conf.update(
    worker_log_format="[%(asctime)s: %(levelname)s/%(processName)s] %(message)s",
    worker_task_log_format="[%(asctime)s: %(levelname)s/%(processName)s][%(task_name)s(%(task_id)s)] %(message)s",
    worker_max_tasks_per_child=50,  # Restart worker after 50 tasks (memory management)
    result_expires=7200,  # Results expire after 2 hours
)

WORKER_COMPONENT = resolve_celery_component(app.main)


def _is_current_worker_signal(sender) -> bool:
    return getattr(getattr(sender, "app", None), "main", None) == app.main


# Signal Handlers
@worker_ready.connect
def worker_ready_handler(sender=None, **kwargs):
    """Initialize worker resources when worker is ready."""
    if not _is_current_worker_signal(sender):
        return

    logger.info(
        "Deal worker starting",
        extra={"event": "worker_starting", "worker_component": WORKER_COMPONENT},
    )
    initialize_redis_pool()
    logger.info(
        "Deal worker ready",
        extra={"event": "worker_ready", "worker_component": WORKER_COMPONENT},
    )


@worker_shutdown.connect
def worker_shutdown_handler(sender=None, **kwargs):
    """Clean up worker resources on shutdown."""
    if not _is_current_worker_signal(sender):
        return

    logger.info(
        "Deal worker shutting down",
        extra={"event": "worker_shutdown", "worker_component": WORKER_COMPONENT},
    )
    try:
        close_redis_pool()
        logger.info(
            "Redis connection pool closed",
            extra={"event": "redis_pool_closed", "worker_component": WORKER_COMPONENT},
        )
    except Exception:
        logger.exception(
            "Failed to close Redis connection pool",
            extra={"event": "redis_pool_close_failed", "worker_component": WORKER_COMPONENT},
        )


# Task modules are auto-discovered via the tasks_module path passed to create_celery_app.
# Do NOT explicitly import task modules here - it causes circular imports since tasks
# import `app` from this module. Celery's autodiscovery handles registration.

if __name__ == "__main__":
    app.start()
