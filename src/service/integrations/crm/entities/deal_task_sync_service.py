"""Deal task entity sync service for CRM integrations."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from src.domain.models.organisation_tables.base_mixins import utcnow
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivityPriority,
    CRMActivityStatus,
    CRMActivityType,
    CRMDeal,
    CRMDealTask,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.core import BaseSyncService

from .activity_enum_mappings import (
    normalize_activity_priority,
    normalize_activity_status,
    normalize_activity_type,
)
from .task_enrichment_service import TaskEnrichmentService

logger = get_logger(__name__)


class DealTaskSyncService(BaseSyncService[CRMDealTask]):
    """Service for syncing CRM deal task data.

    Handles synchronization of task records associated with deals.
    """

    async def sync(
        self,
        *,
        integration: KnowledgeIntegration,
        records: Iterable[dict[str, Any]],
        provider: str,
    ) -> dict[str, Any]:
        """Sync deal task records from external provider.

        Args:
            integration: KnowledgeIntegration instance
            records: Raw task records from provider API
            provider: Provider name (e.g., 'hubspot')

        Returns:
            Dictionary with sync statistics
        """
        result = self._init_result()
        self._log_sync_start("deal_tasks", provider, 0)

        task_list = [t for t in records if isinstance(t, dict)]
        if not task_list:
            result.complete()
            return {
                "deal_tasks": 0,
                **result.to_dict(),
            }

        # Load existing tasks
        task_external_ids = [str(t.get("id")) for t in task_list if t.get("id")]
        existing = await self._load_existing_by_external_ids(
            CRMDealTask,
            task_external_ids,
            integration.integration_id,
        )

        # Load deal mapping for association resolution
        deal_map = await self._load_deal_map(integration.integration_id)

        now = utcnow()
        tasks_processed = 0

        for task in task_list:
            try:
                external_id = task.get("id")
                if not external_id:
                    result.increment_failed()
                    result.add_error(f"Missing external ID for task: {task}")
                    continue

                # Get or create record
                record = existing.get(external_id)
                is_new = record is None

                if not record:
                    record = CRMDealTask(
                        organisation_id=self.organisation_id,
                        integration_id=integration.integration_id,
                        external_id=external_id,
                    )

                # Transform and update record
                self._update_task_record(record, task, provider, deal_map)

                # Update metadata
                record.synced_at = now
                record.last_seen_at = now
                is_archived = bool(task.get("archived"))
                record.is_deleted = is_archived

                self.db.add(record)
                tasks_processed += 1
                result.increment_processed()

                if is_new:
                    result.increment_created()
                else:
                    result.increment_updated()

            except Exception as exc:
                self._log_error("deal_task", task.get("id"), exc)
                result.increment_failed()
                result.add_error(str(exc))

        # Commit tasks
        await self.db.commit()

        # Enrich tasks with owner details from CRMUser
        if tasks_processed > 0:
            enrichment_service = TaskEnrichmentService(
                db=self.db,
                organisation_id=self.organisation_id,
            )
            await enrichment_service.enrich_tasks_for_integration(integration.integration_id)
            await self.db.commit()

        result.complete()
        # Log completion using the standard SyncResult-based logger
        self._log_sync_complete(result)

        return {
            "deal_tasks": tasks_processed,
            **result.to_dict(),
        }

    def _update_task_record(
        self,
        record: CRMDealTask,
        task: dict[str, Any],
        provider: str,
        deal_map: dict[str, UUID],
    ) -> None:
        """Update task record with data from provider.

        Args:
            record: CRMDealTask instance to update
            task: Raw task data from provider
            provider: Provider name
            deal_map: Mapping of deal external_id to deal_id
        """
        props = task.get("properties", {})

        # Extract core task fields
        if provider == "hubspot":
            record.subject = props.get("hs_task_subject")
            record.body = props.get("hs_task_body")
            # Normalize to canonical enum values for cross-CRM analytics
            raw_status = props.get("hs_task_status")
            raw_type = props.get("hs_task_type")
            raw_priority = props.get("hs_task_priority")

            normalized_status = normalize_activity_status(raw_status)
            normalized_type = normalize_activity_type(raw_type)
            normalized_priority = normalize_activity_priority(raw_priority)

            record.status = normalized_status
            record.task_type = normalized_type
            record.priority = normalized_priority

            # Preserve raw provider values when we fall back.
            if raw_status and normalized_status is None:
                props["_raw_activity_status"] = raw_status

            if raw_type and normalized_type == CRMActivityType.OTHER.value:
                if record.activity_subtype is None:
                    record.activity_subtype = str(raw_type)
                props["_raw_activity_type"] = raw_type

            if raw_priority and normalized_priority == CRMActivityPriority.NONE.value:
                props["_raw_activity_priority"] = raw_priority

            # Owner information
            record.owner_external_id = props.get("hubspot_owner_id")

            owner_email = props.get("hubspot_owner_email")
            if isinstance(owner_email, str):
                record.owner_email = owner_email

            owner_name = props.get("hubspot_owner_name")
            if isinstance(owner_name, str):
                record.owner_name = owner_name

            # Parse dates
            record.due_date = self._parse_hubspot_timestamp(props.get("hs_timestamp"))
            record.completed_date = self._parse_hubspot_timestamp(
                props.get("hs_task_completion_date")
            )
            record.reminder_date = self._parse_hubspot_timestamp(props.get("hs_task_reminders"))
            record.source_created_at = self._parse_hubspot_timestamp(props.get("hs_createdate"))
            record.source_modified_at = self._parse_hubspot_timestamp(
                props.get("hs_lastmodifieddate")
            )

        # Resolve deal association
        # HubSpot returns associations in format: {"deals": {"results": [{"id": "...", "type": "..."}]}}
        associations = task.get("associations", {})
        deals_data = associations.get("deals", {})

        # Handle both direct list format and results format
        if isinstance(deals_data, dict):
            deals = deals_data.get("results", [])
        elif isinstance(deals_data, list):
            deals = deals_data
        else:
            deals = []

        if deals and isinstance(deals, list) and len(deals) > 0:
            first_deal = deals[0]
            deal_ext_id = str(first_deal.get("id")) if isinstance(first_deal, dict) else None
            if deal_ext_id:
                record.deal_external_id = deal_ext_id
                record.deal_id = deal_map.get(deal_ext_id)

                # Log if deal mapping failed
                if not record.deal_id:
                    logger.warning(
                        "Task %s has deal association %s but no matching deal found in deal_map",
                        record.external_id,
                        deal_ext_id,
                    )
                else:
                    logger.debug(
                        "Task %s mapped to deal %s (external_id=%s)",
                        record.external_id,
                        record.deal_id,
                        deal_ext_id,
                    )

        # Calculate metrics
        record.metrics = self._calculate_metrics(record)

        # Store raw data
        record.properties = props
        record.data_payload = task

    async def _load_deal_map(self, integration_id: UUID) -> dict[str, UUID]:
        """Load deal external_id → deal_id mapping.

        Args:
            integration_id: Integration UUID

        Returns:
            Dictionary mapping external_id to deal_id
        """
        # Use SQLAlchemy table columns to satisfy static type checking
        # Access __table__ via getattr to avoid static analysis complaints
        deal_table = getattr(CRMDeal, "__table__", None)
        if deal_table is None:
            return {}

        stmt = select(deal_table.c.external_id, deal_table.c.deal_id).where(
            deal_table.c.integration_id == integration_id,
            deal_table.c.is_deleted.is_(False),
        )
        result = await self.db.execute(stmt)
        rows = result.all()
        return {row[0]: row[1] for row in rows}

    def _transform_record(self, raw: dict[str, Any], provider: str) -> CRMDealTask:
        """Transform provider-specific data to CRMDealTask model.

        Note: This is a required abstract method stub. The actual transformation
        logic is in _update_task_record() called from sync().

        Args:
            raw: Raw record from provider API
            provider: Provider name

        Returns:
            Transformed CRMDealTask instance
        """
        # This method is not used in current implementation as we handle
        # transformation directly in sync() via _update_task_record()
        raise NotImplementedError("_transform_record is not used; transformation handled in sync()")

    def _calculate_metrics(
        self,
        record: CRMDealTask,
    ) -> dict[str, Any]:
        """Calculate derived metrics for task analytics.

        Args:
            record: CRMDealTask instance with populated fields

        Returns:
            Dictionary of calculated metrics
        """
        metrics: dict[str, Any] = {}
        now = utcnow()

        # Overdue status (use canonical enum values)
        if record.due_date and record.status not in [
            CRMActivityStatus.COMPLETED.value,
            CRMActivityStatus.DEFERRED.value,
        ]:
            metrics["is_overdue"] = record.due_date < now
            if metrics["is_overdue"]:
                metrics["days_overdue"] = (now - record.due_date).days

        # Completion metrics
        if record.status == CRMActivityStatus.COMPLETED.value and record.completed_date:
            if record.due_date:
                delta = (record.completed_date - record.due_date).days
                metrics["completed_on_time"] = delta <= 0
                metrics["days_to_complete"] = delta
            if record.source_created_at:
                metrics["completion_time_days"] = (
                    record.completed_date - record.source_created_at
                ).days

        # Task age
        if record.source_created_at:
            metrics["task_age_days"] = (now - record.source_created_at).days

        # Priority flag (use canonical enum value)
        if record.priority:
            metrics["is_high_priority"] = record.priority == CRMActivityPriority.HIGH.value

        return metrics

    def _parse_hubspot_timestamp(self, value: Any) -> datetime | None:
        """Parse HubSpot timestamp to datetime.

        HubSpot typically provides timestamps as Unix milliseconds.

        Args:
            value: Timestamp value from HubSpot

        Returns:
            Parsed datetime or None
        """
        if not value:
            return None

        try:
            # HubSpot uses milliseconds
            if isinstance(value, int | float):
                return datetime.fromtimestamp(value / 1000.0)
            # Try parsing ISO string
            if isinstance(value, str):
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError) as exc:
            logger.warning("Failed to parse timestamp %s: %s", value, exc)

        return None
