import os

# CRITICAL: Set DB pool limits BEFORE any other imports
os.environ["MAX_TENANT_ENGINES"] = "1"
os.environ["TENANT_POOL_MULTIPLIER"] = "1.0"

import contextlib

from celery.signals import (
    worker_ready,
    worker_shutdown,
)
from dotenv import load_dotenv

from src.infra.messaging.redis_client import close_redis_pool, initialize_redis_pool
from src.infra.utils.logger import get_logger
from src.workers.base.celery_base import create_celery_app
from src.workers.deal_worker.config import DealWorkerConfig

logger = get_logger("deal_worker")

# Load environment variables
load_dotenv(override=True)

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

# Optional: Register DB pool monitor
with contextlib.suppress(Exception):
    import scripts.monitoring.celery_integration  # noqa: F401


# Signal Handlers
@worker_ready.connect
def worker_ready_handler(sender=None, **kwargs):
    """Initialize worker resources when worker is ready."""
    logger.info("Deal worker starting. Initializing Redis connection pool...")
    initialize_redis_pool()
    logger.info("Deal worker is ready. Redis connection pool initialized.")


@worker_shutdown.connect
def worker_shutdown_handler(sender=None, **kwargs):
    """Clean up worker resources on shutdown."""
    logger.info("Deal worker shutting down. Closing Redis connection pool.")
    try:
        close_redis_pool()
        logger.info("Redis connection pool closed successfully")
    except Exception as e:
        logger.error(f"Error closing Redis connection pool: {e}")


# Task modules are auto-discovered via the tasks_module path passed to create_celery_app.
# Do NOT explicitly import task modules here - it causes circular imports since tasks
# import `app` from this module. Celery's autodiscovery handles registration.

if __name__ == "__main__":
    app.start()
