import os

from dotenv import load_dotenv

# Set minimal DB pool settings for worker process
os.environ["MAX_TENANT_ENGINES"] = "1"
os.environ["TENANT_POOL_MULTIPLIER"] = "1.0"

from celery.signals import (
    task_failure,
    task_postrun,
    task_prerun,
    task_received,
    worker_ready,
    worker_shutdown,
)

# Redis connection pool management
from src.infra.messaging.redis_client import close_redis_pool, initialize_redis_pool
from src.infra.utils.logger import get_logger
from src.workers.base.celery_base import create_celery_app
from src.workers.organization_worker.config import OrganizationWorkerConfig

logger = get_logger("organization_worker")

# Load environment variables first
load_dotenv(override=True)

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

# Register DB pool monitor for Celery workers (signals-based)
try:
    import scripts.monitoring.celery_integration  # noqa: F401
except Exception:
    pass


# Add signal handlers for debugging and resource management
@worker_ready.connect
def worker_ready_handler(sender=None, **kwargs):
    """Initialize worker resources when worker is ready."""
    logger.info("Organization worker starting. Initializing Redis connection pool...")
    initialize_redis_pool()
    logger.info("Organization worker is ready. Redis connection pool initialized.")


@worker_shutdown.connect
def worker_shutdown_handler(sender=None, **kwargs):
    """Clean up worker resources on shutdown."""
    logger.info("Organization worker shutting down. Closing Redis connection pool.")
    try:
        close_redis_pool()
        logger.info("Redis connection pool closed successfully")
    except Exception as e:
        logger.error(f"Error closing Redis connection pool: {e}")


@task_received.connect
def task_received_handler(sender=None, headers=None, body=None, **kwargs):
    print(f"Task received: {headers}")


@task_prerun.connect
def task_prerun_handler(task_id=None, task=None, args=None, kwargs=None, **kwds):
    print(f"Task starting: {task.name}({task_id}) with args={args} kwargs={kwargs}")


@task_postrun.connect
def task_postrun_handler(
    task_id=None, task=None, args=None, kwargs=None, retval=None, state=None, **kwds
):
    print(f"Task finished: {task.name}({task_id}) state={state}")


@task_failure.connect
def task_failure_handler(task_id=None, exception=None, traceback=None, einfo=None, **kwds):
    print(f"Task failed: {task_id} exception={exception}")


# Explicitly import task modules to ensure registration
