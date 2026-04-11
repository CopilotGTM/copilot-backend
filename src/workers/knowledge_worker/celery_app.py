from dotenv import load_dotenv

# Load environment variables first
load_dotenv(override=True)

# Debug: Check if environment variables are loaded
import os

# Disable HuggingFace symlinks for Windows compatibility
# Windows doesn't support symlinks without admin privileges or Developer Mode
# Setting this makes HF use file copies instead of symlinks (uses more disk space but works)
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS"] = "1"

from celery.signals import (
    worker_ready,
    worker_shutdown,
)

# Redis connection pool management
from src.infra.messaging.redis_client import close_redis_pool, initialize_redis_pool
from src.infra.utils.logger import configure_logging, get_logger, resolve_celery_component
from src.workers.base.celery_base import create_celery_app
from src.workers.knowledge_worker.config import KnowledgeWorkerConfig

configure_logging(component=resolve_celery_component("knowledge_worker"))

logger = get_logger("knowledge_worker")

# Initialize the configuration for the Knowledge Worker
config = KnowledgeWorkerConfig()

# Define the tasks module for auto-discovery
tasks_module = "src.workers.knowledge_worker.tasks"

# Create the Celery app for the Knowledge Worker
app = create_celery_app("knowledge_worker", config, tasks_module)

# Add debug configuration and knowledge-worker specific settings
app.conf.update(
    worker_log_format="[%(asctime)s: %(levelname)s/%(processName)s] %(message)s",
    worker_task_log_format="[%(asctime)s: %(levelname)s/%(processName)s][%(task_name)s(%(task_id)s)] %(message)s",
    # Additional configuration for handling long-running tasks
    worker_pool_restarts=True,  # Allow worker pool to be restarted if needed
    worker_max_tasks_per_child=50,  # Restart worker process after 50 tasks to prevent memory leaks
    # Result backend settings for long-running tasks
    result_expires=7200,  # Results expire after 2 hours
    result_persistent=True,  # Persist results to backend
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
        "Knowledge worker starting",
        extra={"event": "worker_starting", "worker_component": WORKER_COMPONENT},
    )
    initialize_redis_pool()
    logger.info(
        "Knowledge worker ready",
        extra={"event": "worker_ready", "worker_component": WORKER_COMPONENT},
    )


@worker_shutdown.connect
def worker_shutdown_handler(sender=None, **kwargs):
    """Clean up worker resources on shutdown."""
    if not _is_current_worker_signal(sender):
        return

    logger.info(
        "Knowledge worker shutting down",
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
