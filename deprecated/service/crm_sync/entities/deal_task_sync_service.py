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
from src.workers.integrations_worker.utils.sync_metadata import (
    SyncAction,
    SyncContext,
    classify_records,
    log_sync_action_distribution,
)

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
        deals_with_new_notes: set[UUID] = set()

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

                # Track deals with new notes for pipeline trigger
                if record.deal_id and record.activity_type == CRMActivityType.NOTE.value:
                    deals_with_new_notes.add(record.deal_id)

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
            "deals_with_new_notes": list(deals_with_new_notes),
            **result.to_dict(),
        }

    async def sync_incremental(
        self,
        *,
        integration: KnowledgeIntegration,
        records: Iterable[dict[str, Any]],
        provider: str,
    ) -> dict[str, Any]:
        """Sync deal task records incrementally.

        This method is optimized for incremental syncs where records come with
        sync_action metadata (ADDED, UPDATED, DELETED). It uses database state
        as the source of truth for create vs update decisions.

        Args:
            integration: KnowledgeIntegration instance
            records: Raw task records from provider API with _sync_metadata
            provider: Provider name (e.g., 'hubspot', 'salesforce')

        Returns:
            Dictionary with sync statistics
        """
        result = self._init_result()
        self._log_sync_start("deal_tasks_incremental", provider, 0)

        task_list = [t for t in records if isinstance(t, dict)]
        if not task_list:
            result.complete()
            return {
                "deal_tasks": 0,
                **result.to_dict(),
            }

        # Log sync action distribution for observability
        log_sync_action_distribution(task_list, "deal_tasks", logger)

        # Load existing tasks to determine create vs update
        task_external_ids = [str(t.get("id")) for t in task_list if t.get("id")]
        existing = await self._load_existing_by_external_ids(
            CRMDealTask,
            task_external_ids,
            integration.integration_id,
        )
        existing_ids = set(existing.keys())

        # Classify records for logging (sync_action is only for observability)
        context = SyncContext(existing_ids=existing_ids)
        classify_records(task_list, context)

        # Load deal mapping for association resolution
        deal_map = await self._load_deal_map(integration.integration_id)

        now = utcnow()
        tasks_processed = 0
        tasks_skipped = 0
        deals_with_new_notes: set[UUID] = set()

        for task in task_list:
            try:
                external_id = str(task.get("id")) if task.get("id") else None
                if not external_id:
                    result.increment_failed()
                    result.add_error(f"Missing external ID for task: {task}")
                    continue

                # Extract sync metadata for logging/observability
                sync_metadata = task.get("_sync_metadata", {})
                sync_action = SyncAction(sync_metadata.get("sync_action", SyncAction.ADDED.value))

                # Handle deletions - check both HubSpot and Salesforce patterns
                is_archived = task.get("archived", False) or task.get("IsDeleted", False)

                if sync_action == SyncAction.DELETED or is_archived:
                    # Soft-delete: mark as deleted if exists
                    record = existing.get(external_id)
                    if record:
                        record.is_deleted = True
                        record.synced_at = now
                        record.last_seen_at = now
                        self.db.add(record)
                        result.increment_deleted()
                        tasks_processed += 1
                    continue

                # Database state is source of truth for create vs update
                record = existing.get(external_id)
                is_new = record is None

                if is_new:
                    # Create new task
                    record = CRMDealTask(
                        organisation_id=self.organisation_id,
                        integration_id=integration.integration_id,
                        external_id=external_id,
                    )
                else:
                    # Check for meaningful changes before updating
                    if not self._has_meaningful_changes(record, task, provider):
                        tasks_skipped += 1
                        # Update last_seen_at even if no changes
                        record.last_seen_at = now
                        self.db.add(record)
                        continue

                # Transform and update record
                self._update_task_record(record, task, provider, deal_map)

                # Update metadata
                record.synced_at = now
                record.last_seen_at = now
                record.is_deleted = False

                self.db.add(record)
                tasks_processed += 1
                result.increment_processed()

                if is_new:
                    result.increment_created()
                else:
                    result.increment_updated()

                # Track deals with new notes for pipeline trigger
                if record.deal_id and record.activity_type == CRMActivityType.NOTE.value:
                    deals_with_new_notes.add(record.deal_id)

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

        logger.info(
            "Incremental deal task sync complete: processed=%d, skipped=%d, created=%d, updated=%d, deleted=%d",
            tasks_processed,
            tasks_skipped,
            result.created,
            result.updated,
            result.deleted,
        )

        return {
            "deal_tasks": tasks_processed,
            "skipped": tasks_skipped,
            "deals_with_new_notes": list(deals_with_new_notes),
            **result.to_dict(),
        }

    def _has_meaningful_changes(
        self,
        existing: CRMDealTask,
        incoming: dict[str, Any],
        provider: str,
    ) -> bool:
        """Check if incoming data has meaningful changes compared to existing record.

        Args:
            existing: Existing CRMDealTask record from database
            incoming: Incoming task data from provider
            provider: Provider name for field mapping

        Returns:
            True if there are meaningful changes, False otherwise
        """
        props = incoming.get("properties") or {}

        # Check source_modified_at as primary indicator
        incoming_modified = self._parse_incoming_modified_date(incoming, props, provider)
        if (
            incoming_modified
            and existing.source_modified_at
            and incoming_modified > existing.source_modified_at
        ):
            return True

        # Compare key fields based on provider
        if provider == "hubspot":
            checks = [
                (existing.subject, props.get("hs_task_subject")),
                (existing.body, props.get("hs_task_body")),
                (existing.owner_external_id, props.get("hubspot_owner_id")),
            ]
            # Check normalized status/type/priority
            raw_status = props.get("hs_task_status")
            raw_type = props.get("hs_task_type")
            raw_priority = props.get("hs_task_priority")
        else:
            # Salesforce - detect if it's a Case (ticket) vs Task
            is_salesforce_ticket = props.get("case_number") is not None
            checks = [
                (existing.subject, props.get("subject")),
                (
                    existing.body,
                    props.get("content") if is_salesforce_ticket else props.get("description"),
                ),
                (
                    existing.owner_external_id,
                    str(props.get("hubspot_owner_id") or props.get("owner_id") or "")
                    if (props.get("hubspot_owner_id") or props.get("owner_id"))
                    else None,
                ),
            ]
            raw_status = (
                props.get("hs_pipeline_stage") if is_salesforce_ticket else props.get("status")
            )
            raw_type = props.get("case_type") if is_salesforce_ticket else props.get("type")
            raw_priority = (
                props.get("hs_ticket_priority") if is_salesforce_ticket else props.get("priority")
            )

        # Compare normalized enum fields
        normalized_status = normalize_activity_status(raw_status)
        normalized_type = normalize_activity_type(raw_type)
        normalized_priority = normalize_activity_priority(raw_priority)

        if existing.status != normalized_status:
            return True
        if existing.task_type != normalized_type:
            return True
        if existing.priority != normalized_priority:
            return True

        # Compare associations
        associations = incoming.get("associations", {})
        deals_data = associations.get("deals", {})
        if isinstance(deals_data, dict):
            deals = deals_data.get("results", [])
        elif isinstance(deals_data, list):
            deals = deals_data
        else:
            deals = []

        if deals and isinstance(deals, list) and len(deals) > 0:
            first_deal = deals[0]
            deal_ext_id = str(first_deal.get("id")) if isinstance(first_deal, dict) else None
            if deal_ext_id and existing.deal_external_id != deal_ext_id:
                return True

        # Compare basic field checks
        for existing_val, incoming_val in checks:
            if existing_val is None and incoming_val is None:
                continue
            if existing_val != incoming_val:
                return True

        return False

    def _parse_incoming_modified_date(
        self,
        incoming: dict[str, Any],
        props: dict[str, Any],
        provider: str,
    ) -> datetime | None:
        """Parse the modified date from incoming data.

        Args:
            incoming: Full incoming record
            props: Properties dictionary
            provider: Provider name

        Returns:
            Parsed datetime or None
        """
        if provider == "hubspot":
            # Try updatedAt first, then hs_lastmodifieddate
            updated_at = incoming.get("updatedAt")
            if updated_at:
                parsed = self._parse_hubspot_timestamp(updated_at)
                if parsed:
                    return parsed
            return self._parse_hubspot_timestamp(props.get("hs_lastmodifieddate"))
        else:
            # Salesforce
            is_salesforce_ticket = props.get("case_number") is not None
            if is_salesforce_ticket:
                return self._parse_salesforce_datetime(props.get("hs_lastmodifieddate"))
            return self._parse_salesforce_datetime(props.get("last_modified_date"))

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

        elif provider == "salesforce":
            # Detect if this is a Case (Ticket) vs Task based on field presence
            # Cases have case_number, case_origin; Tasks have activity_date, what_id
            is_salesforce_ticket = props.get("case_number") is not None

            record.subject = props.get("subject")
            record.body = props.get("content") if is_salesforce_ticket else props.get("description")

            # Status/Type/Priority mapping
            raw_status = (
                props.get("hs_pipeline_stage") if is_salesforce_ticket else props.get("status")
            )
            raw_type = props.get("case_type") if is_salesforce_ticket else props.get("type")
            raw_priority = (
                props.get("hs_ticket_priority") if is_salesforce_ticket else props.get("priority")
            )

            normalized_status = normalize_activity_status(raw_status)
            normalized_type = normalize_activity_type(raw_type)
            normalized_priority = normalize_activity_priority(raw_priority)

            record.status = normalized_status
            record.task_type = normalized_type
            record.priority = normalized_priority

            if raw_status and normalized_status is None:
                props["_raw_activity_status"] = raw_status

            if raw_type and normalized_type == CRMActivityType.OTHER.value:
                if record.activity_subtype is None:
                    record.activity_subtype = str(raw_type)
                props["_raw_activity_type"] = raw_type

            if raw_priority and normalized_priority == CRMActivityPriority.NONE.value:
                props["_raw_activity_priority"] = raw_priority

            # Owner information - both Cases and Tasks use hubspot_owner_id in canonical format
            owner_id = props.get("hubspot_owner_id") or props.get("owner_id")
            if owner_id is not None:
                record.owner_external_id = str(owner_id)

            # Dates - Cases use createdate/closed_date; Tasks use activity_date/created_date
            if is_salesforce_ticket:
                record.source_created_at = self._parse_salesforce_datetime(props.get("createdate"))
                record.source_modified_at = self._parse_salesforce_datetime(
                    props.get("hs_lastmodifieddate")
                )
                record.completed_date = self._parse_salesforce_datetime(props.get("closed_date"))
                # For tickets, activity_type should be "OTHER" or a ticket-specific type
                if record.task_type is None:
                    record.activity_subtype = "TICKET"
            else:
                record.due_date = self._parse_salesforce_date(props.get("activity_date"))
                record.source_created_at = self._parse_salesforce_datetime(
                    props.get("created_date")
                )
                record.source_modified_at = self._parse_salesforce_datetime(
                    props.get("last_modified_date")
                )

            # Association handling differs between Cases and Tasks
            if is_salesforce_ticket:
                # Cases have direct ContactId and AccountId fields
                contact_id = props.get("contact_id")
                if contact_id is not None:
                    record.contact_external_id = str(contact_id)

                account_id = props.get("account_id")
                if account_id is not None:
                    record.company_external_id = str(account_id)
            else:
                # Tasks use WhatId and WhoId - these are polymorphic fields
                # WhatId can point to: Opportunity, Account, Campaign, Case, etc.
                # WhoId can point to: Contact or Lead
                # Salesforce ID prefixes: 006=Opportunity, 001=Account, 003=Contact, 00Q=Lead
                what_id = props.get("what_id")
                if what_id is not None:
                    what_id_str = str(what_id)
                    # Check if WhatId points to an Opportunity (prefix 006)
                    if what_id_str.startswith("006"):
                        record.deal_external_id = what_id_str
                        record.deal_id = deal_map.get(what_id_str)
                    # Check if WhatId points to an Account (prefix 001)
                    elif what_id_str.startswith("001"):
                        record.company_external_id = what_id_str

                who_id = props.get("who_id")
                if who_id is not None:
                    who_id_str = str(who_id)
                    # WhoId typically points to Contact (003) or Lead (00Q)
                    # We store it as contact_external_id for both
                    if who_id_str.startswith("003") or who_id_str.startswith("00Q"):
                        record.contact_external_id = who_id_str

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

        # Also resolve contact/company associations from canonical format
        # (Salesforce Cases/Tickets include these in associations.contacts/companies)
        contacts_data = associations.get("contacts", {})
        if isinstance(contacts_data, dict):
            contacts = contacts_data.get("results", [])
        elif isinstance(contacts_data, list):
            contacts = contacts_data
        else:
            contacts = []

        if (
            contacts
            and isinstance(contacts, list)
            and len(contacts) > 0
            and not record.contact_external_id
        ):
            first_contact = contacts[0]
            contact_ext_id = (
                str(first_contact.get("id")) if isinstance(first_contact, dict) else None
            )
            if contact_ext_id:
                record.contact_external_id = contact_ext_id

        companies_data = associations.get("companies", {})
        if isinstance(companies_data, dict):
            companies = companies_data.get("results", [])
        elif isinstance(companies_data, list):
            companies = companies_data
        else:
            companies = []

        if (
            companies
            and isinstance(companies, list)
            and len(companies) > 0
            and not record.company_external_id
        ):
            first_company = companies[0]
            company_ext_id = (
                str(first_company.get("id")) if isinstance(first_company, dict) else None
            )
            if company_ext_id:
                record.company_external_id = company_ext_id

        # Calculate metrics
        record.metrics = self._calculate_metrics(record)

        # Store raw data
        record.properties = props
        record.data_payload = task

    def _parse_salesforce_datetime(self, value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None

    def _parse_salesforce_date(self, value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            # Salesforce Task.ActivityDate is often YYYY-MM-DD
            try:
                if len(value) == 10 and value[4] == "-" and value[7] == "-":
                    return datetime.fromisoformat(value)
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None

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
