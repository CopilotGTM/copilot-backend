import os

# Set minimal DB pool settings for worker process
os.environ["MAX_TENANT_ENGINES"] = "1"
os.environ["TENANT_POOL_MULTIPLIER"] = "1.0"

from dotenv import load_dotenv
from src.workers.base.celery_base import create_celery_app
from src.workers.content_worker.config import ContentWorkerConfig
from celery.signals import worker_ready, worker_shutdown

# Redis connection pool management
from src.infra.messaging.redis_client import close_redis_pool
from src.infra.utils.logger import get_logger

# Load environment variables first
load_dotenv(override=True)

logger = get_logger("content_worker")

# Initialize the configuration for the Content Worker
config = ContentWorkerConfig()

# Define the tasks module for auto-discovery
tasks_module = "src.workers.content_worker.tasks"

# Create the Celery app for the Content Worker
app = create_celery_app("content_worker", config, tasks_module)


# Add signal handlers for resource management
@worker_ready.connect
def worker_ready_handler(sender=None, **kwargs):
    """Initialize worker resources when worker is ready."""
    logger.info("Content worker is ready. Redis connection pool initialized.")


@worker_shutdown.connect
def worker_shutdown_handler(sender=None, **kwargs):
    """Clean up worker resources on shutdown."""
    logger.info("Content worker shutting down. Closing Redis connection pool.")
    try:
        close_redis_pool()
        logger.info("Redis connection pool closed successfully")
    except Exception as e:
        logger.error(f"Error closing Redis connection pool: {e}")


# Register DB pool monitor for Celery workers (signals-based)
try:
    import scripts.monitoring.celery_integration  # noqa: F401
except Exception:
    pass
