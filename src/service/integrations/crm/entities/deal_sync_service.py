"""Deal entity sync service for CRM integrations."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from src.domain.models.organisation_tables.base_mixins import utcnow
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.crm import (
    CRMDeal,
    CRMDealParticipant,
    CRMDealStage,
    CRMDealStageHistory,
    CRMDealStatus,
    CRMParticipantType,
    CRMPipeline,
)
from src.infra.utils.logger import get_logger
from src.service.crm.deal_timeline_sync_service import (
    build_deal_created_timeline_event,
    build_stage_change_timeline_event,
)
from src.service.crm.timeline_event_source_resolver import resolve_crm_event_source
from src.service.integrations.core import BaseSyncService
from src.workers.integrations_worker.utils.sync_metadata import (
    SyncAction,
    SyncActionCounts,
    extract_sync_action,
)

from .deal_enrichment_service import DealEnrichmentService

logger = get_logger(__name__)


class DealSyncService(BaseSyncService[CRMDeal]):
    """Service for syncing CRM deal data.

    Handles synchronization of deal records with their related data:
    - Deal participants (contacts, companies)
    - Stage history tracking
    - Pipeline and stage associations
    """

    async def sync(
        self,
        *,
        integration: KnowledgeIntegration,
        records: Iterable[dict[str, Any]],
        provider: str,
    ) -> dict[str, Any]:
        """Sync deal records from external provider.

        Args:
            integration: KnowledgeIntegration instance
            records: Raw deal records from provider API
            provider: Provider name (e.g., 'hubspot')

        Returns:
            Dictionary with sync statistics including participants and stage history
        """
        result = self._init_result()
        deal_list = [d for d in records if isinstance(d, dict)]
        self._log_sync_start("deals", provider, len(deal_list))

        if not deal_list:
            result.complete()
            return {
                "deals": 0,
                "participants": 0,
                "stage_history": 0,
                **result.to_dict(),
            }

        # Load existing deals
        deal_external_ids = [str(d.get("id")) for d in deal_list if d.get("id")]
        existing = await self._load_existing_by_external_ids(
            CRMDeal,
            deal_external_ids,
            integration.integration_id,
        )

        # Load reference data
        pipeline_map = await self._load_pipeline_map(integration.integration_id)
        stage_map = await self._load_stage_map(integration.integration_id)

        # Resolve event source for timeline events
        integration_type = getattr(integration, "integration_type", None)
        event_source = resolve_crm_event_source(integration_type)

        now = utcnow()
        participants_to_upsert: list[tuple[CRMDeal, dict[str, Any], CRMParticipantType]] = []
        history_entries: list[dict[str, Any]] = []
        deals_processed = 0
        deals_to_enrich: list[CRMDeal] = []

        for deal in deal_list:
            try:
                external_id = deal.get("id")
                if not external_id:
                    result.increment_failed()
                    result.add_error(f"Missing external ID for deal: {deal}")
                    continue

                # Get or create record
                record = existing.get(external_id)
                is_new = record is None

                if not record:
                    record = CRMDeal(
                        organisation_id=self.organisation_id,
                        integration_id=integration.integration_id,
                        external_id=external_id,
                    )

                # Transform and update record
                self._update_deal_record(record, deal, provider, pipeline_map, stage_map)

                # Update metadata
                record.synced_at = now
                record.last_seen_at = now
                record.is_deleted = bool(deal.get("archived"))

                self.db.add(record)
                deals_to_enrich.append(record)
                deals_processed += 1
                result.increment_processed()

                if is_new:
                    result.increment_created()
                    # Create DEAL_CREATED timeline event for new deals
                    timeline_event = build_deal_created_timeline_event(
                        organisation_id=self.organisation_id,
                        deal=record,
                        event_source=event_source,
                    )
                    self.db.add(timeline_event)
                else:
                    result.increment_updated()

                # Extract related data
                associations = deal.get("associations") or {}

                participants_to_upsert.extend(self._extract_participants(record, associations))
                deal_history_entries, latest_stage_entry = self._extract_stage_history(
                    record, deal, stage_map
                )
                history_entries.extend(deal_history_entries)

                if latest_stage_entry:
                    record.stage_entered_at = latest_stage_entry["entered_at"]
                    record.stage_updated_at = latest_stage_entry["entered_at"]

            except Exception as exc:
                self._log_error("deal", deal.get("id"), exc)
                result.increment_failed()
                result.add_error(str(exc))

        if deals_to_enrich:
            enricher = DealEnrichmentService(db=self.db, organisation_id=self.organisation_id)
            await enricher.enrich_deals(deals_to_enrich)

        # Commit deals
        await self.db.commit()

        # Sync related entities
        participants_synced = await self._upsert_participants(integration, participants_to_upsert)

        # Enrich participants with contact/company details
        if deals_to_enrich:
            deal_ids_for_enrichment = [d.deal_id for d in deals_to_enrich]
            participants_enriched = await enricher.enrich_participants(
                integration.integration_id, deal_ids=deal_ids_for_enrichment
            )
            logger.debug(
                "Enriched deal participants",
                participants_enriched=participants_enriched,
                integration_id=str(integration.integration_id),
            )

        history_synced = await self._upsert_stage_history(integration, history_entries)

        result.metadata.update(
            {
                "deals": deals_processed,
                "participants": participants_synced,
                "stage_history": history_synced,
            }
        )
        result.complete()
        self._log_sync_complete(result)

        return {
            "deals": deals_processed,
            "participants": participants_synced,
            "stage_history": history_synced,
            **result.to_dict(),
        }

    async def sync_incremental(
        self,
        *,
        integration: KnowledgeIntegration,
        records: Iterable[dict[str, Any]],
        provider: str,
    ) -> dict[str, Any]:
        """Sync deal records using incremental optimization.

        This method optimizes sync by:
        - Using database state to determine if records are new or existing
        - Running change detection to skip unchanged records
        - Preserving application-generated data (deal_summary_data)
        - Handling archived/deleted records via soft-delete

        The _sync_metadata.sync_action is used for:
        - DELETED: Soft-delete records (mark is_deleted=True)
        - Logging/observability of expected vs actual actions

        For ADDED/UPDATED actions, we trust database state (is_new) over
        sync_action because we don't have reliable per-record action info
        from Nango when fetching directly from CRM APIs.

        Args:
            integration: KnowledgeIntegration instance
            records: Raw deal records with optional _sync_metadata from fetcher
            provider: Provider name (e.g., 'hubspot', 'salesforce')

        Returns:
            Dictionary with sync statistics including action breakdown
        """
        result = self._init_result()
        deal_list = [d for d in records if isinstance(d, dict)]
        self._log_sync_start("deals (incremental)", provider, len(deal_list))

        if not deal_list:
            result.complete()
            return {
                "deals": 0,
                "participants": 0,
                "stage_history": 0,
                "skipped": 0,
                **result.to_dict(),
            }

        # Track action counts for logging (observability only)
        action_counts = SyncActionCounts()

        # Load existing deals for change detection and update
        deal_external_ids = [str(d.get("id")) for d in deal_list if d.get("id")]
        existing = await self._load_existing_by_external_ids(
            CRMDeal,
            deal_external_ids,
            integration.integration_id,
        )

        # Load reference data
        pipeline_map = await self._load_pipeline_map(integration.integration_id)
        stage_map = await self._load_stage_map(integration.integration_id)

        # Resolve event source for timeline events
        integration_type = getattr(integration, "integration_type", None)
        event_source = resolve_crm_event_source(integration_type)

        now = utcnow()
        participants_to_upsert: list[tuple[CRMDeal, dict[str, Any], CRMParticipantType]] = []
        history_entries: list[dict[str, Any]] = []
        deals_processed = 0
        deals_skipped = 0
        deals_to_enrich: list[CRMDeal] = []

        for deal in deal_list:
            try:
                external_id = deal.get("id")
                if not external_id:
                    result.increment_failed()
                    result.add_error(f"Missing external ID for deal: {deal}")
                    continue

                # Extract sync action for logging and deletion handling
                sync_action = extract_sync_action(deal)
                if sync_action:
                    action_counts.increment(sync_action)

                # Check database state - this is the source of truth
                record = existing.get(external_id)
                is_new = record is None

                # Check if record is archived/deleted
                is_archived = deal.get("archived", False) or deal.get("IsDeleted", False)

                # Handle deletion: archived records or DELETED sync_action
                if is_archived or sync_action == SyncAction.DELETED:
                    if record:
                        record.is_deleted = True
                        record.synced_at = now
                        record.last_seen_at = now
                        self.db.add(record)
                        result.increment_processed()
                        deals_processed += 1
                        logger.debug(
                            "Soft-deleted deal: external_id=%s",
                            external_id,
                        )
                    continue

                # Handle new records
                if is_new:
                    record = CRMDeal(
                        organisation_id=self.organisation_id,
                        integration_id=integration.integration_id,
                        external_id=external_id,
                    )
                    # For new records, no protection needed (no existing data)
                    self._update_deal_record(record, deal, provider, pipeline_map, stage_map)
                    record.synced_at = now
                    record.last_seen_at = now
                    record.is_deleted = False

                    self.db.add(record)
                    deals_to_enrich.append(record)
                    deals_processed += 1
                    result.increment_processed()
                    result.increment_created()

                    # Create DEAL_CREATED timeline event for new deals
                    timeline_event = build_deal_created_timeline_event(
                        organisation_id=self.organisation_id,
                        deal=record,
                        event_source=event_source,
                    )
                    self.db.add(timeline_event)

                else:
                    # Existing record - check for meaningful changes before updating
                    if self._has_meaningful_changes(record, deal):
                        # Use protection wrapper to preserve deal_summary_data
                        self._update_deal_record_with_protection(
                            record, deal, provider, pipeline_map, stage_map
                        )
                        record.synced_at = now
                        record.last_seen_at = now
                        record.is_deleted = False

                        self.db.add(record)
                        deals_to_enrich.append(record)
                        deals_processed += 1
                        result.increment_processed()
                        result.increment_updated()
                    else:
                        # No meaningful changes - skip database write
                        deals_skipped += 1
                        action_counts.unchanged += 1
                        logger.debug(
                            "Skipping unchanged deal: external_id=%s",
                            external_id,
                        )
                        continue

                # Extract related data for processed deals
                associations = deal.get("associations") or {}
                participants_to_upsert.extend(self._extract_participants(record, associations))
                deal_history_entries, latest_stage_entry = self._extract_stage_history(
                    record, deal, stage_map
                )
                history_entries.extend(deal_history_entries)

                if latest_stage_entry:
                    record.stage_entered_at = latest_stage_entry["entered_at"]
                    record.stage_updated_at = latest_stage_entry["entered_at"]

            except Exception as exc:
                self._log_error("deal", deal.get("id"), exc)
                result.increment_failed()
                result.add_error(str(exc))

        # Enrich only the deals that were actually modified
        if deals_to_enrich:
            enricher = DealEnrichmentService(db=self.db, organisation_id=self.organisation_id)
            await enricher.enrich_deals(deals_to_enrich)

        # Commit deals
        await self.db.commit()

        # Sync related entities only for modified deals
        participants_synced = await self._upsert_participants(integration, participants_to_upsert)

        # Enrich participants with contact/company details
        if deals_to_enrich:
            deal_ids_for_enrichment = [d.deal_id for d in deals_to_enrich]
            participants_enriched = await enricher.enrich_participants(
                integration.integration_id, deal_ids=deal_ids_for_enrichment
            )
            logger.debug(
                "Enriched deal participants (incremental)",
                participants_enriched=participants_enriched,
                integration_id=str(integration.integration_id),
            )

        history_synced = await self._upsert_stage_history(integration, history_entries)

        # Log incremental sync summary
        logger.info(
            "Incremental deal sync completed: processed=%d skipped=%d "
            "(added=%d updated=%d deleted=%d) participants=%d history=%d",
            deals_processed,
            deals_skipped,
            action_counts.added,
            action_counts.updated,
            action_counts.deleted,
            participants_synced,
            history_synced,
        )

        result.metadata.update(
            {
                "deals": deals_processed,
                "participants": participants_synced,
                "stage_history": history_synced,
                "skipped": deals_skipped,
                "action_counts": action_counts.to_dict(),
            }
        )
        result.complete()
        self._log_sync_complete(result)

        return {
            "deals": deals_processed,
            "participants": participants_synced,
            "stage_history": history_synced,
            "skipped": deals_skipped,
            "action_counts": action_counts.to_dict(),
            **result.to_dict(),
        }

    def _has_meaningful_changes(
        self,
        existing: CRMDeal,
        incoming: dict[str, Any],
    ) -> bool:
        """Check if incoming data has meaningful changes compared to existing record.

        Compares key fields that typically matter for deal updates.
        Returns True if any significant field differs, False if record is unchanged.

        Args:
            existing: Existing CRMDeal record from database
            incoming: New deal data from provider

        Returns:
            True if changes detected, False otherwise
        """
        properties = incoming.get("properties") or {}

        # Check source_modified_at as primary indicator
        incoming_modified = self._parse_date(incoming.get("updatedAt"))
        if (
            incoming_modified
            and existing.source_modified_at
            and incoming_modified > existing.source_modified_at
        ):
            return True

        # Compare key fields
        checks = [
            (existing.name, properties.get("dealname") or incoming.get("name")),
            (existing.amount, self._safe_float(properties.get("amount"))),
            (existing.stage_external_id, properties.get("dealstage")),
            (existing.pipeline_external_id, properties.get("pipeline")),
            (existing.owner_external_id, properties.get("hubspot_owner_id")),
        ]

        # Compare close_date
        existing_close = existing.close_date.isoformat() if existing.close_date else None
        incoming_close_date = self._parse_date(properties.get("closedate"))
        incoming_close = incoming_close_date.isoformat() if incoming_close_date else None
        if existing_close != incoming_close:
            return True

        # Check key field comparisons
        for existing_val, incoming_val in checks:
            # Normalize None comparisons
            if existing_val is None and incoming_val is None:
                continue
            if existing_val != incoming_val:
                return True

        return False

    def _update_deal_record_with_protection(
        self,
        record: CRMDeal,
        raw: dict[str, Any],
        provider: str,
        pipeline_map: dict[str, CRMPipeline],
        stage_map: dict[str, CRMDealStage],
    ) -> None:
        """Update CRMDeal record while preserving application-generated data.

        This wrapper around _update_deal_record ensures that fields generated
        by our application (like deal_summary_data) are never overwritten by
        CRM sync data.

        Args:
            record: Existing CRMDeal instance to update
            raw: Raw data from provider
            provider: Provider name
            pipeline_map: Mapping of pipeline external IDs to records
            stage_map: Mapping of stage external IDs to records
        """
        # Preserve application-generated fields before update
        preserved_deal_summary = record.deal_summary_data

        # Perform the standard update
        self._update_deal_record(record, raw, provider, pipeline_map, stage_map)

        # Restore preserved fields (only if they had values)
        if preserved_deal_summary is not None:
            record.deal_summary_data = preserved_deal_summary

    def _transform_record(self, raw: dict[str, Any], _provider: str) -> CRMDeal:
        """Transform provider-specific deal data to CRMDeal model.

        Args:
            raw: Raw deal record from provider
            _provider: Provider name (unused, kept for interface compatibility)

        Returns:
            Transformed CRMDeal instance
        """
        # Simplified transformation - actual logic in _update_deal_record
        return CRMDeal(
            organisation_id=self.organisation_id,
            integration_id=UUID("00000000-0000-0000-0000-000000000000"),
            external_id=str(raw.get("id", "")),
            name=raw.get("properties", {}).get("dealname"),
        )

    def _update_deal_record(
        self,
        record: CRMDeal,
        raw: dict[str, Any],
        _provider: str,
        pipeline_map: dict[str, CRMPipeline],
        stage_map: dict[str, CRMDealStage],
    ) -> None:
        """Update CRMDeal record with data from provider.

        Args:
            record: Existing CRMDeal instance to update
            raw: Raw data from provider
            _provider: Provider name (unused, kept for interface compatibility)
            pipeline_map: Mapping of pipeline external IDs to records
            stage_map: Mapping of stage external IDs to records
        """
        properties = raw.get("properties") or {}
        associations = raw.get("associations") or {}
        updated_at = self._parse_date(raw.get("updatedAt"))
        created_at = self._parse_date(raw.get("createdAt"))

        # Update all record sections
        self._update_pipeline_and_stage(record, properties, pipeline_map, stage_map)
        self._update_basic_fields(record, raw, properties)
        self._update_financial_fields(record, properties)
        self._update_forecasting_fields(record, properties)
        self._update_owner_fields(record, properties)
        self._update_audit_fields(record, properties)
        self._update_associations(record, associations)
        self._update_date_fields(record, properties, updated_at, created_at)
        self._update_raw_data(record, raw, properties, associations, updated_at)

    def _update_pipeline_and_stage(
        self,
        record: CRMDeal,
        properties: dict[str, Any],
        pipeline_map: dict[str, CRMPipeline],
        stage_map: dict[str, CRMDealStage],
    ) -> None:
        """Update pipeline and stage associations."""
        pipeline_external = properties.get("pipeline")
        stage_external = properties.get("dealstage")

        # Update pipeline
        pipeline_record = pipeline_map.get(str(pipeline_external)) if pipeline_external else None
        if pipeline_record:
            record.pipeline_id = pipeline_record.pipeline_id
            record.pipeline_external_id = pipeline_record.external_id
            record.pipeline_name = pipeline_record.name
            record.pipeline_label = pipeline_record.label
        elif pipeline_external:
            record.pipeline_id = None
            record.pipeline_external_id = pipeline_external
            record.pipeline_name = None
            record.pipeline_label = None

        # Update stage
        stage_record = stage_map.get(str(stage_external)) if stage_external else None
        if stage_record:
            record.stage_id = stage_record.stage_id
            record.stage_external_id = stage_record.external_id
            record.stage_name = stage_record.name
            record.stage_label = stage_record.label
        elif stage_external:
            record.stage_id = None
            record.stage_external_id = stage_external
            record.stage_name = None
            record.stage_label = None

    def _update_basic_fields(
        self, record: CRMDeal, raw: dict[str, Any], properties: dict[str, Any]
    ) -> None:
        """Update basic deal fields."""
        record.name = properties.get("dealname") or raw.get("name")
        record.description = properties.get("description")
        record.lead_source = properties.get("lead_source") or properties.get("hs_lead_source")
        record.deal_type = properties.get("dealtype")
        record.priority = properties.get("hs_priority")

        # Extract CRM URL from top-level raw data (provider-agnostic)
        crm_url = raw.get("url")
        if crm_url and isinstance(crm_url, str):
            record.crm_url = crm_url

    def _update_financial_fields(self, record: CRMDeal, properties: dict[str, Any]) -> None:
        """Update financial fields."""
        record.amount = self._safe_float(properties.get("amount"))
        record.amount_home_currency = self._safe_float(properties.get("amount_in_home_currency"))
        # Extract currency from multiple possible fields
        currency = (
            properties.get("deal_currency_code")
            or properties.get("currency")
            or properties.get("hs_deal_currency_code")
        )
        # Only set if we have a non-empty value
        if currency and isinstance(currency, str) and len(currency.strip()) > 0:
            record.currency = currency.strip().upper()  # Normalize to uppercase ISO codes

    def _update_forecasting_fields(self, record: CRMDeal, properties: dict[str, Any]) -> None:
        """Update forecasting and probability fields."""
        probability = properties.get("hs_deal_stage_probability") or properties.get(
            "hs_forecast_probability"
        )
        record.probability = self._normalize_probability(probability)
        record.predicted_amount = self._safe_float(properties.get("hs_forecast_amount"))
        # Start with stage-derived status
        record.deal_status = self._determine_deal_status(properties.get("dealstage"))
        record.forecast_category = properties.get("hs_forecast_category")

        # Prefer explicit HubSpot closed flags when available
        if self._is_truthy(properties.get("hs_is_closed_won")):
            record.deal_status = CRMDealStatus.WON
        elif self._is_truthy(properties.get("hs_is_closed_lost")):
            record.deal_status = CRMDealStatus.LOST
        elif self._is_truthy(properties.get("hs_is_closed")):
            record.deal_status = CRMDealStatus.CLOSED

    def _update_owner_fields(self, record: CRMDeal, properties: dict[str, Any]) -> None:
        """Update owner information fields."""
        owner_id = properties.get("hubspot_owner_id")
        if owner_id is not None:
            record.owner_external_id = owner_id

        owner_email = properties.get("hubspot_owner_email")
        if isinstance(owner_email, str):
            record.owner_email = owner_email

        owner_name = properties.get("hubspot_owner_name")
        if isinstance(owner_name, str):
            record.owner_name = owner_name

    def _update_audit_fields(self, record: CRMDeal, properties: dict[str, Any]) -> None:
        """Update audit and count-oriented fields."""
        created_by = properties.get("hs_created_by_user_id")
        if created_by is not None:
            record.created_by_user_external_id = str(created_by)

        updated_by = properties.get("hs_updated_by_user_id")
        if updated_by is not None:
            record.updated_by_user_external_id = str(updated_by)

        record.days_to_close = self._safe_int(properties.get("days_to_close"))
        record.associated_contact_count = self._safe_int(properties.get("num_associated_contacts"))
        record.associated_company_count = self._safe_int(properties.get("num_associated_companies"))
        record.note_count = self._safe_int(properties.get("num_notes"))

    def _update_associations(self, record: CRMDeal, associations: dict[str, Any]) -> None:
        """Update association references and counts from HubSpot associations.

        HubSpot sends associations as:
        {
            "companies": {"results": [{"id": "123", ...}, ...]},
            "contacts": {"results": [{"id": "456", ...}, ...]}
        }
        """
        # Extract company associations
        companies_data = associations.get("companies", {})
        if isinstance(companies_data, dict):
            companies_list = companies_data.get("results", [])
        else:
            companies_list = []

        company_id = self._first_association_id(companies_list)
        record.company_external_id = company_id
        if company_id is None:
            record.company_name = None
            record.company_domain = None

        # Set company count from associations if we have them
        if companies_list:
            record.associated_company_count = len(companies_list)

        # Extract contact associations
        contacts_data = associations.get("contacts", {})
        contacts_list = contacts_data.get("results", []) if isinstance(contacts_data, dict) else []

        contact_id = self._first_association_id(contacts_list)
        record.primary_contact_external_id = contact_id
        if contact_id is None:
            record.primary_contact_name = None
            record.primary_contact_email = None

    def _update_date_fields(
        self,
        record: CRMDeal,
        properties: dict[str, Any],
        updated_at: datetime | None,
        created_at: datetime | None,
    ) -> None:
        """Update date-related fields."""
        # Parse all date fields from properties
        close_date = self._parse_date(properties.get("closedate"))
        closed_won_date = self._parse_date(properties.get("hs_closed_won_date"))
        closed_lost_date = self._parse_date(properties.get("hs_closed_lost_date"))
        expected_close = self._parse_date(properties.get("hs_expected_close_date"))

        # Set the parsed values
        record.close_date = close_date
        record.closed_won_at = closed_won_date
        record.closed_lost_at = closed_lost_date
        record.expected_close_date = expected_close

        # Set closed reason (prefer won reason, then lost reason, then generic)
        record.closed_reason = (
            properties.get("closed_won_reason")
            or properties.get("closed_lost_reason")
            or properties.get("hs_closed_reason")
        )

        # Activity and modification dates
        record.last_activity_at = (
            self._parse_date(properties.get("lastactivitydate"))
            or self._parse_date(properties.get("hs_lastmodifieddate"))
            or updated_at
        )
        record.stage_updated_at = updated_at or self._parse_date(
            properties.get("hs_lastmodifieddate")
        )
        record.source_created_at = self._parse_date(properties.get("createdate")) or created_at
        record.source_modified_at = updated_at or self._parse_date(
            properties.get("hs_lastmodifieddate")
        )

        # Ensure consistency: for WON/LOST deals, fill in missing dates
        self._ensure_closed_date_consistency(record)

    def _update_raw_data(
        self,
        record: CRMDeal,
        raw: dict[str, Any],
        properties: dict[str, Any],
        associations: dict[str, Any],
        updated_at: datetime | None,
    ) -> None:
        """Update raw data storage fields."""
        record.properties = {k: v for k, v in properties.items() if v is not None}
        record.metrics = self._extract_metrics(properties)
        record.associations_payload = associations
        record.source_modified_at = updated_at or self._parse_date(
            properties.get("hs_lastmodifieddate")
        )
        record.data_payload = raw

    def _ensure_closed_date_consistency(self, record: CRMDeal) -> None:
        """Ensure consistency between close_date and closed_won_at/closed_lost_at.

        For closed deals, if one date is missing but the other exists, copy it.
        This handles cases where HubSpot provides partial date information.
        """
        status = record.deal_status

        if status == CRMDealStatus.WON:
            # For won deals, sync close_date and closed_won_at
            if record.close_date is None and record.closed_won_at is not None:
                record.close_date = record.closed_won_at
            elif record.closed_won_at is None and record.close_date is not None:
                record.closed_won_at = record.close_date

        elif status == CRMDealStatus.LOST:
            # For lost deals, sync close_date and closed_lost_at
            if record.close_date is None and record.closed_lost_at is not None:
                record.close_date = record.closed_lost_at
            elif record.closed_lost_at is None and record.close_date is not None:
                record.closed_lost_at = record.close_date

        elif status == CRMDealStatus.CLOSED and record.close_date is None:
            # For generic closed status, try to infer from available dates
            record.close_date = record.closed_won_at or record.closed_lost_at

    async def _load_pipeline_map(self, integration_id: UUID) -> dict[str, CRMPipeline]:
        """Load pipeline mapping by external ID."""
        stmt = (
            select(CRMPipeline)
            .where(CRMPipeline.integration_id == integration_id)  # type: ignore[arg-type]
            .where(CRMPipeline.is_deleted.is_(False))  # type: ignore[attr-defined]
        )
        result = await self.db.execute(stmt)
        return {row.external_id: row for row in result.scalars().all()}

    async def _load_stage_map(self, integration_id: UUID) -> dict[str, CRMDealStage]:
        """Load stage mapping by external ID."""
        stmt = (
            select(CRMDealStage)
            .where(CRMDealStage.integration_id == integration_id)  # type: ignore[arg-type]
            .where(CRMDealStage.is_deleted.is_(False))  # type: ignore[attr-defined]
        )
        result = await self.db.execute(stmt)
        return {row.external_id: row for row in result.scalars().all()}

    async def _upsert_participants(
        self,
        integration: KnowledgeIntegration,
        participants: list[tuple[CRMDeal, dict[str, Any], CRMParticipantType]],
    ) -> int:
        """Upsert deal participants.

        Looks up existing participants by the unique constraint fields
        (deal_id, participant_type, related_external_id, relationship_role)
        to handle cases where external_id format may have changed.
        """
        if not participants:
            return 0

        now = utcnow()

        # Build lookup keys from unique constraint fields
        lookup_keys: list[tuple[UUID, str, str, str]] = []
        for deal_record, payload, participant_type in participants:
            related_ext_id = payload.get("related_external_id")
            relationship_role = payload.get("relationship_role") or ""
            if related_ext_id:
                lookup_keys.append(
                    (
                        deal_record.deal_id,
                        participant_type.value,
                        str(related_ext_id),
                        relationship_role,
                    )
                )

        # Query existing participants by unique constraint fields
        existing: dict[tuple[UUID, str, str, str], CRMDealParticipant] = {}
        if lookup_keys:
            deal_ids = list({k[0] for k in lookup_keys})
            stmt = (
                select(CRMDealParticipant)
                .where(CRMDealParticipant.integration_id == integration.integration_id)  # type: ignore[arg-type]
                .where(CRMDealParticipant.deal_id.in_(deal_ids))  # type: ignore[attr-defined]
            )
            result = await self.db.execute(stmt)
            for row in result.scalars().all():
                # Ensure participant_type is always an enum for lookup key
                participant_type_enum = (
                    row.participant_type
                    if isinstance(row.participant_type, CRMParticipantType)
                    else CRMParticipantType(row.participant_type)
                )
                key = (
                    row.deal_id,
                    participant_type_enum.value,
                    row.related_external_id,
                    row.relationship_role or "",
                )
                existing[key] = row

        processed = 0
        for deal_record, payload, participant_type in participants:
            external_id = payload.get("external_id")
            if not external_id:
                continue

            related_ext_id = payload.get("related_external_id")
            relationship_role = payload.get("relationship_role") or ""

            # Look up by unique constraint fields
            lookup_key = (
                deal_record.deal_id,
                participant_type.value,
                str(related_ext_id) if related_ext_id else "",
                relationship_role,
            )
            record = existing.get(lookup_key)

            if not record:
                record = CRMDealParticipant(
                    organisation_id=self.organisation_id,
                    integration_id=integration.integration_id,
                    external_id=external_id,
                    deal_id=deal_record.deal_id,
                )

            # Update external_id to new format if it changed
            record.external_id = external_id
            record.deal_id = deal_record.deal_id
            record.participant_type = participant_type
            record.relationship_role = relationship_role or None
            record.related_external_id = str(related_ext_id) if related_ext_id else ""
            record.related_local_id = None
            record.related_name = payload.get("related_name")
            record.related_email = payload.get("related_email")
            record.data_payload = payload.get("raw")
            record.synced_at = now
            record.last_seen_at = now
            record.source_modified_at = payload.get("source_modified_at")
            record.is_deleted = False

            self.db.add(record)
            processed += 1

        await self.db.commit()
        return processed

    async def _upsert_stage_history(
        self,
        integration: KnowledgeIntegration,
        history_entries: list[dict[str, Any]],
    ) -> int:
        """Upsert stage history entries and create timeline events."""
        if not history_entries:
            return 0

        now = utcnow()
        ext_ids = [entry["external_id"] for entry in history_entries if entry.get("external_id")]
        existing: dict[str, CRMDealStageHistory] = {}

        if ext_ids:
            stmt = (
                select(CRMDealStageHistory)
                .where(CRMDealStageHistory.integration_id == integration.integration_id)  # type: ignore[arg-type]
                .where(CRMDealStageHistory.external_id.in_(ext_ids))  # type: ignore[attr-defined]
            )
            result = await self.db.execute(stmt)
            existing = {row.external_id: row for row in result.scalars().all()}

        # Resolve event source for timeline events
        integration_type = getattr(integration, "integration_type", None)
        event_source = resolve_crm_event_source(integration_type)

        processed = 0
        for entry in history_entries:
            external_id = entry.get("external_id")
            if not external_id:
                continue

            record = existing.get(external_id)
            is_new_record = record is None

            if not record:
                record = CRMDealStageHistory(
                    organisation_id=self.organisation_id,
                    integration_id=integration.integration_id,
                    external_id=external_id,
                    deal_id=entry["deal_id"],
                )

            record.deal_id = entry["deal_id"]
            record.stage_id = entry.get("stage_id")
            record.stage_external_id = entry.get("stage_external_id")
            record.from_stage_external_id = entry.get("from_stage_external_id")

            entered_at_val = entry.get("entered_at")
            if isinstance(entered_at_val, datetime):
                record.entered_at = entered_at_val
            elif entered_at_val:
                record.entered_at = utcnow()
            else:
                record.entered_at = utcnow()

            exited_at_val = entry.get("exited_at")
            record.exited_at = exited_at_val if isinstance(exited_at_val, datetime) else None
            record.elapsed_seconds = entry.get("elapsed_seconds")
            record.amount_at_stage = entry.get("amount_at_stage")
            record.probability_at_stage = entry.get("probability_at_stage")
            record.source = entry.get("source")
            record.data_payload = entry.get("raw")
            record.synced_at = now
            record.last_seen_at = now
            record.source_modified_at = entry.get("source_modified_at")
            record.is_deleted = False

            self.db.add(record)
            processed += 1

            # Create STAGE_CHANGE timeline event for new records
            if is_new_record:
                timeline_event = build_stage_change_timeline_event(
                    organisation_id=self.organisation_id,
                    deal_id=entry["deal_id"],
                    history_entry=entry,
                    event_source=event_source,
                )
                self.db.add(timeline_event)

        await self.db.commit()
        return processed

    # Helper methods from original service
    def _extract_participants(
        self,
        deal_record: CRMDeal,
        associations: dict[str, Any],
    ) -> list[tuple[CRMDeal, dict[str, Any], CRMParticipantType]]:
        """Extract participant data from associations.

        HubSpot sends associations in the format:
        {"contacts": {"results": [{"id": "123", ...}]}, "companies": {"results": [...]}}

        We need to extract the 'results' array from each association type.
        """
        participants: list[tuple[CRMDeal, dict[str, Any], CRMParticipantType]] = []

        # Extract contacts - handle HubSpot's {"results": [...]} structure
        contacts_data = associations.get("contacts", {})
        contacts_list = contacts_data.get("results", []) if isinstance(contacts_data, dict) else []
        participants.extend(self._extract_contact_participants(deal_record, contacts_list))

        # Extract companies - handle HubSpot's {"results": [...]} structure
        companies_data = associations.get("companies", {})
        companies_list = (
            companies_data.get("results", []) if isinstance(companies_data, dict) else []
        )
        participants.extend(self._extract_company_participants(deal_record, companies_list))

        # Extract standard HubSpot collaborators and split owners
        participants.extend(self._extract_hubspot_owner_participants(deal_record))

        return participants

    def _extract_hubspot_owner_participants(
        self, deal_record: CRMDeal
    ) -> list[tuple[CRMDeal, dict[str, Any], CRMParticipantType]]:
        """Extract deal owner, collaborators, and split owners as user participants."""
        participants: list[tuple[CRMDeal, dict[str, Any], CRMParticipantType]] = []
        props = deal_record.properties or {}

        # Add the deal owner as a participant
        if deal_record.owner_external_id:
            participants.append(
                (
                    deal_record,
                    {
                        "external_id": f"deal_{deal_record.external_id}_owner_{deal_record.owner_external_id}",
                        "related_external_id": deal_record.owner_external_id,
                        "relationship_role": "owner",
                        "related_name": deal_record.owner_name,
                        "related_email": deal_record.owner_email,
                        "raw": {
                            "id": deal_record.owner_external_id,
                            "source": "deal_owner",
                        },
                    },
                    CRMParticipantType.USER,
                )
            )

        # Process Collaborators
        collaborators_str = props.get("hs_all_collaborator_owner_ids")
        if collaborators_str and isinstance(collaborators_str, str):
            for owner_id in collaborators_str.split(";"):
                owner_id = owner_id.strip()
                if not owner_id:
                    continue

                participants.append(
                    (
                        deal_record,
                        {
                            "external_id": f"deal_{deal_record.external_id}_collab_{owner_id}",
                            "related_external_id": owner_id,
                            "relationship_role": "collaborator",
                            "raw": {
                                "id": owner_id,
                                "source": "hs_all_collaborator_owner_ids",
                            },
                        },
                        CRMParticipantType.USER,
                    )
                )

        # Process Deal Splits
        splits_str = props.get("hs_all_deal_split_owner_ids")
        if splits_str and isinstance(splits_str, str):
            for owner_id in splits_str.split(";"):
                owner_id = owner_id.strip()
                if not owner_id:
                    continue

                participants.append(
                    (
                        deal_record,
                        {
                            "external_id": f"deal_{deal_record.external_id}_split_{owner_id}",
                            "related_external_id": owner_id,
                            "relationship_role": "split_owner",
                            "raw": {
                                "id": owner_id,
                                "source": "hs_all_deal_split_owner_ids",
                            },
                        },
                        CRMParticipantType.USER,
                    )
                )

        return participants

    def _extract_contact_participants(
        self, deal_record: CRMDeal, contacts: Any
    ) -> list[tuple[CRMDeal, dict[str, Any], CRMParticipantType]]:
        """Extract contact participants from associations.

        HubSpot may return multiple association entries for the same contact with different
        types (e.g., 'deal_to_contact' and 'deal_to_contact_unlabeled'). We deduplicate by
        keeping only the primary (labeled) association when both exist for the same entity.
        """
        if not isinstance(contacts, list):
            return []

        # Group contacts by ID and identify which have primary vs unlabeled associations
        contact_map: dict[str, dict[str, Any]] = {}
        for contact in contacts:
            if not isinstance(contact, dict):
                continue
            contact_id = contact.get("id")
            if not contact_id:
                continue

            assoc_type = contact.get("type") or "contact"
            is_unlabeled = "_unlabeled" in assoc_type

            # Keep primary (non-unlabeled) over unlabeled
            if contact_id not in contact_map or not is_unlabeled:
                contact_map[contact_id] = contact

        # Build participant list from deduplicated contacts
        participants: list[tuple[CRMDeal, dict[str, Any], CRMParticipantType]] = []
        for contact_id, contact in contact_map.items():
            assoc_type = contact.get("type") or "contact"
            participants.append(
                (
                    deal_record,
                    {
                        "external_id": f"deal_{deal_record.external_id}_contact_{contact_id}",
                        "related_external_id": contact_id,
                        "relationship_role": assoc_type,
                        "raw": contact,
                    },
                    CRMParticipantType.CONTACT,
                )
            )

        return participants

    def _extract_company_participants(
        self, deal_record: CRMDeal, companies: Any
    ) -> list[tuple[CRMDeal, dict[str, Any], CRMParticipantType]]:
        """Extract company participants from associations.

        HubSpot may return multiple association entries for the same company with different
        types (e.g., 'deal_to_company' and 'deal_to_company_unlabeled'). We deduplicate by
        keeping only the primary (labeled) association when both exist for the same entity.
        """
        if not isinstance(companies, list):
            return []

        # Group companies by ID and identify which have primary vs unlabeled associations
        company_map: dict[str, dict[str, Any]] = {}
        for company in companies:
            if not isinstance(company, dict):
                continue
            company_id = company.get("id")
            if not company_id:
                continue

            assoc_type = company.get("type") or "company"
            is_unlabeled = "_unlabeled" in assoc_type

            # Keep primary (non-unlabeled) over unlabeled
            if company_id not in company_map or not is_unlabeled:
                company_map[company_id] = company

        # Build participant list from deduplicated companies
        participants: list[tuple[CRMDeal, dict[str, Any], CRMParticipantType]] = []
        for company_id, company in company_map.items():
            assoc_type = company.get("type") or "company"
            participants.append(
                (
                    deal_record,
                    {
                        "external_id": f"deal_{deal_record.external_id}_company_{company_id}",
                        "related_external_id": company_id,
                        "relationship_role": assoc_type,
                        "raw": company,
                    },
                    CRMParticipantType.COMPANY,
                )
            )

        return participants

    def _extract_stage_history(
        self,
        _deal_record: CRMDeal,
        _raw: dict[str, Any],
        _stage_map: dict[str, CRMDealStage],
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """Extract stage history events using HubSpot property history."""

        history_map = _raw.get("propertiesWithHistory") or {}
        stage_events = self._parse_property_history(
            history_map.get("dealstage"), self._sanitize_stage
        )

        if not stage_events:
            return [], None

        amount_history = self._parse_property_history(history_map.get("amount"), self._safe_float)
        probability_history = self._parse_property_history(
            history_map.get("hs_deal_stage_probability"), self._normalize_probability
        )
        forecast_prob_history = self._parse_property_history(
            history_map.get("hs_forecast_probability"), self._normalize_probability
        )

        history_entries: list[dict[str, Any]] = []
        for idx, (entered_at, stage_value, raw_entry) in enumerate(stage_events):
            if stage_value is None:
                continue

            stage_row = _stage_map.get(stage_value)
            exited_at = stage_events[idx + 1][0] if idx + 1 < len(stage_events) else None
            from_stage = stage_events[idx - 1][1] if idx > 0 else None
            from_stage_row = _stage_map.get(from_stage) if from_stage else None

            history_entries.append(
                self._build_stage_history_entry(
                    deal_record=_deal_record,
                    stage_value=stage_value,
                    entered_at=entered_at,
                    exited_at=exited_at,
                    from_stage=from_stage,
                    stage_row=stage_row,
                    from_stage_row=from_stage_row,
                    raw_entry=raw_entry,
                    amount_history=amount_history,
                    probability_history=probability_history,
                    forecast_history=forecast_prob_history,
                )
            )

        latest_entry = history_entries[-1] if history_entries else None
        return history_entries, latest_entry

    def _build_stage_history_entry(
        self,
        *,
        deal_record: CRMDeal,
        stage_value: str,
        entered_at: datetime,
        exited_at: datetime | None,
        from_stage: str | None,
        stage_row: CRMDealStage | None,
        from_stage_row: CRMDealStage | None,
        raw_entry: Any,
        amount_history: list[tuple[datetime, Any, dict[str, Any]]],
        probability_history: list[tuple[datetime, Any, dict[str, Any]]],
        forecast_history: list[tuple[datetime, Any, dict[str, Any]]],
    ) -> dict[str, Any]:
        elapsed_seconds = None
        if exited_at:
            elapsed_seconds = int((exited_at - entered_at).total_seconds())

        amount_at_stage = self._value_from_history(amount_history, entered_at)
        if amount_at_stage is None:
            amount_at_stage = deal_record.amount

        probability_at_stage = self._value_from_history(probability_history, entered_at)
        if probability_at_stage is None:
            probability_at_stage = self._value_from_history(forecast_history, entered_at)
        if probability_at_stage is None:
            probability_at_stage = deal_record.probability

        source_type = raw_entry.get("sourceType") if isinstance(raw_entry, dict) else None
        external_id = f"{deal_record.external_id}:{stage_value}:{int(entered_at.timestamp())}"

        return {
            "external_id": external_id,
            "deal_id": deal_record.deal_id,
            "stage_id": stage_row.stage_id if stage_row else None,
            "stage_external_id": stage_value,
            "stage_name": stage_row.name if stage_row else None,
            "stage_label": stage_row.label if stage_row else None,
            "from_stage_external_id": from_stage,
            "from_stage_name": from_stage_row.name if from_stage_row else None,
            "from_stage_label": from_stage_row.label if from_stage_row else None,
            "entered_at": entered_at,
            "exited_at": exited_at,
            "elapsed_seconds": elapsed_seconds,
            "amount_at_stage": amount_at_stage,
            "probability_at_stage": probability_at_stage,
            "source": source_type,
            "raw": raw_entry,
            "source_modified_at": entered_at,
        }

    def _parse_property_history(
        self,
        history_entries: Any,
        transform: Callable[[Any], Any | None] | None = None,
    ) -> list[tuple[datetime, Any, dict[str, Any]]]:
        """Parse HubSpot property history into sorted tuples."""

        parsed: list[tuple[datetime, Any, dict[str, Any]]] = []
        if not isinstance(history_entries, list):
            return parsed

        for entry in history_entries:
            if not isinstance(entry, dict):
                continue
            timestamp = self._parse_date(entry.get("timestamp") or entry.get("timestampMillis"))
            if not timestamp:
                continue
            value = entry.get("value")
            if transform:
                try:
                    value = transform(value)
                except Exception:
                    value = None
            parsed.append((timestamp, value, entry))

        parsed.sort(key=lambda item: item[0])
        return parsed

    def _value_from_history(
        self,
        history_entries: list[tuple[datetime, Any, dict[str, Any]]],
        reference_time: datetime,
    ) -> Any:
        """Return last value from history before or at the reference time."""

        result: Any = None
        for timestamp, value, _ in history_entries:
            if timestamp <= reference_time:
                result = value
            else:
                break
        return result

    def _sanitize_stage(self, value: Any) -> str | None:
        if value is None:
            return None
        return str(value).strip() or None

    def _extract_metrics(self, properties: dict[str, Any]) -> dict[str, Any]:
        """Extract metric fields."""
        metrics: dict[str, Any] = {}

        monetary_keys = (
            "hs_arr",
            "hs_mrr",
            "hs_tcv",
            "hs_acv",
            "hs_projected_amount",
            "hs_projected_amount_in_home_currency",
            "closed_amount",
        )
        for key in monetary_keys:
            self._store_metric(metrics, key, properties.get(key))

        self._store_metric(
            metrics, "days_to_close", properties.get("days_to_close"), self._safe_int
        )

        for counter_key in ("num_associated_contacts", "num_associated_companies", "num_notes"):
            self._store_metric(metrics, counter_key, properties.get(counter_key), self._safe_int)

        self._store_metric(
            metrics, "hs_exchange_rate", properties.get("hs_exchange_rate"), self._safe_float
        )
        self._store_metric(
            metrics,
            "hs_time_in_current_stage",
            properties.get("hs_time_in_current_stage"),
            self._safe_int,
        )
        self._store_metric(
            metrics,
            "hs_recurring_revenue_amount",
            properties.get("hs_recurring_revenue_amount"),
            self._safe_float,
        )
        self._store_metric(
            metrics,
            "hs_recurring_revenue_end_date",
            properties.get("hs_recurring_revenue_end_date"),
        )

        for key in ("hs_recurring_revenue_deal_type", "hs_recurring_revenue_inactive_reason"):
            self._store_metric(metrics, key, properties.get(key))

        return metrics

    def _store_metric(
        self,
        metrics: dict[str, Any],
        key: str,
        value: Any,
        converter: Callable[[Any], Any | None] | None = None,
    ) -> None:
        """Store metric if value is present, applying optional conversion."""
        if value in (None, ""):
            return
        if converter:
            converted = converter(value)
            if converted is not None:
                metrics[key] = converted
                return
        metrics[key] = value

    def _first_association_id(self, associations: Any) -> str | None:
        """Get first association ID from list."""
        if not isinstance(associations, list) or not associations:
            return None
        first = associations[0]
        if isinstance(first, dict):
            return first.get("id")
        return None

    def _parse_date(self, value: Any) -> datetime | None:
        """Parse datetime value from various formats.

        Handles:
        - None/empty values
        - datetime objects
        - Unix timestamps (milliseconds)
        - ISO 8601 strings (e.g., "2025-08-28T04:00:00Z")
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
                from dateutil import parser as date_parser  # type: ignore[import-untyped]

                return cast(datetime | None, date_parser.isoparse(value))
            except Exception:
                return None
        return None

    def _safe_float(self, value: Any) -> float | None:
        """Safely convert value to float."""
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _safe_int(self, value: Any) -> int | None:
        """Safely convert value to int."""
        if value is None or value == "":
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    def _is_truthy(self, value: Any) -> bool:
        """Interpret provider boolean-like values consistently."""
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        if isinstance(value, int | float):
            return value != 0
        s = str(value).strip().lower()
        return s in {"true", "yes", "1", "y", "t"}

    def _normalize_probability(self, value: Any) -> float | None:
        """Normalize probability to 0-100 scale."""
        numeric = self._safe_float(value)
        if numeric is None:
            return None
        if 0 <= numeric <= 1:
            return numeric * 100
        return numeric

    def _determine_deal_status(self, stage_external_id: str | None) -> CRMDealStatus:
        """Determine deal status from stage ID."""
        if not stage_external_id:
            return CRMDealStatus.OPEN
        slug = stage_external_id.lower()
        if "won" in slug:
            return CRMDealStatus.WON
        if "lost" in slug:
            return CRMDealStatus.LOST
        if "closed" in slug:
            return CRMDealStatus.CLOSED
        return CRMDealStatus.OPEN
