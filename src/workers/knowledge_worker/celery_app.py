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
from src.infra.utils.logger import get_logger
from src.workers.base.celery_base import create_celery_app
from src.workers.knowledge_worker.config import KnowledgeWorkerConfig

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


# Add signal handlers for debugging and resource management
@worker_ready.connect
def worker_ready_handler(sender=None, **kwargs):
    """Initialize worker resources when worker is ready."""
    logger.info("Knowledge worker starting. Initializing Redis connection pool...")
    initialize_redis_pool()
    logger.info("Knowledge worker is ready. Redis connection pool initialized.")


@worker_shutdown.connect
def worker_shutdown_handler(sender=None, **kwargs):
    """Clean up worker resources on shutdown."""
    logger.info("Knowledge worker shutting down. Closing Redis connection pool.")
    try:
        close_redis_pool()
        logger.info("Redis connection pool closed successfully")
    except Exception as e:
        logger.error(f"Error closing Redis connection pool: {e}")


# Explicitly import task modules to ensure registration
