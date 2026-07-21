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

# Keep task registration explicit so importing the package does not pull in the
# full chat worker graph during lightweight import checks.
tasks_module = [
    "src.workers.chat_worker.tasks.agent_run_tasks",
    "src.workers.chat_worker.tasks.bot_message_processing",
    "src.workers.chat_worker.tasks.message_processing",
    "src.workers.chat_worker.tasks.post_upload",
    "src.workers.chat_worker.tasks.reminder_tasks",
    "src.workers.chat_worker.tasks.stale_job_cleanup_tasks",
    "src.workers.chat_worker.tasks.test_tasks",
    "src.workers.chat_worker.tasks.voice_realtime",
]

# Create the Celery app for the Chat Worker
app = create_celery_app("chat_worker", config, tasks_module)

# Add debug configuration
app.conf.update(
    worker_log_format="[%(asctime)s: %(levelname)s/%(processName)s] %(message)s",
    worker_task_log_format="[%(asctime)s: %(levelname)s/%(processName)s][%(task_name)s(%(task_id)s)] %(message)s",
)

WORKER_COMPONENT = resolve_celery_component(app.main)


def _is_current_worker_signal(sender) -> bool:
    return getattr(getattr(sender, "app", None), "main", None) == app.main


# Log broker connection details for debugging (mask password)
broker_url_masked = config.broker_url
if config.redis_password:
    broker_url_masked = config.broker_url.replace(config.redis_password, "****")
logger.debug(f"Chat worker broker URL: {broker_url_masked}")
logger.debug(
    f"Chat worker backend URL: {config.backend_url.replace(config.redis_password, '****') if config.redis_password else config.backend_url}"
)
logger.debug(f"Chat worker SSL config: {config.ssl_config}")
logger.debug(f"Chat worker queues: {[q.name for q in config.task_queues]}")
logger.debug(f"Chat worker default queue: {config.task_default_queue}")


# Add signal handlers for debugging and resource management
@worker_ready.connect
def worker_ready_handler(sender=None, **kwargs):
    """Initialize worker resources when worker is ready."""
    if not _is_current_worker_signal(sender):
        return

    logger.info(
        "Chat worker starting",
        extra={"event": "worker_starting", "worker_component": WORKER_COMPONENT},
    )
    initialize_redis_pool()
    logger.info(
        "Chat worker ready",
        extra={"event": "worker_ready", "worker_component": WORKER_COMPONENT},
    )


@worker_shutdown.connect
def worker_shutdown_handler(sender=None, **kwargs):
    """Clean up worker resources on shutdown."""
    if not _is_current_worker_signal(sender):
        return

    logger.info(
        "Chat worker shutting down",
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
