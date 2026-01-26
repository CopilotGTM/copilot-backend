"""Stale database connection cleanup tasks.

This module provides a Tier 1 scheduled task that runs every 10 minutes
to clean up stale database connections older than 10 minutes.

This is a global task (not per-organization) that maintains database health
by terminating connections that may be stuck or leaked.

Architecture:
- Tier 1 only (no organization loop needed)
- Connects directly to master/maintenance database
- Terminates connections idle > 10 minutes
- Runs every 10 minutes via Celery beat
"""

from __future__ import annotations

from typing import Any

from src.infra.utils.logger import get_logger
from src.workers.knowledge_worker.celery_app import app
from src.workers.knowledge_worker.services.stale_connection_cleanup_service import (
    StaleConnectionCleanupService,
)

logger = get_logger("stale_connection_cleanup_tasks")

# Configuration
STALE_THRESHOLD_MINUTES = 10


@app.task(
    name="knowledge.cleanup_stale_connections",
    bind=False,
    max_retries=0,  # Don't retry - will run again in 10 minutes
    time_limit=120,  # 2 minute hard limit
    soft_time_limit=90,  # 1.5 minute soft limit
)
def cleanup_stale_connections_task() -> dict[str, Any]:
    """
    Tier 1 task: Cleanup stale database connections.

    This task runs globally (not per-organization) and:
    1. Connects to the database (main or maintenance)
    2. Finds connections older than 10 minutes
    3. Terminates them (except superuser connections)

    Returns:
        dict with cleanup statistics
    """
    logger.info(
        "Starting stale connection cleanup (threshold: %d minutes)",
        STALE_THRESHOLD_MINUTES,
    )

    try:
        service = StaleConnectionCleanupService(threshold_minutes=STALE_THRESHOLD_MINUTES)
        result = service.cleanup_stale_connections()

        if result.error:
            logger.error("Connection cleanup failed: %s", result.error)
            return {
                "success": False,
                "error": result.error,
            }

        logger.info(
            "Connection cleanup completed: found=%d, stale=%d, terminated=%d, remaining=%d",
            result.total_connections_found,
            result.stale_connections_found,
            result.connections_terminated,
            result.remaining_connections,
        )

        return {
            "success": True,
            "total_connections_found": result.total_connections_found,
            "stale_connections_found": result.stale_connections_found,
            "connections_terminated": result.connections_terminated,
            "remaining_connections": result.remaining_connections,
            "terminated_details": result.connections_details,
        }

    except Exception as e:
        logger.exception("Unexpected error in connection cleanup task: %s", e)
        return {
            "success": False,
            "error": str(e),
        }
