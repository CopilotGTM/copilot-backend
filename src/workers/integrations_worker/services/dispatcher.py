from __future__ import annotations

from contextlib import suppress
from typing import Any
from uuid import UUID

from sqlalchemy import select
from src.api.dependencies.organisation_dependencies import acquire_org_async_session
from src.domain.models.organisation_tables.sync import KnowledgeIntegration, SyncStatus
from src.infra.config.nango_provider_mapping import to_nango_connect_integration_key
from src.infra.utils.logger import get_logger
from src.service.integrations import NangoClient
from src.service.integrations.crm_sync_service import CRMSyncService

from ..adapters.calendar_adapter import upsert_calendar
from ..adapters.crm_adapter import sync_crm_data
from ..models.sync_models import SyncItemResult, SyncJobRequest, SyncJobResult
from ..providers.registry import get_fetcher
from ..utils.dedupe import acquire_sync_lock

logger = get_logger(__name__)


async def execute_sync(request: SyncJobRequest) -> dict[str, Any]:
    """
    Execute a sync job for a specific integration.

    Args:
        request: Sync job request containing organisation, integration, and data type info

    Returns:
        Dictionary with sync results including status and items synced
    """
    org_id = UUID(request.organisation_id)
    int_id = UUID(request.integration_id)

    logger.info(
        "Starting sync job: org=%s integration=%s category=%s provider=%s data_types=%s",
        request.organisation_id,
        request.integration_id,
        request.category,
        request.provider,
        request.data_types,
    )

    crm_service: CRMSyncService | None = None

    try:
        # acquire_org_async_session returns an async generator, not a context manager
        session_gen = acquire_org_async_session(org_id)
        session = await anext(session_gen)

        try:
            # Fetch integration record
            res = await session.execute(
                select(KnowledgeIntegration).where(KnowledgeIntegration.integration_id == int_id)
            )
            integration = res.scalars().first()

            if not integration:
                logger.warning("Integration not found: %s", request.integration_id)
                return SyncJobResult(
                    status="not_found", extra={"error": "Integration not found"}
                ).to_dict()

            logger.info(
                "Integration found: connection_id=%s last_synced=%s",
                integration.connection_id,
                integration.last_synced_at,
            )

            nango = NangoClient()
            result = SyncJobResult(status="success")

            # Process each data type
            provider_key = request.provider_config_key or to_nango_connect_integration_key(
                integration.integration_type
            )

            for dt in request.data_types:
                logger.info("Processing data type: %s", dt)

                # Check sync lock
                if not acquire_sync_lock(request.integration_id, dt):
                    logger.warning(
                        "Sync locked for integration=%s data_type=%s", request.integration_id, dt
                    )
                    result.items[dt] = SyncItemResult(status="skipped", reason="locked")
                    continue

                # Get appropriate fetcher
                fetcher = get_fetcher(request.category, request.provider, dt)
                if not fetcher:
                    logger.warning(
                        "No fetcher found for category=%s provider=%s data_type=%s",
                        request.category,
                        request.provider,
                        dt,
                    )
                    result.items[dt] = SyncItemResult(status="ignored", reason="no_fetcher")
                    continue

                try:
                    # Fetch records from provider
                    logger.info("Fetching records from provider for data_type=%s", dt)
                    logger.info(
                        "Fetch params: connection_id=%s, is_initial=%s, last_synced=%s",
                        integration.connection_id,
                        not request.incremental,
                        integration.last_synced_at,
                    )
                    records = await fetcher(
                        nango=nango,
                        connection_id=integration.connection_id,
                        is_initial=not request.incremental,
                        last_synced_at=integration.last_synced_at,
                        data_type=dt,
                        provider_config_key=provider_key,
                    )

                    logger.info("Fetched %d records for data_type=%s", len(records), dt)
                    if records:
                        logger.debug(
                            "Sample record keys: %s", list(records[0].keys()) if records else []
                        )

                    # Process records based on category
                    if request.category == "calendar":
                        count = await upsert_calendar(
                            session=session,
                            organisation_id=org_id,
                            integration=integration,
                            provider=request.provider,
                            events=records,
                        )
                        logger.info(
                            "Calendar sync completed for data_type=%s: %d events processed",
                            dt,
                            count,
                        )
                        result.items[dt] = SyncItemResult(status="success", records_synced=count)
                    elif request.category == "crm":
                        if crm_service is None:
                            crm_service = CRMSyncService(session, org_id)
                        metrics = await sync_crm_data(
                            service=crm_service,
                            integration=integration,
                            data_type=dt,
                            records=records,
                            provider=request.provider,
                        )
                        records_synced = metrics.get("records_synced")
                        if records_synced is None:
                            records_synced = metrics.get("deals") or len(records)
                        logger.info(
                            "CRM sync completed for data_type=%s: %s",
                            dt,
                            metrics,
                        )
                        result.items[dt] = SyncItemResult(
                            status="success", records_synced=records_synced
                        )
                        result.extra.setdefault("crm", {})[dt] = metrics
                    else:
                        logger.warning("Unsupported category: %s", request.category)
                        result.items[dt] = SyncItemResult(
                            status="ignored", reason="unsupported_category"
                        )

                except Exception as e:
                    logger.error(
                        "Error syncing data_type=%s: %s",
                        dt,
                        str(e),
                        exc_info=True,
                    )
                    result.items[dt] = SyncItemResult(
                        status="failed",
                        reason="sync_error",
                        error=str(e),
                    )

            if crm_service:
                await _finalize_crm_service(crm_service, result)

            if any(item.status == "failed" for item in result.items.values()):
                result.status = "failed"
            elif any(
                item.status not in {"success", "ignored", "skipped"}
                for item in result.items.values()
            ):
                result.status = "partial"
            elif result.items and all(item.status == "success" for item in result.items.values()):
                result.status = "success"

            # Log summary
            success_count = sum(1 for item in result.items.values() if item.status == "success")
            total_records = sum(
                item.records_synced or 0
                for item in result.items.values()
                if item.status == "success"
            )
            logger.info(
                "Sync job completed: org=%s integration=%s success=%d/%d total_records=%d",
                request.organisation_id,
                request.integration_id,
                success_count,
                len(request.data_types),
                total_records,
            )

            return result.to_dict()

        finally:
            # Close the async session generator
            with suppress(StopAsyncIteration):
                await anext(session_gen)

    except Exception as e:
        # If CRM service was initialized before failure, attempt to mark failed
        if crm_service:
            with suppress(Exception):
                await crm_service.finalize_sync(SyncStatus.FAILED, str(e))
        logger.error(
            "Sync job failed: org=%s integration=%s error=%s",
            request.organisation_id,
            request.integration_id,
            str(e),
            exc_info=True,
        )
        return SyncJobResult(
            status="failed", extra={"error": str(e), "error_type": type(e).__name__}
        ).to_dict()


async def _finalize_crm_service(service: CRMSyncService, result: SyncJobResult) -> None:
    statuses = list(result.items.values())
    failed = next((item for item in statuses if item.status == "failed"), None)
    if failed:
        await service.finalize_sync(SyncStatus.FAILED, failed.error or failed.reason)
        return

    partial = any(item.status not in {"success", "ignored", "skipped"} for item in statuses)
    if partial:
        await service.finalize_sync(SyncStatus.PARTIAL, None)
        return

    await service.finalize_sync(SyncStatus.SUCCESS, None)
