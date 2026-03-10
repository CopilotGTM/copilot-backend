from celery.signals import (
    worker_ready,
    worker_shutdown,
)
from dotenv import load_dotenv

# Load environment variables first
load_dotenv(override=True)

from src.infra.utils.logger import configure_logging, resolve_celery_component

configure_logging(component=resolve_celery_component("chat_worker"))

# Redis connection pool management
from src.infra.messaging.redis_client import close_redis_pool, initialize_redis_pool
from src.infra.utils.logger import get_logger
from src.workers.base.celery_base import create_celery_app
from src.workers.chat_worker.config import ChatWorkerConfig

logger = get_logger("chat_worker")

# Initialize the configuration for the Chat Worker
config = ChatWorkerConfig()

# Define the tasks module for auto-discovery
tasks_module = "src.workers.chat_worker.tasks"

# Create the Celery app for the Chat Worker
app = create_celery_app("chat_worker", config, tasks_module)

# Add debug configuration
app.conf.update(
    worker_log_format="[%(asctime)s: %(levelname)s/%(processName)s] %(message)s",
    worker_task_log_format="[%(asctime)s: %(levelname)s/%(processName)s][%(task_name)s(%(task_id)s)] %(message)s",
)

# Log broker connection details for debugging (mask password)
broker_url_masked = config.broker_url
if config.redis_password:
    broker_url_masked = config.broker_url.replace(config.redis_password, "****")
logger.info(f"Chat worker broker URL: {broker_url_masked}")
logger.info(
    f"Chat worker backend URL: {config.backend_url.replace(config.redis_password, '****') if config.redis_password else config.backend_url}"
)
logger.info(f"Chat worker SSL config: {config.ssl_config}")
logger.info(f"Chat worker queues: {[q.name for q in config.task_queues]}")
logger.info(f"Chat worker default queue: {config.task_default_queue}")


# Add signal handlers for debugging and resource management
@worker_ready.connect
def worker_ready_handler(sender=None, **kwargs):
    """Initialize worker resources when worker is ready."""
    logger.info("Chat worker starting. Initializing Redis connection pool...")
    initialize_redis_pool()
    logger.info("Chat worker is ready. Redis connection pool initialized.")


@worker_shutdown.connect
def worker_shutdown_handler(sender=None, **kwargs):
    """Clean up worker resources on shutdown."""
    logger.info("Chat worker shutting down. Closing Redis connection pool.")
    try:
        close_redis_pool()
        logger.info("Redis connection pool closed successfully")
    except Exception as e:
        logger.error(f"Error closing Redis connection pool: {e}")


# Explicitly import task modules to ensure registration
