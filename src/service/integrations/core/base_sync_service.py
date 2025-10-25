"""Core base classes for integration sync services."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Generic, TypeVar
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.base_mixins import utcnow
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)

# Generic type for domain models
T = TypeVar("T")


class SyncResult:
    """Standard result object for sync operations."""

    def __init__(self, entity_type: str):
        self.entity_type = entity_type
        self.records_processed = 0
        self.records_failed = 0
        self.records_created = 0
        self.records_updated = 0
        self.records_deleted = 0
        self.errors: list[str] = []
        self.metadata: dict[str, Any] = {}
        self.started_at = utcnow()
        self.completed_at: datetime | None = None

    def increment_processed(self, count: int = 1) -> None:
        """Increment processed counter."""
        self.records_processed += count

    def increment_failed(self, count: int = 1) -> None:
        """Increment failed counter."""
        self.records_failed += count

    def increment_created(self, count: int = 1) -> None:
        """Increment created counter."""
        self.records_created += count

    def increment_updated(self, count: int = 1) -> None:
        """Increment updated counter."""
        self.records_updated += count

    def increment_deleted(self, count: int = 1) -> None:
        """Increment deleted counter."""
        self.records_deleted += count

    def add_error(self, error: str) -> None:
        """Add an error message."""
        self.errors.append(error)

    def complete(self) -> None:
        """Mark sync as completed."""
        self.completed_at = utcnow()

    def to_dict(self) -> dict[str, Any]:
        """Convert result to dictionary."""
        return {
            "entity_type": self.entity_type,
            "records_processed": self.records_processed,
            "records_failed": self.records_failed,
            "records_created": self.records_created,
            "records_updated": self.records_updated,
            "records_deleted": self.records_deleted,
            "errors": self.errors,
            "metadata": self.metadata,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class BaseSyncService(ABC, Generic[T]):
    """Abstract base class for all entity sync services.

    This class provides common functionality for syncing external data
    to our database, including:
    - Database session management
    - Organisation scoping
    - Record counting and error tracking
    - Standard sync result format

    Subclasses must implement:
    - sync(): Main synchronization logic
    - _transform_record(): Provider-specific data transformation
    """

    def __init__(self, db: AsyncSession, organisation_id: UUID):
        """Initialize sync service.

        Args:
            db: Async database session
            organisation_id: Organisation UUID for scoping
        """
        self.db = db
        self.organisation_id = organisation_id
        self.result: SyncResult | None = None

    @abstractmethod
    async def sync(
        self,
        *,
        integration: KnowledgeIntegration,
        records: Iterable[dict[str, Any]],
        provider: str,
    ) -> dict[str, Any]:
        """Sync records from external provider to database.

        Args:
            integration: KnowledgeIntegration instance
            records: Iterator of raw records from provider
            provider: Provider name (e.g., 'hubspot', 'salesforce')

        Returns:
            Dictionary with sync statistics and metadata
        """
        pass

    @abstractmethod
    def _transform_record(self, raw: dict[str, Any], provider: str) -> T:
        """Transform provider-specific data to domain model.

        Args:
            raw: Raw record from provider API
            provider: Provider name

        Returns:
            Transformed domain model instance
        """
        pass

    def _get_entity_type(self) -> str:
        """Get entity type name for this service.

        Returns:
            Entity type name (e.g., 'deal', 'contact')
        """
        return self.__class__.__name__.replace("SyncService", "").lower()

    def _init_result(self) -> SyncResult:
        """Initialize sync result tracking.

        Returns:
            New SyncResult instance
        """
        entity_type = self._get_entity_type()
        self.result = SyncResult(entity_type)
        return self.result

    async def _load_existing_by_external_ids(
        self,
        model_class: type[T],
        external_ids: list[str],
        integration_id: UUID,
    ) -> dict[str, T]:
        """Load existing records by external IDs.

        Args:
            model_class: SQLModel class
            external_ids: List of external IDs to fetch
            integration_id: Integration UUID

        Returns:
            Dictionary mapping external_id to model instance
        """
        from sqlalchemy import select

        if not external_ids:
            return {}

        stmt = (
            select(model_class)
            .where(model_class.integration_id == integration_id)  # type: ignore[attr-defined]
            .where(model_class.external_id.in_(external_ids))  # type: ignore[attr-defined]
        )
        result = await self.db.execute(stmt)
        rows = result.scalars().all()
        return {row.external_id: row for row in rows}  # type: ignore[attr-defined]

    def _log_sync_start(
        self,
        entity_type: str,
        provider: str,
        record_count: int,
    ) -> None:
        """Log sync operation start.

        Args:
            entity_type: Type of entity being synced
            provider: Provider name
            record_count: Number of records to sync
        """
        logger.info(
            "Starting %s sync: org=%s provider=%s count=%d",
            entity_type,
            self.organisation_id,
            provider,
            record_count,
        )

    def _log_sync_complete(self, result: SyncResult) -> None:
        """Log sync operation completion.

        Args:
            result: SyncResult with statistics
        """
        logger.info(
            "Completed %s sync: org=%s processed=%d failed=%d created=%d updated=%d",
            result.entity_type,
            self.organisation_id,
            result.records_processed,
            result.records_failed,
            result.records_created,
            result.records_updated,
        )

    def _log_error(self, entity_type: str, external_id: str | None, error: Exception) -> None:
        """Log sync error for specific record.

        Args:
            entity_type: Type of entity
            external_id: External ID of failed record
            error: Exception that occurred
        """
        logger.error(
            "Failed to sync %s %s: %s",
            entity_type,
            external_id or "unknown",
            error,
            exc_info=True,
        )
