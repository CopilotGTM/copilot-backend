import os

from dotenv import load_dotenv

# Skip Alembic's fileConfig to avoid logging conflicts in Celery worker
os.environ["CELERY_SKIP_ALEMBIC_FILECONFIG"] = "1"

from celery.signals import worker_ready, worker_shutdown

# Load environment variables first
load_dotenv(override=True)

from src.infra.utils.logger import configure_logging, resolve_celery_component

configure_logging(component=resolve_celery_component("organization_worker"))

# Redis connection pool management
from src.infra.messaging.redis_client import close_redis_pool, initialize_redis_pool
from src.infra.utils.logger import get_logger
from src.workers.base.celery_base import create_celery_app
from src.workers.organization_worker.config import OrganizationWorkerConfig

logger = get_logger("organization_worker")

# Initialize the configuration for the Organization Worker
config = OrganizationWorkerConfig()

# Define the tasks module for auto-discovery
tasks_module = "src.workers.organization_worker.tasks"

# Create the Celery app for the Organization Worker
app = create_celery_app("organization_worker", config, tasks_module)

# Add debug configuration
app.conf.update(
    worker_log_format="[%(asctime)s: %(levelname)s/%(processName)s] %(message)s",
    worker_task_log_format="[%(asctime)s: %(levelname)s/%(processName)s][%(task_name)s(%(task_id)s)] %(message)s",
)

WORKER_COMPONENT = resolve_celery_component(app.main)


def _is_current_worker_signal(sender) -> bool:
    return getattr(getattr(sender, "app", None), "main", None) == app.main


# Add signal handlers for debugging and resource management
@worker_ready.connect
def worker_ready_handler(sender=None, **kwargs):
    """Initialize worker resources when worker is ready."""
    if not _is_current_worker_signal(sender):
        return

    logger.info(
        "Organization worker starting",
        extra={"event": "worker_starting", "worker_component": WORKER_COMPONENT},
    )
    initialize_redis_pool()
    logger.info(
        "Organization worker ready",
        extra={"event": "worker_ready", "worker_component": WORKER_COMPONENT},
    )


@worker_shutdown.connect
def worker_shutdown_handler(sender=None, **kwargs):
    """Clean up worker resources on shutdown."""
    if not _is_current_worker_signal(sender):
        return

    logger.info(
        "Organization worker shutting down",
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


# Explicitly import task modules to ensure registration
