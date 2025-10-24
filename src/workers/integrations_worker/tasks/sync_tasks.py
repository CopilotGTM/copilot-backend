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

from typing import Any
from uuid import UUID

from celery import Task
from celery.exceptions import MaxRetriesExceededError

from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.models.sync_models import SyncJobRequest
from src.workers.integrations_worker.services.dispatcher import execute_sync

logger = get_logger(__name__)

NON_RETRYABLE_ITEM_REASONS = {"no_fetcher", "integration_missing", "unsupported_category"}
NON_RETRYABLE_ERRORS = {"integration_not_found"}


class RetryableSyncError(RuntimeError):
    """Raised when a sync operation should be retried by Celery."""

    def __init__(self, result: dict[str, Any]):
        super().__init__("Retryable sync failure")
        self.result = result


def _is_non_retryable_failure(result: dict[str, Any]) -> bool:
    """Determine if a failure should not be retried."""
    extra = result.get("extra") or {}
    if extra.get("error") in NON_RETRYABLE_ERRORS:
        return True

    for item in (result.get("items") or {}).values():
        if not isinstance(item, dict):
            continue
        reason = item.get("reason")
        if reason in NON_RETRYABLE_ITEM_REASONS:
            return True

    return False


def _summarize_items(result: dict[str, Any]) -> dict[str, Any]:
    """Return a lightweight summary of item results for logging."""
    summary: dict[str, Any] = {}
    for key, item in (result.get("items") or {}).items():
        if not isinstance(item, dict):
            continue
        summary[key] = {
            field: item.get(field)
            for field in ("status", "reason", "error", "records_synced")
            if item.get(field) is not None
        }
    return summary


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
    provider_config_key: str | None = None,
    sync_name: str | None = None,
    sync_type: str | None = None,
    sync_variant: str | None = None,
    incremental: bool = True,
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
        provider_config_key: Provider config key from Nango webhook (if available)
        sync_name: Sync name from Nango webhook (e.g., 'deals', 'contacts')
        sync_type: Sync type from Nango webhook (e.g., 'INITIAL', 'INCREMENTAL')
        sync_variant: Sync variant from Nango webhook (e.g., 'base', 'custom')
        incremental: Whether to do incremental sync (default: True)
        integration_details: Integration details from Nango webhook (if available)

    Returns:
        Dictionary with sync results
    """
    try:
        logger.info(
            "Sync task parameters: org=%s integration=%s category=%s provider=%s sync_name=%s sync_type=%s",
            organisation_id,
            integration_id,
            category,
            provider,
            sync_name,
            sync_type,
        )

        async def _run() -> dict[str, Any]:
            try:
                req = SyncJobRequest(
                    organisation_id=UUID(organisation_id),
                    integration_id=UUID(integration_id),
                    category=category,
                    provider=provider,
                    provider_config_key=provider_config_key,
                    incremental=incremental,
                    sync_name=sync_name,
                    sync_type=sync_type,
                    sync_variant=sync_variant,
                )
                result = await execute_sync(req)
            except Exception as exc:
                logger.error(
                    "Dispatcher execution error: task_id=%s org=%s integration=%s error=%s",
                    self.request.id,
                    organisation_id,
                    integration_id,
                    str(exc),
                    exc_info=True,
                )
                raise

            status = result.get("status")
            if status == "success":
                logger.info(
                    "Task completed successfully: task_id=%s org=%s integration=%s status=%s",
                    self.request.id,
                    organisation_id,
                    integration_id,
                    status,
                )
                return result

            if status == "partial":
                logger.warning(
                    "Task completed with partial success: task_id=%s org=%s integration=%s summary=%s",
                    self.request.id,
                    organisation_id,
                    integration_id,
                    _summarize_items(result),
                )
                return result

            logger.error(
                "Task reported failure: task_id=%s org=%s integration=%s summary=%s extra=%s",
                self.request.id,
                organisation_id,
                integration_id,
                _summarize_items(result),
                result.get("extra"),
            )
            if _is_non_retryable_failure(result):
                logger.warning(
                    "Failure deemed non-retryable; returning result without retry: task_id=%s",
                    self.request.id,
                )
                return result

            raise RetryableSyncError(result)

        try:
            return run_async(_run())
        except RetryableSyncError as retryable:
            summary = _summarize_items(retryable.result)
            logger.error(
                "Retryable sync failure: task_id=%s org=%s integration=%s attempt=%d/%s summary=%s extra=%s",
                self.request.id,
                organisation_id,
                integration_id,
                self.request.retries + 1,
                getattr(self, "max_retries", "unbounded"),
                summary,
                retryable.result.get("extra"),
            )
            max_retries = getattr(self, "max_retries", None)
            if max_retries is not None and self.request.retries >= max_retries:
                logger.error(
                    "Max retries reached; propagating failure: task_id=%s org=%s integration=%s",
                    self.request.id,
                    organisation_id,
                    integration_id,
                )
                raise
            raise self.retry(exc=retryable) from retryable
        except MaxRetriesExceededError:
            logger.error(
                "Celery reported max retries exceeded automatically: task_id=%s org=%s integration=%s",
                self.request.id,
                organisation_id,
                integration_id,
                exc_info=True,
            )
            raise
        except Exception as exc:
            logger.error(
                "Task execution error: task_id=%s org=%s integration=%s error=%s",
                self.request.id,
                organisation_id,
                integration_id,
                str(exc),
                exc_info=True,
            )
            max_retries = getattr(self, "max_retries", None)
            if max_retries is not None and self.request.retries >= max_retries:
                raise
            raise self.retry(exc=exc) from exc

    except MaxRetriesExceededError:
        logger.error(
            "Max retries exceeded for task_id=%s org=%s integration=%s; giving up.",
            self.request.id,
            organisation_id,
            integration_id,
            exc_info=True,
        )
        raise
