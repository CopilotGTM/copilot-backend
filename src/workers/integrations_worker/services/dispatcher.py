from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import acquire_org_async_session
from src.domain.models.organisation_tables.sync import (
    IntegrationType,
    KnowledgeIntegration,
    SyncStatus,
)
from src.infra.config.nango_provider_mapping import to_nango_connect_integration_key
from src.infra.utils.logger import get_logger
from src.service.integrations import NangoClient
from src.service.integrations.crm_sync_service import CRMSyncService

from ..adapters.calendar_adapter import upsert_calendar
from ..adapters.crm_adapter import sync_crm_data
from ..models.sync_models import SyncItemResult, SyncJobRequest, SyncJobResult
from ..providers.registry import get_fetcher
from ..utils.dedupe import acquire_sync_lock, release_sync_lock

logger = get_logger(__name__)


@dataclass(slots=True)
class IntegrationMetadata:
    integration_id: UUID
    connection_id: str
    integration_type: IntegrationType
    last_synced_at: datetime | None


def _to_metadata(integration: KnowledgeIntegration) -> IntegrationMetadata:
    return IntegrationMetadata(
        integration_id=integration.integration_id,
        connection_id=integration.connection_id,
        integration_type=integration.integration_type,
        last_synced_at=integration.last_synced_at,
    )


@asynccontextmanager
async def _integration_session(organisation_id: UUID) -> AsyncIterator[AsyncSession]:
    session_gen = acquire_org_async_session(organisation_id)
    session = await anext(session_gen)
    try:
        yield session
    finally:
        with suppress(StopAsyncIteration):
            await anext(session_gen)


async def _load_integration_metadata(
    organisation_id: UUID,
    integration_id: UUID,
) -> IntegrationMetadata:
    async with _integration_session(organisation_id) as session:
        stmt = select(KnowledgeIntegration).where(
            KnowledgeIntegration.integration_id == integration_id
        )
        res = await session.execute(stmt)
        integration = res.scalars().first()
        if not integration:
            raise ValueError("Integration not found")
        return _to_metadata(integration)


async def _reload_integration(
    session: AsyncSession,
    integration_id: UUID,
) -> KnowledgeIntegration | None:
    stmt = select(KnowledgeIntegration).where(KnowledgeIntegration.integration_id == integration_id)
    res = await session.execute(stmt)
    return res.scalars().first()


async def execute_sync(request: SyncJobRequest) -> dict[str, Any]:
    """
    Execute a sync job for a specific integration.

    Args:
        request: Sync job request containing organisation, integration, and data type info

    Returns:
        Dictionary with sync results including status and items synced
    """

    # Initialize Required client
    nango = NangoClient()

    # Initialize variables
    organisation_id: UUID = UUID(str(request.organisation_id))
    integration_id: UUID = UUID(str(request.integration_id))

    try:
        integration_meta = await _load_integration_metadata(organisation_id, integration_id)
        logger.info(
            "Integration found: connection_id=%s last_synced=%s",
            integration_meta.connection_id,
            integration_meta.last_synced_at,
        )

        # Prepare for sync
        result = SyncJobResult(status="success")

        # Process each data type
        provider_key = request.provider_config_key or to_nango_connect_integration_key(
            integration_meta.integration_type
        )

        for data_type in [request.sync_name]:
            logger.info("Processing data type: %s", data_type)

            # Check sync lock
            lock_acquired = acquire_sync_lock(request.integration_id, data_type)
            if not lock_acquired:
                result.items[data_type] = SyncItemResult(status="skipped", reason="locked")
                continue

            try:
                # Get appropriate fetcher
                fetcher = get_fetcher(request.category, request.provider, data_type)
                logger.info("Fetcher obtained: %s", fetcher.__name__ if fetcher else "None")
                if not fetcher:
                    logger.warning(
                        "No fetcher found for category=%s provider=%s data_type=%s",
                        request.category,
                        request.provider,
                        data_type,
                    )
                    result.items[data_type] = SyncItemResult(status="ignored", reason="no_fetcher")
                    continue

                try:
                    logger.info(
                        "Starting Fetch for Connection ID: %s", integration_meta.connection_id
                    )
                    logger.info("Last Synced At: %s", integration_meta.last_synced_at)
                    logger.info("Is Initial Sync: %s", not request.incremental)

                    # Fetch records from provider
                    records = await fetcher(
                        nango=nango,
                        connection_id=integration_meta.connection_id,
                        is_initial=not request.incremental,
                        last_synced_at=integration_meta.last_synced_at,
                        data_type=data_type,
                        provider_config_key=provider_key,
                    )
                    logger.info("Fetched %d records for data_type=%s", len(records), data_type)
                    if records:
                        logger.info("Sample record for data_type=%s: %s", data_type, records[0])
                    else:
                        logger.info(
                            "No records returned for data_type=%s; skipping processing.",
                            data_type,
                        )
                        result.items[data_type] = SyncItemResult(
                            status="success", records_synced=0, reason="no_records"
                        )
                        continue

                    # Process records based on category
                    match request.category:
                        case "calendar":
                            async with _integration_session(organisation_id) as session:
                                integration_row = await _reload_integration(session, integration_id)
                                if not integration_row:
                                    logger.warning(
                                        "Integration %s missing when processing calendar data_type=%s",
                                        integration_id,
                                        data_type,
                                    )
                                    result.items[data_type] = SyncItemResult(
                                        status="failed", reason="integration_missing"
                                    )
                                    continue

                                count = await upsert_calendar(
                                    session=session,
                                    organisation_id=organisation_id,
                                    integration=integration_row,
                                    provider=request.provider,
                                    events=records,
                                )
                                logger.info(
                                    "Calendar sync completed for data_type=%s: %d events processed",
                                    data_type,
                                    count,
                                )
                                result.items[data_type] = SyncItemResult(
                                    status="success", records_synced=count
                                )
                                await session.refresh(integration_row)
                                integration_meta = _to_metadata(integration_row)
                        case "crm":
                            async with _integration_session(organisation_id) as session:
                                integration_row = await _reload_integration(session, integration_id)
                                if not integration_row:
                                    logger.warning(
                                        "Integration %s missing when processing CRM data_type=%s",
                                        integration_id,
                                        data_type,
                                    )
                                    result.items[data_type] = SyncItemResult(
                                        status="failed", reason="integration_missing"
                                    )
                                    continue

                                crm_service = CRMSyncService(session, organisation_id)
                                try:
                                    metrics = await sync_crm_data(
                                        service=crm_service,
                                        integration=integration_row,
                                        data_type=data_type,
                                        records=records,
                                        provider=request.provider,
                                    )
                                    records_synced = metrics.get("records_synced")
                                    if records_synced is None:
                                        records_synced = metrics.get("deals") or len(records)
                                    logger.info(
                                        "CRM sync completed for data_type=%s: %s",
                                        data_type,
                                        metrics,
                                    )
                                    result.items[data_type] = SyncItemResult(
                                        status="success", records_synced=records_synced
                                    )
                                    result.extra.setdefault("crm", {})[data_type] = metrics
                                    await _finalize_crm_service(crm_service, result)
                                    await session.refresh(integration_row)
                                    integration_meta = _to_metadata(integration_row)
                                except Exception as crm_error:
                                    with suppress(Exception):
                                        await crm_service.finalize_sync(
                                            SyncStatus.FAILED, str(crm_error)
                                        )
                                    raise
                        case _:
                            logger.warning("Unsupported category: %s", request.category)
                            result.items[data_type] = SyncItemResult(
                                status="ignored", reason="unsupported_category"
                            )

                except Exception as e:
                    logger.error(
                        "Error syncing data_type=%s: %s",
                        data_type,
                        str(e),
                        exc_info=True,
                    )
                    result.items[data_type] = SyncItemResult(
                        status="failed",
                        reason="sync_error",
                        error=str(e),
                    )
            finally:
                if lock_acquired:
                    release_sync_lock(request.integration_id, data_type)

        # Determine overall status
        if any(item.status == "failed" for item in result.items.values()):
            result.status = "failed"
        elif any(
            item.status not in {"success", "ignored", "skipped"} for item in result.items.values()
        ):
            result.status = "partial"
        elif result.items and all(item.status == "success" for item in result.items.values()):
            result.status = "success"

        # Log summary
        success_count = sum(1 for item in result.items.values() if item.status == "success")
        total_records = sum(
            item.records_synced or 0 for item in result.items.values() if item.status == "success"
        )
        logger.info(
            "Sync job completed: org=%s integration=%s success=%d/%d total_records=%d",
            request.organisation_id,
            request.integration_id,
            success_count,
            1,  # Processing one data_type at a time
            total_records,
        )

        return result.to_dict()

    except ValueError as not_found:
        logger.warning(
            "Integration %s not found for org %s: %s",
            integration_id,
            organisation_id,
            str(not_found),
        )
        return SyncJobResult(
            status="failed",
            extra={"error": "integration_not_found", "integration_id": str(integration_id)},
        ).to_dict()
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
    finally:
        with suppress(Exception):
            await nango.close()


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
