"""Celery application bootstrap for the integrations worker.

This module is imported by Celery before task discovery. Keep environment and
logging setup ahead of config creation so local overrides and worker-scoped logs
apply consistently to every registered integrations task.
"""

from celery.signals import worker_ready, worker_shutdown
from dotenv import load_dotenv

# Load .env before importing config-backed modules so local overrides are visible.
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

app = create_celery_app("integrations_worker", config, tasks_module)

WORKER_COMPONENT = resolve_celery_component(app.main)


def _is_current_worker_signal(sender) -> bool:
    return getattr(getattr(sender, "app", None), "main", None) == app.main


@worker_ready.connect
def worker_ready_handler(sender=None, **kwargs):
    """Initialize worker resources when worker is ready."""
    if not _is_current_worker_signal(sender):
        return

    logger.info(
        "Integrations worker starting",
        extra={"event": "worker_starting", "worker_component": WORKER_COMPONENT},
    )
    initialize_redis_pool()
    logger.info(
        "Integrations worker ready",
        extra={"event": "worker_ready", "worker_component": WORKER_COMPONENT},
    )


@worker_shutdown.connect
def worker_shutdown_handler(sender=None, **kwargs):
    """Clean up worker resources on shutdown."""
    if not _is_current_worker_signal(sender):
        return

    logger.info(
        "Integrations worker shutting down",
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


# Explicitly import active task modules to ensure registration.
# Calendar/email expose only single-integration event/post-connect tasks here.
from .tasks import (
    calendar,  # noqa: F401, E402 - calendar single-integration sync
    crm,  # noqa: F401, E402 - three-tier CRM schema sync (HubSpot)
    email,  # noqa: F401, E402 - email single-integration sync
    messaging,  # noqa: F401, E402 - three-tier messaging sync
    token_refresh,  # noqa: F401, E402
)

# Future imports (Step 5):
# from .tasks import integration_sync  # noqa: F401, E402
