import os

from dotenv import load_dotenv

# Set minimal DB pool settings for worker process
os.environ["MAX_TENANT_ENGINES"] = "1"
os.environ["TENANT_POOL_MULTIPLIER"] = "1.0"

from celery.signals import worker_ready, worker_shutdown

# Redis connection pool management
from src.infra.messaging.redis_client import close_redis_pool, initialize_redis_pool
from src.infra.utils.logger import get_logger
from src.workers.base.celery_base import create_celery_app
from src.workers.integrations_worker.config import IntegrationsWorkerConfig

logger = get_logger("integrations_worker")

# Load environment variables first
load_dotenv(override=True)

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

# Register DB pool monitor for Celery workers (signals-based)
try:
    import scripts.monitoring.celery_integration  # noqa: F401
except Exception:
    pass


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
# Email sync tasks (three-tier architecture)
from .tasks import (
    deal_channel_messages_tasks,  # noqa: F401, E402
    email,  # noqa: F401, E402
    sync_tasks,  # noqa: F401, E402
    teams_sync_tasks,  # noqa: F401, E402
    token_refresh,  # noqa: F401, E402
)

# Future imports (Step 5):
# from .tasks import integration_sync  # noqa: F401, E402
