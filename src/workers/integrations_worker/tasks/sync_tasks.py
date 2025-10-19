"""
Celery task for integration data synchronization (webhook-driven).

This module contains the main sync task that fetches data from external providers
(Google Calendar, HubSpot, etc.) via Nango proxy and stores it in our database.

Task Routing:
- Webhook reception happens in FastAPI (nango_webhook_service.py)
- Webhook handler resolves org/integration and enqueues this task
- This task delegates to dispatcher for actual sync logic

Architecture:
- ONE task per sync operation (handles multiple data types)
- Provider-agnostic design via dispatcher pattern
- Comprehensive error handling and logging
"""

from __future__ import annotations

import asyncio
from typing import Any

from celery import Task

from src.infra.utils.logger import get_logger
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.models.sync_models import SyncJobRequest
from src.workers.integrations_worker.services.dispatcher import execute_sync

logger = get_logger(__name__)


@app.task(
    name="integrations.sync_integration_data",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    reject_on_worker_lost=True,
)
def sync_integration_data_task(
    self: Task,
    *,
    organisation_id: str,
    integration_id: str,
    category: str,
    provider: str,
    data_types: list[str],
    incremental: bool = True,
    sync_name: str | None = None,
) -> dict[str, Any]:
    """
    Sync integration data from external provider.

    This task orchestrates the data synchronization process:
    1. Validates integration exists
    2. Fetches data from provider via Nango proxy
    3. Transforms and stores data in database
    4. Updates integration metadata

    Args:
        organisation_id: Organisation UUID
        integration_id: Integration UUID
        category: Integration category (e.g., 'calendar', 'crm')
        provider: Provider name (e.g., 'google', 'microsoft')
        data_types: List of data types to sync (e.g., ['events', 'calendars'])
        incremental: Whether to do incremental sync (default: True)
        sync_name: Optional sync name from webhook

    Returns:
        Dictionary with sync results
    """
    logger.info(
        "Task received: integrations.sync_integration_data task_id=%s org=%s integration=%s",
        self.request.id,
        organisation_id,
        integration_id,
    )

    async def _run() -> dict[str, Any]:
        try:
            req = SyncJobRequest(
                organisation_id=organisation_id,
                integration_id=integration_id,
                category=category,
                provider=provider,
                data_types=data_types,
                incremental=incremental,
                sync_name=sync_name,
            )
            result = await execute_sync(req)

            logger.info(
                "Task completed successfully: task_id=%s org=%s integration=%s status=%s",
                self.request.id,
                organisation_id,
                integration_id,
                result.get("status"),
            )

            return result

        except Exception as e:
            logger.error(
                "Task execution error: task_id=%s org=%s integration=%s error=%s",
                self.request.id,
                organisation_id,
                integration_id,
                str(e),
                exc_info=True,
            )
            # Return error dict instead of raising to avoid retry on unrecoverable errors
            return {
                "status": "failed",
                "items": {},
                "extra": {
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "task_id": self.request.id,
                },
            }

    try:
        return asyncio.run(_run())
    except Exception as e:
        logger.error(
            "Task asyncio error: task_id=%s org=%s integration=%s error=%s",
            self.request.id,
            organisation_id,
            integration_id,
            str(e),
            exc_info=True,
        )
        return {
            "status": "failed",
            "items": {},
            "extra": {
                "error": str(e),
                "error_type": type(e).__name__,
                "task_id": self.request.id,
            },
        }
