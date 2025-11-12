from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
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
)
from src.infra.config.nango_provider_mapping import to_nango_connect_integration_key
from src.infra.utils.logger import get_logger
from src.service.integrations import NangoClient
from src.service.integrations.crm.entities import (
    CompanySyncService,
    ContactSyncService,
    DealSyncService,
    DealTaskSyncService,
    OwnerSyncService,
    PipelineSyncService,
    UserSyncService,
)
from src.service.integrations.email_sync_service import EmailSyncService

from ..adapters.calendar_adapter import upsert_calendar
from ..adapters.conversation_intelligence_adapter import sync_conversation_intelligence_data
from ..adapters.crm_adapter import sync_crm_data
from ..models.sync_models import SyncItemResult, SyncJobRequest, SyncJobResult
from ..providers.registry import get_fetcher
from ..utils.dedupe import acquire_sync_lock, release_sync_lock

logger = get_logger(__name__)


def _create_crm_service(
    data_type: str,
    session: AsyncSession,
    organisation_id: UUID,
) -> (
    DealSyncService
    | DealTaskSyncService
    | ContactSyncService
    | CompanySyncService
    | OwnerSyncService
    | UserSyncService
    | PipelineSyncService
):
    """Create appropriate CRM sync service based on data type."""
    service_map: dict[
        str,
        type[
            DealSyncService
            | DealTaskSyncService
            | ContactSyncService
            | CompanySyncService
            | OwnerSyncService
            | UserSyncService
            | PipelineSyncService
        ],
    ] = {
        "deals": DealSyncService,
        "deal_tasks": DealTaskSyncService,
        "tasks": DealTaskSyncService,  # Alias for backward compatibility
        "contacts": ContactSyncService,
        "companies": CompanySyncService,
        "owners": OwnerSyncService,
        "users": UserSyncService,
        "pipelines": PipelineSyncService,
    }

    service_class = service_map.get(data_type)
    if not service_class:
        raise ValueError(f"No service found for data_type: {data_type}")

    return service_class(db=session, organisation_id=organisation_id)


@dataclass(slots=True)
class IntegrationMetadata:
    integration_id: UUID
    connection_id: str
    integration_type: IntegrationType
    last_synced_at: datetime | None


@dataclass(slots=True)
class CategoryHandlerOutcome:
    item_result: SyncItemResult
    extra: dict[str, Any] | None = None
    should_stop: bool = False
    integration_meta: IntegrationMetadata | None = None


def _to_metadata(integration: KnowledgeIntegration) -> IntegrationMetadata:
    if not integration.connection_id:
        raise ValueError(f"Integration {integration.integration_id} has no connection_id")
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
            KnowledgeIntegration.integration_id == integration_id  # type: ignore[arg-type]
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
    stmt = select(KnowledgeIntegration).where(KnowledgeIntegration.integration_id == integration_id)  # type: ignore[arg-type]
    res = await session.execute(stmt)
    return res.scalars().first()


_CI_ALIAS_MAP = {
    "call_speakers": "speakers",
    "speakers": "speakers",
    "transcript_segments": "segments",
    "call_transcript_segments": "segments",
    "segments": "segments",
    "call_analytics": "analytics",
    "analytics": "analytics",
}


async def _handle_calendar(
    organisation_id: UUID,
    integration_id: UUID,
    data_type: str,
    records: list[dict[str, Any]],
    provider: str,
) -> CategoryHandlerOutcome:
    async with _integration_session(organisation_id) as session:
        integration_row = await _reload_integration(session, integration_id)
        if not integration_row:
            logger.warning(
                "Integration %s missing when processing calendar data_type=%s",
                integration_id,
                data_type,
            )
            return CategoryHandlerOutcome(
                item_result=SyncItemResult(status="failed", reason="integration_missing"),
                should_stop=True,
            )

        count = await upsert_calendar(
            session=session,
            organisation_id=organisation_id,
            integration=integration_row,
            provider=provider,
            events=records,
        )
        logger.info(
            "Calendar sync completed for data_type=%s: %d events processed",
            data_type,
            count,
        )
        await session.refresh(integration_row)
        return CategoryHandlerOutcome(
            item_result=SyncItemResult(status="success", records_synced=count),
            integration_meta=_to_metadata(integration_row),
        )


async def _handle_email(
    organisation_id: UUID,
    integration_id: UUID,
    data_type: str,
    records: list[dict[str, Any]],
    provider: str,
) -> CategoryHandlerOutcome:
    """
    Handle email folder and message sync.

    Supported data_types:
    - folders: Email folder/label sync
    - messages: Email message sync
    - emails: Alias for messages
    """
    async with _integration_session(organisation_id) as session:
        integration_row = await _reload_integration(session, integration_id)
        if not integration_row:
            logger.warning(
                "Integration %s missing when processing email data_type=%s",
                integration_id,
                data_type,
            )
            return CategoryHandlerOutcome(
                item_result=SyncItemResult(status="failed", reason="integration_missing"),
                should_stop=True,
            )

        email_service = EmailSyncService(session=session, organisation_id=organisation_id)

        # Route to appropriate sync method based on data_type
        if data_type in ("folders",):
            metrics = await email_service.sync_folders(
                integration=integration_row,
                folders=records,
                provider=provider,
            )
        elif data_type in ("messages", "emails"):
            metrics = await email_service.sync_messages(
                integration=integration_row,
                messages=records,
                provider=provider,
            )
        else:
            logger.warning("Unknown email data_type: %s", data_type)
            return CategoryHandlerOutcome(
                item_result=SyncItemResult(status="ignored", reason="unknown_data_type"),
                should_stop=True,
            )

        records_synced = metrics.get("records_processed", 0)

        logger.info(
            "Email sync completed for data_type=%s: %s",
            data_type,
            metrics,
        )
        await session.refresh(integration_row)
        return CategoryHandlerOutcome(
            item_result=SyncItemResult(status="success", records_synced=records_synced),
            extra={"email": {data_type: metrics}},
            integration_meta=_to_metadata(integration_row),
        )


async def _handle_crm(
    organisation_id: UUID,
    integration_id: UUID,
    data_type: str,
    records: list[dict[str, Any]],
    provider: str,
) -> CategoryHandlerOutcome:
    async with _integration_session(organisation_id) as session:
        integration_row = await _reload_integration(session, integration_id)
        if not integration_row:
            logger.warning(
                "Integration %s missing when processing CRM data_type=%s",
                integration_id,
                data_type,
            )
            return CategoryHandlerOutcome(
                item_result=SyncItemResult(status="failed", reason="integration_missing"),
                should_stop=True,
            )

        crm_service = _create_crm_service(data_type, session, organisation_id)
        metrics = await sync_crm_data(
            service=crm_service,
            integration=integration_row,
            data_type=data_type,
            records=records,
            provider=provider,
        )
        records_synced = metrics.get("records_synced")
        if records_synced is None:
            records_synced = metrics.get("deals") or len(records)

        try:
            synced_count = int(records_synced or 0)
        except (TypeError, ValueError):
            synced_count = len(records)

        logger.info(
            "CRM sync completed for data_type=%s: %s",
            data_type,
            metrics,
        )
        await session.refresh(integration_row)
        return CategoryHandlerOutcome(
            item_result=SyncItemResult(status="success", records_synced=synced_count),
            extra={"crm": {data_type: metrics}},
            integration_meta=_to_metadata(integration_row),
        )


async def _sync_conversation_intelligence(
    organisation_id: UUID,
    integration_id: UUID,
    data_type: str,
    records: list[dict[str, Any]],
    provider: str,
) -> CategoryHandlerOutcome:
    async with _integration_session(organisation_id) as session:
        integration_row = await _reload_integration(session, integration_id)
        if not integration_row:
            logger.warning(
                "Integration %s missing when processing CI data_type=%s",
                integration_id,
                data_type,
            )
            return CategoryHandlerOutcome(
                item_result=SyncItemResult(status="failed", reason="integration_missing"),
                should_stop=True,
            )

        metrics = await sync_conversation_intelligence_data(
            session=session,
            integration=integration_row,
            data_type=data_type,
            records=records,
            provider=provider,
        )

        if metrics.get("status") == "ignored":
            reason = str(metrics.get("reason") or "ignored")
            return CategoryHandlerOutcome(
                item_result=SyncItemResult(status="ignored", reason=reason),
                should_stop=True,
            )

        normalized = data_type.lower()
        metric_key = _CI_ALIAS_MAP.get(normalized, normalized)
        records_synced = metrics.get(metric_key)
        if records_synced is None:
            records_synced = metrics.get("records_processed", 0)

        try:
            synced_count = int(records_synced or 0)
        except (TypeError, ValueError):
            synced_count = 0

        logger.info(
            "Conversation intelligence sync completed for data_type=%s: %s",
            data_type,
            metrics,
        )

        return CategoryHandlerOutcome(
            item_result=SyncItemResult(status="success", records_synced=synced_count),
            extra={"conversation_intelligence": {data_type: metrics}},
        )


async def _dispatch_category(
    category: str,
    organisation_id: UUID,
    integration_id: UUID,
    data_type: str,
    records: list[dict[str, Any]],
    provider: str,
) -> CategoryHandlerOutcome:
    handler_map = {
        "calendar": _handle_calendar,
        "email": _handle_email,
        "crm": _handle_crm,
        "conversation_intelligence": _sync_conversation_intelligence,
    }

    handler = handler_map.get(category)
    if not handler:
        logger.warning("Unsupported category: %s", category)
        return CategoryHandlerOutcome(
            item_result=SyncItemResult(status="ignored", reason="unsupported_category"),
            should_stop=True,
        )

    return await handler(
        organisation_id=organisation_id,
        integration_id=integration_id,
        data_type=data_type,
        records=records,
        provider=provider,
    )


@contextmanager
def _sync_lock_guard(integration_id: str, data_type: str) -> Iterator[bool]:
    lock_acquired = acquire_sync_lock(integration_id, data_type)
    try:
        yield lock_acquired
    finally:
        if lock_acquired:
            release_sync_lock(integration_id, data_type)


def _apply_outcome(
    result: SyncJobResult,
    outcome: CategoryHandlerOutcome,
) -> tuple[IntegrationMetadata | None, bool]:
    if outcome.extra:
        for key, value in outcome.extra.items():
            if isinstance(value, dict):
                result.extra.setdefault(key, {}).update(value)
            else:
                result.extra[key] = value
    return outcome.integration_meta, outcome.should_stop


async def _execute_category_sync(
    *,
    request: SyncJobRequest,
    nango: NangoClient,
    integration_meta: IntegrationMetadata,
    provider_key: str,
    result: SyncJobResult,
    organisation_id: UUID,
    integration_id: UUID,
    sync_data_type: str,
) -> tuple[IntegrationMetadata | None, bool]:
    fetcher = get_fetcher(request.category, request.provider, sync_data_type)
    logger.info("Fetcher obtained: %s", fetcher.__name__ if fetcher else "None")
    if not fetcher:
        logger.warning(
            "No fetcher found for category=%s provider=%s data_type=%s",
            request.category,
            request.provider,
            sync_data_type,
        )
        result.items[sync_data_type] = SyncItemResult(status="ignored", reason="no_fetcher")
        return None, True

    logger.info("Starting Fetch for Connection ID: %s", integration_meta.connection_id)
    logger.info("Last Synced At: %s", integration_meta.last_synced_at)
    logger.info("Is Initial Sync: %s", not request.incremental)

    records = await fetcher(
        nango=nango,
        connection_id=integration_meta.connection_id,
        is_initial=not request.incremental,
        last_synced_at=integration_meta.last_synced_at,
        data_type=sync_data_type,
        provider_config_key=provider_key,
    )
    logger.info("Fetched %d records for data_type=%s", len(records), sync_data_type)
    if not records:
        logger.info(
            "No records returned for data_type=%s; skipping processing.",
            sync_data_type,
        )
        result.items[sync_data_type] = SyncItemResult(
            status="success", records_synced=0, reason="no_records"
        )
        return None, True

    logger.info("Sample record for data_type=%s: %s", sync_data_type, records[0])

    outcome = await _dispatch_category(
        category=request.category,
        organisation_id=organisation_id,
        integration_id=integration_id,
        data_type=sync_data_type,
        records=records,
        provider=request.provider,
    )

    result.items[sync_data_type] = outcome.item_result
    return _apply_outcome(result, outcome)


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

    # Validate required fields
    if not request.sync_name:
        return {
            "status": "failed",
            "error": "sync_name is required",
            "items": {},
        }

    sync_data_type: str = request.sync_name  # Type narrowing

    try:
        integration_meta = await _load_integration_metadata(organisation_id, integration_id)
        logger.info(
            "Integration found: connection_id=%s last_synced=%s",
            integration_meta.connection_id,
            integration_meta.last_synced_at,
        )

        # Prepare for sync
        result = SyncJobResult(status="success")

        # Determine provider key
        provider_key = request.provider_config_key or to_nango_connect_integration_key(
            integration_meta.integration_type
        )

        logger.info("Processing data type: %s", sync_data_type)

        # Check sync lock
        with _sync_lock_guard(str(request.integration_id), sync_data_type) as lock_acquired:
            if not lock_acquired:
                result.items[sync_data_type] = SyncItemResult(status="skipped", reason="locked")
                return result.to_dict()

            try:
                integration_update, should_stop = await _execute_category_sync(
                    request=request,
                    nango=nango,
                    integration_meta=integration_meta,
                    provider_key=provider_key,
                    result=result,
                    organisation_id=organisation_id,
                    integration_id=integration_id,
                    sync_data_type=sync_data_type,
                )
                integration_meta = integration_update or integration_meta
                if should_stop:
                    return result.to_dict()

            except Exception as e:
                logger.error(
                    "Error syncing data_type=%s: %s",
                    sync_data_type,
                    str(e),
                    exc_info=True,
                )
                result.items[sync_data_type] = SyncItemResult(
                    status="failed",
                    reason="sync_error",
                    error=str(e),
                )

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
            1,
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
