from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.infra.database.provider_async import AsyncSqlAlchemyProvider
from src.infra.utils.logger import get_logger
from src.service.integrations import NangoClient

from ..adapters.calendar_adapter import upsert_calendar
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
    db = AsyncSqlAlchemyProvider()
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

    try:
        async with db.tenant_session(org_id) as session:
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
                    records = await fetcher(
                        nango=nango,
                        connection_id=integration.connection_id,
                        is_initial=not request.incremental,
                        last_synced_at=integration.last_synced_at,
                        data_type=dt,
                    )

                    logger.info("Fetched %d records for data_type=%s", len(records), dt)

                    # Process records based on category
                    if request.category == "calendar":
                        count = await upsert_calendar(
                            session=session,
                            organisation_id=org_id,
                            integration=integration,
                            provider=request.provider,
                            events=records,
                        )
                        logger.info("Upserted %d calendar events for data_type=%s", count, dt)
                        result.items[dt] = SyncItemResult(status="success", records_synced=count)
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

    except Exception as e:
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
