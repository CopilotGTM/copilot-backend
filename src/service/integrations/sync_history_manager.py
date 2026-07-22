"""Shared sync history lifecycle manager.

Provides start/complete helpers for IntegrationSyncHistory records.
Used by all sync services (Calendar, Email, Messaging) and by task files
that manage sync history with fresh-session semantics.

Replaces the identical _create_sync_history / _complete_sync_history
private methods that were duplicated across CalendarSyncService,
EmailSyncService, and MessagingSyncService.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationSyncHistory,
    KnowledgeIntegration,
    SyncStatus,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.api_error_utils import extract_api_error_details

logger = get_logger(__name__)


def datetime_now_naive() -> datetime:
    """Return current UTC timestamp without timezone info for naive columns."""
    return datetime.now(UTC).replace(tzinfo=None)


class SyncHistoryManager:
    """Manages IntegrationSyncHistory record lifecycle.

    Two usage patterns:

    1. Service-held session: Pass the service's session at construction.
       Call start() and complete() — both use that session.

    2. Fresh-session (task files): Pass session=None at construction.
       Call start_fresh() and complete_fresh() with an org_id — each
       acquires and releases its own session.
    """

    def __init__(self, session: AsyncSession | None = None) -> None:
        self._session = session

    # ── Pattern 1: Service-held session ─────────────────────────────────

    async def start(self, integration_id: UUID) -> IntegrationSyncHistory:
        """Create an IN_PROGRESS sync history record.

        Requires a session passed at construction.
        """
        assert self._session is not None, "start() requires a session"
        sync_history = IntegrationSyncHistory(
            integration_id=integration_id,
            started_at=datetime_now_naive(),
            status=SyncStatus.IN_PROGRESS,
        )
        self._session.add(sync_history)
        await self._session.commit()
        await self._session.refresh(sync_history)

        logger.debug("Created sync history: sync_id=%s", sync_history.sync_id)
        return sync_history

    async def complete(
        self,
        sync_history: IntegrationSyncHistory,
        *,
        status: SyncStatus,
        records_processed: int,
        records_failed: int,
        metadata: dict[str, Any] | None = None,
        error_details: dict[str, Any] | None = None,
    ) -> None:
        """Update a sync history record with completion details.

        Requires a session passed at construction.
        """
        assert self._session is not None, "complete() requires a session"
        sync_history.completed_at = datetime_now_naive()
        sync_history.status = status
        sync_history.records_processed = records_processed
        sync_history.records_failed = records_failed

        if metadata:
            sync_history.sync_metadata = metadata

        if error_details:
            sync_history.error_details = error_details

        self._session.add(sync_history)
        await self._session.commit()

        logger.debug(
            "Updated sync history: sync_id=%s status=%s processed=%d failed=%d",
            sync_history.sync_id,
            status.value,
            records_processed,
            records_failed,
        )

    async def fail(
        self,
        sync_history: IntegrationSyncHistory,
        *,
        integration_id: UUID,
        records_processed: int = 0,
        records_failed: int,
        error: Exception,
        update_integration: bool = True,
    ) -> None:
        """Persist terminal failure state after discarding a poisoned transaction.

        Database write errors leave SQLAlchemy sessions unusable until rollback.
        This method captures stable IDs first, rolls back, then reloads both rows
        so the original exception cannot strand history in ``in_progress``.
        """
        assert self._session is not None, "fail() requires a session"
        sync_id = sync_history.sync_id
        details = extract_api_error_details(error)
        error_details = {
            "error": details.summary,
            "error_type": type(error).__name__,
            "status_code": details.status_code,
        }

        try:
            await self._session.rollback()

            history_result = await self._session.execute(
                select(IntegrationSyncHistory).where(
                    col(IntegrationSyncHistory.sync_id) == sync_id,
                )
            )
            record = history_result.scalar_one_or_none()
            if record is not None:
                record.completed_at = datetime_now_naive()
                record.status = SyncStatus.FAILED
                record.records_processed = records_processed
                record.records_failed = records_failed
                record.error_details = error_details
                self._session.add(record)

            if update_integration:
                integration_result = await self._session.execute(
                    select(KnowledgeIntegration).where(
                        col(KnowledgeIntegration.integration_id) == integration_id,
                    )
                )
                integration = integration_result.scalar_one_or_none()
                if integration is not None:
                    integration.last_sync_status = SyncStatus.FAILED.value
                    integration.last_sync_error = details.summary
                    self._session.add(integration)

            await self._session.commit()
            logger.warning(
                "Persisted failed sync after transaction rollback: sync_id=%s integration=%s",
                sync_id,
                integration_id,
            )
        except Exception:
            # Failure reporting is best-effort. Callers are already handling a
            # more useful provider/database exception, which must remain the
            # one seen by retry classification and error monitoring.
            logger.error(
                "Could not persist terminal sync failure: sync_id=%s integration=%s",
                sync_id,
                integration_id,
                exc_info=True,
            )

    # ── Pattern 2: Fresh-session (task files) ───────────────────────────

    @staticmethod
    async def start_fresh(
        session: AsyncSession,
        integration_id: UUID,
    ) -> UUID:
        """Create an IN_PROGRESS sync history record using a caller-provided session.

        Returns the sync_id so the caller can complete it later with a
        different session (fresh-session pattern in task files).
        """
        sync_history = IntegrationSyncHistory(
            integration_id=integration_id,
            started_at=datetime_now_naive(),
            status=SyncStatus.IN_PROGRESS,
        )
        session.add(sync_history)
        await session.commit()
        await session.refresh(sync_history)

        logger.debug("Created sync history (fresh): sync_id=%s", sync_history.sync_id)
        return sync_history.sync_id

    @staticmethod
    async def complete_fresh(
        session: AsyncSession,
        sync_id: UUID,
        *,
        status: SyncStatus,
        records_processed: int,
        records_failed: int = 0,
        metadata: dict[str, Any] | None = None,
        error_details: dict[str, Any] | None = None,
    ) -> None:
        """Update a sync history record using a caller-provided session.

        Looks up the record by sync_id, updates it, and commits.
        Used by task files that acquire fresh sessions for each DB operation.
        """
        stmt = select(IntegrationSyncHistory).where(
            col(IntegrationSyncHistory.sync_id) == sync_id,
        )
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()

        if not record:
            logger.warning("Sync history record not found: sync_id=%s", sync_id)
            return

        record.completed_at = datetime_now_naive()
        record.status = status
        record.records_processed = records_processed
        record.records_failed = records_failed

        if metadata:
            record.sync_metadata = metadata

        if error_details:
            record.error_details = error_details

        session.add(record)
        await session.commit()

        logger.debug(
            "Updated sync history (fresh): sync_id=%s status=%s processed=%d failed=%d",
            sync_id,
            status.value,
            records_processed,
            records_failed,
        )
