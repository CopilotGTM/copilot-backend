"""Pipeline entity sync service for CRM integrations."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from src.domain.models.organisation_tables.base_mixins import utcnow
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.crm import (
    CRMDeal,
    CRMDealStage,
    CRMPipeline,
    CRMStageState,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.core import BaseSyncService
from src.workers.integrations_worker.utils.sync_metadata import (
    SyncAction,
    SyncContext,
    classify_records,
    log_sync_action_distribution,
)

logger = get_logger(__name__)


class PipelineSyncService(BaseSyncService[CRMPipeline]):
    """Service for syncing CRM pipeline and stage data.

    Handles synchronization of pipeline records with:
    - Nested stage synchronization
    - Soft-delete for orphaned stages
    - Probability normalization
    - Stage state determination (open/won/lost)
    """

    async def sync(
        self,
        *,
        integration: KnowledgeIntegration,
        records: Iterable[dict[str, Any]],
        provider: str,
    ) -> dict[str, Any]:
        """Sync pipeline records from external provider.

        Args:
            integration: KnowledgeIntegration instance
            records: Raw pipeline records from provider API
            provider: Provider name (e.g., 'hubspot')

        Returns:
            Dictionary with sync statistics including stages
        """
        result = self._init_result()
        self._log_sync_start("pipelines", provider, 0)

        pipeline_list = [p for p in records if isinstance(p, dict)]
        if not pipeline_list:
            result.complete()
            return {"pipelines": 0, "stages": 0, **result.to_dict()}

        # Load existing pipelines
        pipeline_external_ids = [
            str(ext_id)
            for pipeline in pipeline_list
            if (ext_id := self._extract_external_id(pipeline)) is not None
        ]
        existing = await self._load_existing_by_external_ids(
            CRMPipeline,
            pipeline_external_ids,
            integration.integration_id,
        )

        now = utcnow()
        pipelines_processed = 0
        stages_processed = 0

        for pipeline in pipeline_list:
            try:
                external_id = self._extract_external_id(pipeline)
                if not external_id:
                    result.increment_failed()
                    result.add_error(f"Missing external ID for pipeline: {pipeline}")
                    continue

                # Get or create record
                record = existing.get(external_id)
                is_new = record is None

                if not record:
                    record = CRMPipeline(
                        organisation_id=self.organisation_id,
                        integration_id=integration.integration_id,
                        external_id=external_id,
                    )

                # Transform and update record
                self._update_pipeline_record(record, pipeline, provider)

                # Update metadata
                record.synced_at = now
                record.last_seen_at = now
                record.is_deleted = False

                self.db.add(record)
                pipelines_processed += 1
                result.increment_processed()

                if is_new:
                    result.increment_created()
                else:
                    result.increment_updated()

                # Sync stages for this pipeline
                stages_synced = await self._sync_stages_for_pipeline(
                    integration, record, pipeline.get("stages")
                )
                stages_processed += stages_synced

            except Exception as exc:
                self._log_error("pipeline", pipeline.get("id"), exc)
                result.increment_failed()
                result.add_error(str(exc))

        # Commit pipelines and stages
        await self.db.commit()

        result.metadata.update(
            {
                "pipelines": pipelines_processed,
                "stages": stages_processed,
            }
        )
        result.complete()
        self._log_sync_complete(result)

        return {
            "pipelines": pipelines_processed,
            "stages": stages_processed,
            **result.to_dict(),
        }

    async def sync_incremental(
        self,
        *,
        integration: KnowledgeIntegration,
        records: Iterable[dict[str, Any]],
        provider: str,
    ) -> dict[str, Any]:
        """Sync pipeline records incrementally.

        This method is optimized for incremental syncs where records come with
        sync_action metadata. It uses database state as the source of truth.

        Args:
            integration: KnowledgeIntegration instance
            records: Raw pipeline records from provider API with _sync_metadata
            provider: Provider name (e.g., 'hubspot')

        Returns:
            Dictionary with sync statistics including stages
        """
        result = self._init_result()
        self._log_sync_start("pipelines_incremental", provider, 0)

        pipeline_list = [p for p in records if isinstance(p, dict)]
        if not pipeline_list:
            result.complete()
            return {"pipelines": 0, "stages": 0, **result.to_dict()}

        # Log sync action distribution for observability
        log_sync_action_distribution(pipeline_list, "pipelines", logger)

        # Load existing pipelines
        pipeline_external_ids = [
            str(ext_id)
            for pipeline in pipeline_list
            if (ext_id := self._extract_external_id(pipeline)) is not None
        ]
        existing = await self._load_existing_by_external_ids(
            CRMPipeline,
            pipeline_external_ids,
            integration.integration_id,
        )
        existing_ids = set(existing.keys())

        # Classify records for logging (sync_action is only for observability)
        context = SyncContext(existing_ids=existing_ids)
        classify_records(pipeline_list, context)

        now = utcnow()
        pipelines_processed = 0
        pipelines_skipped = 0
        stages_processed = 0

        for pipeline in pipeline_list:
            try:
                external_id = self._extract_external_id(pipeline)
                if not external_id:
                    result.increment_failed()
                    result.add_error(f"Missing external ID for pipeline: {pipeline}")
                    continue

                # Extract sync metadata for logging/observability
                sync_metadata = pipeline.get("_sync_metadata", {})
                sync_action = SyncAction(sync_metadata.get("sync_action", SyncAction.ADDED.value))

                # Handle deletions
                is_archived = pipeline.get("archived", False) or pipeline.get("IsDeleted", False)

                if sync_action == SyncAction.DELETED or is_archived:
                    # Soft-delete: mark pipeline and its stages as deleted if exists
                    record = existing.get(external_id)
                    if record:
                        record.is_deleted = True
                        record.is_active = False
                        record.synced_at = now
                        record.last_seen_at = now
                        self.db.add(record)
                        result.increment_deleted()
                        pipelines_processed += 1
                        # Also soft-delete stages
                        await self._soft_delete_pipeline_stages(
                            integration.integration_id, record.pipeline_id, now
                        )
                    continue

                # Database state is source of truth for create vs update
                record = existing.get(external_id)
                is_new = record is None

                if is_new:
                    # Create new pipeline
                    record = CRMPipeline(
                        organisation_id=self.organisation_id,
                        integration_id=integration.integration_id,
                        external_id=external_id,
                    )
                else:
                    # Check for meaningful changes before updating
                    if not self._has_meaningful_changes(record, pipeline, provider):
                        pipelines_skipped += 1
                        # Update last_seen_at even if no changes
                        record.last_seen_at = now
                        self.db.add(record)
                        # Still need to sync stages even if pipeline unchanged
                        stages_synced = await self._sync_stages_for_pipeline(
                            integration, record, pipeline.get("stages")
                        )
                        stages_processed += stages_synced
                        continue

                # Transform and update record
                self._update_pipeline_record(record, pipeline, provider)

                # Update metadata
                record.synced_at = now
                record.last_seen_at = now
                record.is_deleted = False

                self.db.add(record)
                pipelines_processed += 1
                result.increment_processed()

                if is_new:
                    result.increment_created()
                else:
                    result.increment_updated()

                # Sync stages for this pipeline
                stages_synced = await self._sync_stages_for_pipeline(
                    integration, record, pipeline.get("stages")
                )
                stages_processed += stages_synced

            except Exception as exc:
                self._log_error("pipeline", pipeline.get("id"), exc)
                result.increment_failed()
                result.add_error(str(exc))

        # Commit pipelines and stages
        await self.db.commit()

        result.metadata.update(
            {
                "pipelines": pipelines_processed,
                "stages": stages_processed,
                "skipped": pipelines_skipped,
            }
        )
        result.complete()

        logger.info(
            "Incremental pipeline sync complete: pipelines=%d, skipped=%d, stages=%d, created=%d, updated=%d, deleted=%d",
            pipelines_processed,
            pipelines_skipped,
            stages_processed,
            result.created,
            result.updated,
            result.deleted,
        )

        return {
            "pipelines": pipelines_processed,
            "stages": stages_processed,
            "skipped": pipelines_skipped,
            **result.to_dict(),
        }

    def _has_meaningful_changes(
        self,
        existing: CRMPipeline,
        incoming: dict[str, Any],
        provider: str,
    ) -> bool:
        """Check if incoming data has meaningful changes compared to existing record.

        Args:
            existing: Existing CRMPipeline record from database
            incoming: Incoming pipeline data from provider
            provider: Provider name for field mapping

        Returns:
            True if there are meaningful changes, False otherwise
        """
        _ = provider  # Reserved for future provider-specific logic

        # Compare key fields
        incoming_name = incoming.get("label") or incoming.get("name") or existing.external_id
        incoming_label = incoming.get("label") or incoming_name

        checks = [
            (existing.name, incoming_name),
            (existing.label, incoming_label),
            (existing.object_type, incoming.get("objectType") or "deals"),
            (existing.is_default, bool(incoming.get("default"))),
            (existing.is_active, not bool(incoming.get("archived"))),
        ]

        for existing_val, incoming_val in checks:
            if existing_val is None and incoming_val is None:
                continue
            if existing_val != incoming_val:
                return True

        # Check display order
        incoming_display_order = incoming.get("displayOrder")
        if incoming_display_order is not None:
            try:
                if existing.display_order != int(incoming_display_order):
                    return True
            except (TypeError, ValueError):
                pass

        # Check source_modified_at
        incoming_modified = self._parse_date(incoming.get("updatedAt"))
        return bool(
            incoming_modified
            and existing.source_modified_at
            and incoming_modified > existing.source_modified_at
        )

    async def _soft_delete_pipeline_stages(
        self,
        integration_id: UUID,
        pipeline_id: UUID,
        now: datetime,
    ) -> None:
        """Soft-delete all stages for a deleted pipeline.

        Args:
            integration_id: Integration ID
            pipeline_id: Pipeline ID
            now: Current timestamp
        """
        stmt = (
            select(CRMDealStage)
            .where(CRMDealStage.integration_id == integration_id)  # type: ignore[arg-type]
            .where(CRMDealStage.pipeline_id == pipeline_id)  # type: ignore[arg-type]
            .where(CRMDealStage.is_deleted.is_(False))  # type: ignore[attr-defined]
        )
        result = await self.db.execute(stmt)
        stages = result.scalars().all()

        for stage in stages:
            stage.is_deleted = True
            stage.last_seen_at = now
            self.db.add(stage)

    async def enrich_deals_with_pipeline_metadata(
        self,
        integration_id: UUID,
    ) -> int:
        """Enrich deals with pipeline and stage names after pipeline sync.

        Updates deals that have pipeline_external_id or stage_external_id
        but are missing the denormalized name fields. This happens when
        deals are synced before pipelines exist in the database.

        Args:
            integration_id: Integration UUID to scope the enrichment

        Returns:
            Number of deals updated with enriched metadata
        """
        logger.info(
            "Enriching deals with pipeline/stage metadata for integration %s",
            integration_id,
        )

        # Load all pipelines and stages for this integration
        pipeline_map = await self._load_pipeline_map(integration_id)
        stage_map = await self._load_stage_map(integration_id)

        if not pipeline_map and not stage_map:
            logger.info("No pipelines or stages found - skipping deal enrichment")
            return 0

        # Find deals that need enrichment
        # We'll fetch deals with pipeline_external_id or stage_external_id set
        # and filter in memory for those missing names (more reliable than complex SQLAlchemy)
        stmt = (
            select(CRMDeal)
            .where(CRMDeal.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMDeal.integration_id == integration_id)  # type: ignore[arg-type]
            .where(CRMDeal.is_deleted.is_(False))  # type: ignore[attr-defined]
        )

        result = await self.db.execute(stmt)
        all_deals = list(result.scalars().all())

        # Filter to deals that need enrichment
        deals_to_enrich = [
            deal
            for deal in all_deals
            if (deal.pipeline_external_id and not deal.pipeline_name)
            or (deal.stage_external_id and not deal.stage_name)
        ]

        if not deals_to_enrich:
            logger.info("No deals found needing pipeline/stage enrichment")
            return 0

        deals_updated = 0

        for deal in deals_to_enrich:
            updated = False

            # Enrich pipeline metadata
            if deal.pipeline_external_id and not deal.pipeline_name:
                pipeline = pipeline_map.get(deal.pipeline_external_id)
                if pipeline:
                    deal.pipeline_id = pipeline.pipeline_id
                    deal.pipeline_name = pipeline.name
                    deal.pipeline_label = pipeline.label
                    updated = True
                    logger.debug(
                        "Enriched deal %s with pipeline: %s",
                        deal.deal_id,
                        pipeline.name,
                    )

            # Enrich stage metadata
            if deal.stage_external_id and not deal.stage_name:
                stage = stage_map.get(deal.stage_external_id)
                if stage:
                    deal.stage_id = stage.stage_id
                    deal.stage_name = stage.name
                    deal.stage_label = stage.label
                    # Also populate probability if not already set
                    if stage.probability is not None and deal.probability is None:
                        deal.probability = stage.probability
                    updated = True
                    logger.debug(
                        "Enriched deal %s with stage: %s",
                        deal.deal_id,
                        stage.name,
                    )

            if updated:
                self.db.add(deal)
                deals_updated += 1

        # Commit all enriched deals
        if deals_updated > 0:
            await self.db.commit()
            logger.info(
                "Successfully enriched %d deals with pipeline/stage metadata",
                deals_updated,
            )

        return deals_updated

    def _transform_record(self, raw: dict[str, Any], _provider: str) -> CRMPipeline:
        """Transform provider-specific pipeline data to CRMPipeline model.

        Args:
            raw: Raw pipeline record from provider
            _provider: Provider name (unused, kept for interface compatibility)

        Returns:
            Transformed CRMPipeline instance

        Note:
            Simplified transformation - actual logic in _update_pipeline_record.
        """
        external_id = self._extract_external_id(raw)
        return CRMPipeline(
            organisation_id=self.organisation_id,
            integration_id=UUID("00000000-0000-0000-0000-000000000000"),
            external_id=external_id or "",
            name=raw.get("label") or raw.get("name") or "",
        )

    def _update_pipeline_record(
        self,
        record: CRMPipeline,
        raw: dict[str, Any],
        _provider: str,
    ) -> None:
        """Update CRMPipeline record with data from provider.

        Args:
            record: Existing CRMPipeline instance to update
            raw: Raw data from provider
            _provider: Provider name (unused, kept for interface compatibility)
        """
        # Basic fields
        record.name = raw.get("label") or raw.get("name") or record.external_id
        record.label = raw.get("label") or record.name

        # Strip whitespace from labels
        if record.name:
            record.name = record.name.strip()
        if record.label:
            record.label = record.label.strip()

        record.object_type = raw.get("objectType") or "deals"

        # Status fields
        record.is_default = bool(raw.get("default"))
        record.is_active = not bool(raw.get("archived"))

        # Display order
        display_order = raw.get("displayOrder")
        if display_order is not None:
            try:
                record.display_order = int(display_order)
            except (TypeError, ValueError):
                record.display_order = None

        # Timestamps
        record.source_modified_at = self._parse_date(raw.get("updatedAt"))

        # Store raw data
        record.properties = raw
        record.data_payload = raw

    async def _sync_stages_for_pipeline(
        self,
        integration: KnowledgeIntegration,
        pipeline_record: CRMPipeline,
        stages_payload: Any,
    ) -> int:
        """Sync stages for a pipeline with soft-delete for orphaned stages.

        Args:
            integration: KnowledgeIntegration instance
            pipeline_record: Parent pipeline record
            stages_payload: Raw stages data from provider

        Returns:
            Number of stages processed
        """
        if not isinstance(stages_payload, list):
            return 0

        stage_items = [s for s in stages_payload if isinstance(s, dict)]
        if not stage_items:
            return 0

        # Load existing stages for this pipeline
        stage_external_ids = [
            str(ext_id)
            for stage in stage_items
            if (ext_id := self._extract_stage_external_id(stage)) is not None
        ]

        existing_stages = await self._load_existing_stages(
            integration.integration_id,
            stage_external_ids,
        )

        now = utcnow()
        seen_external_ids: set[str] = set()
        stages_processed = 0

        for stage in stage_items:
            try:
                stage_external_id = self._extract_stage_external_id(stage)
                if not stage_external_id:
                    continue

                seen_external_ids.add(stage_external_id)

                # Get or create stage record
                record = existing_stages.get(stage_external_id)
                if not record:
                    record = CRMDealStage(
                        organisation_id=self.organisation_id,
                        integration_id=integration.integration_id,
                        external_id=stage_external_id,
                        pipeline_id=pipeline_record.pipeline_id,
                    )

                # Update stage record
                self._update_stage_record(record, stage, pipeline_record)

                # Update metadata
                record.synced_at = now
                record.last_seen_at = now
                record.is_deleted = False

                self.db.add(record)
                stages_processed += 1

            except Exception as exc:
                logger.error(
                    "Failed to sync stage %s for pipeline %s: %s",
                    stage.get("id"),
                    pipeline_record.external_id,
                    exc,
                    exc_info=True,
                )

        # Soft-delete stages missing in latest payload
        for ext_id, record in existing_stages.items():
            if ext_id not in seen_external_ids:
                record.is_deleted = True
                record.last_seen_at = now
                self.db.add(record)

        return stages_processed

    async def _load_existing_stages(
        self,
        integration_id: UUID,
        external_ids: list[str],
    ) -> dict[str, CRMDealStage]:
        """Load existing stages by external IDs.

        Args:
            integration_id: Integration ID
            external_ids: List of stage external IDs

        Returns:
            Dictionary mapping external_id to CRMDealStage
        """
        if not external_ids:
            return {}

        stmt = (
            select(CRMDealStage)
            .where(CRMDealStage.integration_id == integration_id)  # type: ignore[arg-type]
            .where(CRMDealStage.external_id.in_(external_ids))  # type: ignore[attr-defined]
        )
        result = await self.db.execute(stmt)
        return {row.external_id: row for row in result.scalars().all()}

    async def _load_pipeline_map(self, integration_id: UUID) -> dict[str, CRMPipeline]:
        """Load pipeline mapping by external ID for enrichment.

        Args:
            integration_id: Integration ID

        Returns:
            Dictionary mapping external_id to CRMPipeline
        """
        stmt = (
            select(CRMPipeline)
            .where(CRMPipeline.integration_id == integration_id)  # type: ignore[arg-type]
            .where(CRMPipeline.is_deleted.is_(False))  # type: ignore[attr-defined]
        )
        result = await self.db.execute(stmt)
        return {row.external_id: row for row in result.scalars().all()}

    async def _load_stage_map(self, integration_id: UUID) -> dict[str, CRMDealStage]:
        """Load stage mapping by external ID for enrichment.

        Args:
            integration_id: Integration ID

        Returns:
            Dictionary mapping external_id to CRMDealStage
        """
        stmt = (
            select(CRMDealStage)
            .where(CRMDealStage.integration_id == integration_id)  # type: ignore[arg-type]
            .where(CRMDealStage.is_deleted.is_(False))  # type: ignore[attr-defined]
        )
        result = await self.db.execute(stmt)
        return {row.external_id: row for row in result.scalars().all()}

    def _update_stage_record(
        self,
        record: CRMDealStage,
        raw: dict[str, Any],
        pipeline_record: CRMPipeline,
    ) -> None:
        """Update CRMDealStage record with data from provider.

        Args:
            record: Existing CRMDealStage instance to update
            raw: Raw stage data from provider
            pipeline_record: Parent pipeline record
        """
        # Ensure pipeline association
        record.pipeline_id = pipeline_record.pipeline_id

        # Basic fields
        stage_external_id = self._extract_stage_external_id(raw)
        record.name = raw.get("label") or raw.get("displayName") or stage_external_id or ""
        record.label = raw.get("label")

        # Strip whitespace from labels
        if record.name:
            record.name = record.name.strip()
        if record.label:
            record.label = record.label.strip()

        record.stage_key = raw.get("stageId") or raw.get("id")

        # Probability (can be in metadata or root)
        metadata_raw = raw.get("metadata")
        metadata = metadata_raw if isinstance(metadata_raw, dict) else {}
        probability = metadata.get("probability", raw.get("probability"))
        record.probability = self._normalize_probability(probability)

        # Stage state (open, won, lost) - check metadata.isClosed first
        self._set_stage_state_from_metadata(record, metadata, stage_external_id)

        # Forecast category
        record.forecast_category = (
            metadata.get("forecastCategory") if metadata else raw.get("forecastCategory")
        )

        # Display order
        display_order = raw.get("displayOrder")
        if display_order is not None:
            try:
                record.display_order = int(display_order)
            except (TypeError, ValueError):
                record.display_order = None

        # Timestamps
        record.source_modified_at = self._parse_date(raw.get("updatedAt"))

        # Store raw data
        record.data_payload = raw

    def _extract_external_id(self, raw: dict[str, Any]) -> str | None:
        """Extract external ID from pipeline data.

        Args:
            raw: Raw pipeline record

        Returns:
            External ID or None
        """
        return raw.get("id") or raw.get("pipelineId")

    def _extract_stage_external_id(self, raw: dict[str, Any]) -> str | None:
        """Extract external ID from stage data.

        Args:
            raw: Raw stage record

        Returns:
            External ID or None
        """
        return raw.get("id") or raw.get("stageId")

    def _determine_stage_state(self, stage_external_id: str | None) -> CRMStageState:
        """Determine stage state from external ID.

        Args:
            stage_external_id: Stage external ID

        Returns:
            Stage state (open, won, or lost)
        """
        if not stage_external_id:
            return CRMStageState.OPEN

        slug = stage_external_id.lower()
        if "won" in slug:
            return CRMStageState.WON
        if "lost" in slug:
            return CRMStageState.LOST

        return CRMStageState.OPEN

    def _set_stage_state_from_metadata(
        self,
        record: CRMDealStage,
        metadata: dict[str, Any],
        stage_external_id: str | None,
    ) -> None:
        """Set stage state and is_closed from metadata or fallback to slug matching.

        Args:
            record: Stage record to update
            metadata: Metadata dictionary from provider
            stage_external_id: Stage external ID for fallback matching
        """
        is_closed_str = metadata.get("isClosed")
        if is_closed_str is not None:
            # HubSpot returns "true"/"false" strings
            record.is_closed = self._is_truthy(is_closed_str)
            if record.is_closed:
                # Use probability to distinguish won vs lost
                prob = record.probability or 0
                record.stage_state = CRMStageState.WON if prob >= 50 else CRMStageState.LOST
            else:
                record.stage_state = CRMStageState.OPEN
        else:
            # Fallback to stage ID slug matching
            record.stage_state = self._determine_stage_state(stage_external_id)
            record.is_closed = record.stage_state != CRMStageState.OPEN

    def _is_truthy(self, value: Any) -> bool:
        """Interpret provider boolean-like values consistently.

        Args:
            value: Value to interpret as boolean

        Returns:
            True if value represents a truthy value
        """
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        if isinstance(value, int | float):
            return value != 0
        s = str(value).strip().lower()
        return s in {"true", "yes", "1", "y", "t"}

    def _normalize_probability(self, value: Any) -> float | None:
        """Normalize probability to 0-100 scale.

        Args:
            value: Probability value (0-1 or 0-100)

        Returns:
            Normalized probability (0-100) or None
        """
        numeric = self._safe_float(value)
        if numeric is None:
            return None

        # Convert 0-1 range to 0-100
        if 0 <= numeric <= 1:
            return numeric * 100

        return numeric

    def _safe_float(self, value: Any) -> float | None:
        """Safely convert value to float.

        Args:
            value: Value to convert

        Returns:
            Float value or None
        """
        if value in (None, ""):
            return None

        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _parse_date(self, value: Any) -> datetime | None:
        """Parse datetime value from various formats.

        Handles:
        - None/empty values
        - datetime objects
        - Unix timestamps (milliseconds)
        - ISO 8601 strings (e.g., "2025-10-03T13:37:32.290Z")

        Args:
            value: Date value (datetime, timestamp, or string)

        Returns:
            Parsed datetime or None
        """
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, int | float):
            try:
                from datetime import UTC

                return datetime.fromtimestamp(float(value) / 1000, tz=UTC)
            except Exception:
                return None
        if isinstance(value, str):
            try:
                from typing import cast

                from dateutil import parser as date_parser  # type: ignore[import-untyped]

                return cast(datetime | None, date_parser.isoparse(value))
            except Exception:
                return None

        return None
