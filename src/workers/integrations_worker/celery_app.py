from celery.signals import worker_ready, worker_shutdown
from dotenv import load_dotenv

# Load environment variables first
load_dotenv(override=True)

from src.infra.utils.logger import configure_logging, resolve_celery_component

configure_logging(component=resolve_celery_component("integrations_worker"))

# Redis connection pool management
from src.infra.messaging.redis_client import close_redis_pool, initialize_redis_pool
from src.infra.utils.logger import get_logger
from src.workers.base.celery_base import create_celery_app
from src.workers.integrations_worker.config import IntegrationsWorkerConfig

logger = get_logger("integrations_worker")

# Initialize the configuration for the Integrations Worker
config = IntegrationsWorkerConfig()

# Define the tasks module for auto-discovery
tasks_module = "src.workers.integrations_worker.tasks"

# Create the Celery app for the Integrations Worker
app = create_celery_app("integrations_worker", config, tasks_module)

# Add debug configuration
app.conf.update(
    worker_log_format="[%(asctime)s: %(levelname)s/%(processName)s] %(message)s",
    worker_task_log_format="[%(asctime)s: %(levelname)s/%(processName)s][%(task_name)s(%(task_id)s)] %(message)s",
)


# Add signal handlers for debugging and resource management
@worker_ready.connect
def worker_ready_handler(sender=None, **kwargs):
    """Initialize worker resources when worker is ready."""
    logger.info("Integrations worker starting. Initializing Redis connection pool...")
    initialize_redis_pool()
    logger.info("Integrations worker is ready. Redis connection pool initialized.")


@worker_shutdown.connect
def worker_shutdown_handler(sender=None, **kwargs):
    """Clean up worker resources on shutdown."""
    logger.info("Integrations worker shutting down. Closing Redis connection pool.")
    try:
        close_redis_pool()
        logger.info("Redis connection pool closed successfully")
    except Exception as e:
        logger.error(f"Error closing Redis connection pool: {e}")


# Explicitly import task modules to ensure registration
# Three-tier architecture: calendar, email, and messaging sync tasks
# CRM sync (HubSpot, Salesforce) deprecated — moved to deprecated/workers/
from .tasks import (
    calendar,  # noqa: F401, E402 - three-tier calendar sync
    email,  # noqa: F401, E402 - three-tier email sync
    messaging,  # noqa: F401, E402 - three-tier messaging sync
    token_refresh,  # noqa: F401, E402
)

# Future imports (Step 5):
# from .tasks import integration_sync  # noqa: F401, E402
