"""CRM sync service for HubSpot deals and related metadata."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.base_mixins import utcnow
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMDealStage,
    CRMDealStageHistory,
    CRMDealStatus,
    CRMParticipantType,
    CRMPipeline,
    CRMStageState,
)
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationSyncHistory,
    SyncStatus,
)
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


def _now() -> datetime:
    return utcnow()


def _datetime_now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _parse_hs_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(float(value) / 1000, tz=UTC)
        except Exception:
            return None
    text = str(value)
    if not text:
        return None
    try:
        if text.isdigit():
            return datetime.fromtimestamp(int(text) / 1000, tz=UTC)
        # Ensure Z suffix handled
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


class CRMSyncService:
    """Upserts HubSpot CRM data into organisation scoped tables."""

    def __init__(self, db: AsyncSession, organisation_id: UUID) -> None:
        self.db = db
        self.organisation_id = organisation_id
        self._sync_history: IntegrationSyncHistory | None = None
        self._integration: KnowledgeIntegration | None = None
        self._records_processed: int = 0
        self._records_failed: int = 0

    async def begin_sync(self, integration: KnowledgeIntegration) -> None:
        if self._integration:
            return
        self._integration = integration
        history = IntegrationSyncHistory(
            integration_id=integration.integration_id,
            status=SyncStatus.IN_PROGRESS,
        )
        self.db.add(history)
        await self.db.commit()
        await self.db.refresh(history)
        self._sync_history = history

    async def finalize_sync(self, status: SyncStatus, error: str | None = None) -> None:
        if not self._sync_history or not self._integration:
            return
        history = self._sync_history
        history.status = status
        history.completed_at = _datetime_now_naive()
        history.records_processed = self._records_processed
        history.records_failed = self._records_failed
        if error:
            if not history.error_details:
                history.error_details = {"messages": []}
            details = history.error_details or {}
            messages = details.get("messages") or []
            messages.append(error)
            details["messages"] = messages
            history.error_details = details
        self.db.add(history)

        integration = self._integration
        integration.last_synced_at = _datetime_now_naive()
        integration.last_sync_status = status.value
        if error:
            integration.last_sync_error = error
        else:
            integration.last_sync_error = None
        self.db.add(integration)
        await self.db.commit()

    def _increment_processed(self, amount: int = 1) -> None:
        self._records_processed += amount

    def _increment_failed(self, amount: int = 1) -> None:
        self._records_failed += amount

    # NOTE: sync_pipelines method has been migrated to PipelineSyncService
    # in src/service/integrations/crm/entities/pipeline_sync_service.py

    async def sync_deals(
        self,
        *,
        integration: KnowledgeIntegration,
        deals: Iterable[dict[str, Any]],
        provider: str,
    ) -> dict[str, int]:
        await self.begin_sync(integration)
        deal_list = [d for d in deals if isinstance(d, dict)]
        if not deal_list:
            return {"deals": 0, "participants": 0, "stage_history": 0}

        logger.info(
            "CRM sync deals: org=%s integration=%s provider=%s count=%d",
            self.organisation_id,
            integration.integration_id,
            provider,
            len(deal_list),
        )

        deal_external_ids = [d.get("id") for d in deal_list if d.get("id")]
        existing: dict[str, CRMDeal] = {}
        if deal_external_ids:
            stmt = (
                select(CRMDeal)
                .where(CRMDeal.integration_id == integration.integration_id)  # type: ignore[arg-type]
                .where(CRMDeal.external_id.in_(deal_external_ids))  # type: ignore[attr-defined]
            )
            result = await self.db.execute(stmt)
            existing = {row.external_id: row for row in result.scalars().all()}

        pipeline_map = await self._load_pipeline_map(integration.integration_id)
        stage_map = await self._load_stage_map(integration.integration_id)

        now = _now()
        participants_to_upsert: list[tuple[CRMDeal, dict[str, Any], CRMParticipantType]] = []
        history_entries: list[dict[str, Any]] = []

        deals_processed = 0
        for deal in deal_list:
            try:
                external_id = deal.get("id")
                if not external_id:
                    self._increment_failed()
                    continue

                record = existing.get(external_id)
                if not record:
                    record = CRMDeal(
                        organisation_id=self.organisation_id,
                        integration_id=integration.integration_id,
                        external_id=external_id,
                    )

                properties = deal.get("properties") or {}
                associations = deal.get("associations") or {}
                updated_at = self._parse_date(deal.get("updatedAt"))
                created_at = self._parse_date(deal.get("createdAt"))

                pipeline_external = properties.get("pipeline")
                stage_external = properties.get("dealstage")

                pipeline_record = (
                    pipeline_map.get(str(pipeline_external)) if pipeline_external else None
                )
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

                record.name = properties.get("dealname") or deal.get("name")
                record.description = properties.get("description")

                record.amount = self._safe_float(properties.get("amount"))
                record.amount_home_currency = self._safe_float(
                    properties.get("amount_in_home_currency")
                )
                record.currency = properties.get("deal_currency_code") or properties.get("currency")

                probability = properties.get("hs_deal_stage_probability") or properties.get(
                    "hs_forecast_probability"
                )
                record.probability = self._normalize_probability(probability)
                record.predicted_amount = self._safe_float(properties.get("hs_forecast_amount"))
                record.deal_status = self._determine_deal_status(stage_external)
                record.forecast_category = properties.get("hs_forecast_category")
                record.lead_source = properties.get("lead_source") or properties.get(
                    "hs_lead_source"
                )
                record.deal_type = properties.get("dealtype")
                record.priority = properties.get("hs_priority")

                owner_id = properties.get("hubspot_owner_id")
                if owner_id is not None:
                    record.owner_external_id = owner_id
                owner_email = properties.get("hubspot_owner_email")
                if isinstance(owner_email, str):
                    record.owner_email = owner_email
                owner_name = properties.get("hubspot_owner_name")
                if isinstance(owner_name, str):
                    record.owner_name = owner_name

                record.company_external_id = self._first_association_id(
                    associations.get("companies")
                )
                record.primary_contact_external_id = self._first_association_id(
                    associations.get("contacts")
                )

                record.close_date = self._parse_date(properties.get("closedate"))
                record.closed_won_at = self._parse_date(properties.get("hs_closed_won_date"))
                record.closed_lost_at = self._parse_date(properties.get("hs_closed_lost_date"))
                record.closed_reason = properties.get("closed_lost_reason") or properties.get(
                    "hs_closed_reason"
                )

                record.expected_close_date = self._parse_date(
                    properties.get("hs_expected_close_date")
                )
                record.last_activity_at = (
                    self._parse_date(properties.get("lastactivitydate"))
                    or self._parse_date(properties.get("hs_lastmodifieddate"))
                    or updated_at
                )
                record.stage_entered_at = None
                record.stage_updated_at = updated_at or self._parse_date(
                    properties.get("hs_lastmodifieddate")
                )
                record.source_created_at = (
                    self._parse_date(properties.get("createdate")) or created_at
                )

                record.properties = {k: v for k, v in properties.items() if v is not None}
                record.metrics = self._extract_metrics(properties)
                record.associations_payload = associations

                record.synced_at = now
                record.last_seen_at = now
                record.source_modified_at = updated_at or self._parse_date(
                    properties.get("hs_lastmodifieddate")
                )
                record.data_payload = deal
                record.is_deleted = False

                self.db.add(record)
                deals_processed += 1
                self._increment_processed()

                participants_to_upsert.extend(self._extract_participants(record, associations))
                deal_history_entries, latest_stage_entry = self._extract_stage_history(
                    record, deal, stage_map
                )
                history_entries.extend(deal_history_entries)
                if latest_stage_entry:
                    record.stage_entered_at = latest_stage_entry["entered_at"]
                    record.stage_updated_at = latest_stage_entry["entered_at"]
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.error(
                    "Failed to process HubSpot deal %s: %s",
                    deal.get("id"),
                    exc,
                    exc_info=True,
                )
                self._increment_failed()

        await self.db.commit()

        participants_processed = await self._upsert_participants(
            integration, participants_to_upsert
        )
        history_processed = await self._upsert_stage_history(integration, history_entries)

        return {
            "deals": deals_processed,
            "participants": participants_processed,
            "stage_history": history_processed,
            "records_synced": deals_processed,
        }

    async def sync_contacts(
        self,
        *,
        integration: KnowledgeIntegration,
        contacts: Iterable[dict[str, Any]],
        provider: str,
    ) -> dict[str, int]:
        await self.begin_sync(integration)
        contact_list = [c for c in contacts if isinstance(c, dict)]
        if not contact_list:
            return {"contacts": 0, "records_synced": 0}

        logger.info(
            "CRM sync contacts: org=%s integration=%s provider=%s count=%d",
            self.organisation_id,
            integration.integration_id,
            provider,
            len(contact_list),
        )

        contact_external_ids = [c.get("id") for c in contact_list if c.get("id")]
        existing: dict[str, CRMContact] = {}
        if contact_external_ids:
            stmt = (
                select(CRMContact)
                .where(CRMContact.integration_id == integration.integration_id)  # type: ignore[arg-type]
                .where(CRMContact.external_id.in_(contact_external_ids))  # type: ignore[attr-defined]
            )
            result = await self.db.execute(stmt)
            existing = {row.external_id: row for row in result.scalars().all()}

        now = _now()
        contacts_processed = 0

        for contact in contact_list:
            try:
                external_id = contact.get("id")
                if not external_id:
                    self._increment_failed()
                    continue

                record = existing.get(external_id)
                if not record:
                    record = CRMContact(
                        organisation_id=self.organisation_id,
                        integration_id=integration.integration_id,
                        external_id=external_id,
                    )
                    existing[external_id] = record

                properties = contact.get("properties") or {}
                updated_at = (
                    self._parse_date(contact.get("updatedAt"))
                    or self._parse_date(properties.get("hs_lastmodifieddate"))
                    or self._parse_date(properties.get("lastmodifieddate"))
                )
                created_at = self._parse_date(contact.get("createdAt")) or self._parse_date(
                    properties.get("createdate")
                )

                record.email = properties.get("email")
                record.additional_emails = self._parse_email_list(
                    properties.get("hs_additional_emails") or properties.get("additional_emails")
                )

                first_name = properties.get("firstname")
                last_name = properties.get("lastname")
                record.first_name = first_name
                record.last_name = last_name
                record.full_name = properties.get("fullname") or self._combine_name(
                    first_name, last_name, properties.get("company")
                )
                record.job_title = properties.get("jobtitle") or properties.get("title")
                record.company_name = properties.get("company")

                record.lifecycle_stage = properties.get("lifecyclestage")
                record.lead_status = properties.get("hs_lead_status") or properties.get(
                    "leadstatus"
                )
                record.marketing_status = properties.get("hs_marketable_status") or properties.get(
                    "hs_content_membership_email_status"
                )

                owner_id = properties.get("hubspot_owner_id")
                if owner_id is not None:
                    record.owner_external_id = owner_id
                owner_email = properties.get("hubspot_owner_email")
                if isinstance(owner_email, str):
                    record.owner_email = owner_email
                owner_name = properties.get("hubspot_owner_name")
                if isinstance(owner_name, str):
                    record.owner_name = owner_name

                record.phone = properties.get("phone")
                record.mobile_phone = properties.get("mobilephone")
                record.fax = properties.get("fax")
                record.website = properties.get("website")

                record.address = properties.get("address")
                record.city = properties.get("city")
                record.state = properties.get("state") or properties.get("state_region")
                record.postal_code = properties.get("zip") or properties.get("postalcode")
                record.country = properties.get("country")
                record.timezone = properties.get("timezone")

                record.last_contacted_at = self._parse_date(
                    properties.get("last_contacted")
                ) or self._parse_date(properties.get("hs_last_contacted"))
                record.last_activity_at = (
                    self._parse_date(properties.get("lastactivitydate"))
                    or self._parse_date(properties.get("hs_lastactivitydate"))
                    or self._parse_date(properties.get("recent_activity_date"))
                    or updated_at
                )

                record.source_created_at = created_at
                record.source_modified_at = updated_at
                record.synced_at = now
                record.last_seen_at = now

                record.properties = {k: v for k, v in properties.items() if v is not None}
                record.metrics = self._extract_contact_metrics(properties)
                record.data_payload = contact
                record.is_deleted = False

                self.db.add(record)
                contacts_processed += 1
                self._increment_processed()
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.error(
                    "Failed to process HubSpot contact %s: %s",
                    contact.get("id"),
                    exc,
                    exc_info=True,
                )
                self._increment_failed()

        await self.db.commit()

        return {
            "contacts": contacts_processed,
            "records_synced": contacts_processed,
        }

    # NOTE: sync_users method has been migrated to UserSyncService
    # in src/service/integrations/crm/entities/user_sync_service.py

    async def sync_companies(
        self,
        *,
        integration: KnowledgeIntegration,
        companies: Iterable[dict[str, Any]],
        provider: str,
    ) -> dict[str, int]:
        """
        Sync CRM companies/accounts into the database.

        Args:
            integration: Integration record from KnowledgeIntegration
            companies: Iterable of company dictionaries from the provider
            provider: Provider name (e.g., 'hubspot')

        Returns:
            Dictionary with counts of companies synced
        """
        await self.begin_sync(integration)
        company_list = [c for c in companies if isinstance(c, dict)]
        if not company_list:
            return {"companies": 0, "records_synced": 0}

        logger.info(
            "CRM sync companies: org=%s integration=%s provider=%s count=%d",
            self.organisation_id,
            integration.integration_id,
            provider,
            len(company_list),
        )

        # Load existing companies by external_id
        company_external_ids = [c.get("id") for c in company_list if c.get("id")]
        existing: dict[str, CRMCompany] = {}
        if company_external_ids:
            stmt = (
                select(CRMCompany)
                .where(CRMCompany.integration_id == integration.integration_id)  # type: ignore[arg-type]
                .where(CRMCompany.external_id.in_(company_external_ids))  # type: ignore[attr-defined]
            )
            result = await self.db.execute(stmt)
            existing = {row.external_id: row for row in result.scalars().all()}

        now = _now()
        companies_processed = 0

        for company in company_list:
            try:
                external_id = company.get("id")
                if not external_id:
                    self._increment_failed()
                    continue

                record = existing.get(external_id)
                if not record:
                    record = CRMCompany(
                        organisation_id=self.organisation_id,
                        integration_id=integration.integration_id,
                        external_id=external_id,
                    )
                    existing[external_id] = record

                properties = company.get("properties") or {}
                updated_at = self._parse_date(company.get("updatedAt")) or self._parse_date(
                    properties.get("hs_lastmodifieddate")
                )
                created_at = self._parse_date(company.get("createdAt")) or self._parse_date(
                    properties.get("createdate")
                )

                # Core company details
                record.name = properties.get("name") or company.get("name")
                record.domain = properties.get("domain")
                record.website = properties.get("website")
                record.description = properties.get("description")
                record.industry = properties.get("industry")
                record.company_type = properties.get("type")
                record.lifecycle_stage = properties.get("lifecyclestage")

                # Owner information
                owner_id = properties.get("hubspot_owner_id")
                if owner_id is not None:
                    record.owner_external_id = str(owner_id)
                owner_email = properties.get("hubspot_owner_email")
                if isinstance(owner_email, str):
                    record.owner_email = owner_email
                owner_name = properties.get("hubspot_owner_name")
                if isinstance(owner_name, str):
                    record.owner_name = owner_name

                # Business metrics
                num_employees = properties.get("numberofemployees")
                if num_employees is not None:
                    try:
                        record.number_of_employees = int(num_employees)
                    except (ValueError, TypeError):
                        record.number_of_employees = None

                annual_rev = properties.get("annualrevenue")
                if annual_rev is not None:
                    try:
                        record.annual_revenue = float(annual_rev)
                    except (ValueError, TypeError):
                        record.annual_revenue = None

                # Contact information
                record.phone = properties.get("phone")
                record.address = properties.get("address") or properties.get("address2")
                record.city = properties.get("city")
                record.state = properties.get("state")
                record.country = properties.get("country")
                record.postal_code = properties.get("zip")
                record.timezone = properties.get("timezone")

                # Social links
                linkedin = properties.get("linkedin_company_page") or properties.get("linkedinbio")
                if isinstance(linkedin, str):
                    record.linkedin_url = linkedin

                # Timing
                record.source_created_at = created_at
                record.source_modified_at = updated_at
                record.synced_at = now
                record.last_seen_at = now

                # Store raw data
                record.properties = properties
                record.data_payload = company

                # Metrics (can be computed from deals, etc.)
                metrics = {}
                if properties.get("hs_total_deal_value"):
                    metrics["total_deal_value"] = properties.get("hs_total_deal_value")
                if properties.get("total_revenue"):
                    metrics["total_revenue"] = properties.get("total_revenue")
                if properties.get("total_money_raised"):
                    metrics["total_money_raised"] = properties.get("total_money_raised")
                record.metrics = metrics

                record.is_active = True
                record.is_deleted = False

                self.db.add(record)
                companies_processed += 1
                self._increment_processed()

            except Exception as exc:
                logger.error(
                    "Failed to process company %s: %s",
                    company.get("id"),
                    exc,
                    exc_info=True,
                )
                self._increment_failed()

        await self.db.commit()

        return {
            "companies": companies_processed,
            "records_synced": companies_processed,
        }

    async def _load_pipeline_map(self, integration_id: UUID) -> dict[str, CRMPipeline]:
        stmt = (
            select(CRMPipeline)
            .where(CRMPipeline.integration_id == integration_id)  # type: ignore[arg-type]
            .where(CRMPipeline.is_deleted.is_(False))  # type: ignore[attr-defined]
        )
        result = await self.db.execute(stmt)
        return {row.external_id: row for row in result.scalars().all()}

    async def _load_stage_map(self, integration_id: UUID) -> dict[str, CRMDealStage]:
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
        if not participants:
            return 0

        now = _now()
        ext_ids = [payload[1]["external_id"] for payload in participants]
        existing: dict[str, CRMDealParticipant] = {}
        if ext_ids:
            stmt = (
                select(CRMDealParticipant)
                .where(CRMDealParticipant.integration_id == integration.integration_id)  # type: ignore[arg-type]
                .where(CRMDealParticipant.external_id.in_(ext_ids))  # type: ignore[attr-defined]
            )
            result = await self.db.execute(stmt)
            existing = {row.external_id: row for row in result.scalars().all()}

        processed = 0
        for deal_record, payload, participant_type in participants:
            external_id = payload.get("external_id")
            if not external_id:
                self._increment_failed()
                continue

            record = existing.get(external_id)
            if not record:
                record = CRMDealParticipant(
                    organisation_id=self.organisation_id,
                    integration_id=integration.integration_id,
                    external_id=external_id,
                    deal_id=deal_record.deal_id,
                )
            record.deal_id = deal_record.deal_id
            record.participant_type = participant_type
            record.relationship_role = payload.get("relationship_role")
            related_ext_id = payload.get("related_external_id")
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
            self._increment_processed()

        await self.db.commit()
        return processed

    async def _upsert_stage_history(
        self,
        integration: KnowledgeIntegration,
        history_entries: list[dict[str, Any]],
    ) -> int:
        if not history_entries:
            return 0

        now = _now()
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

        processed = 0
        for entry in history_entries:
            external_id = entry.get("external_id")
            if not external_id:
                continue
            record = existing.get(external_id)
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
                # Fallback if it's not already a datetime
                record.entered_at = _now()
            else:
                record.entered_at = _now()
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
            self._increment_processed()

        await self.db.commit()
        return processed

    # NOTE: Pipeline and stage helper methods (_pipeline_external_id, _stage_external_id,
    # _normalize_probability) have been moved to PipelineSyncService

    @staticmethod
    def _determine_stage_state(stage_external_id: str | None) -> CRMStageState:
        if not stage_external_id:
            return CRMStageState.OPEN
        slug = stage_external_id.lower()
        if "won" in slug:
            return CRMStageState.WON
        if "lost" in slug:
            return CRMStageState.LOST
        return CRMStageState.OPEN

    @staticmethod
    def _determine_deal_status(stage_external_id: str | None) -> CRMDealStatus:
        state = CRMSyncService._determine_stage_state(stage_external_id)
        return CRMDealStatus(state)

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_probability(value: Any) -> float | None:
        """Normalize probability from 0-1 range to 0-100 percentage."""
        numeric = CRMSyncService._safe_float(value)
        if numeric is None:
            return None
        if 0 <= numeric <= 1:
            return numeric * 100
        return numeric

    @staticmethod
    def _as_bool(value: Any) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, int | float):
            return value != 0
        text = str(value).strip().lower()
        if text in {"true", "1", "yes", "y"}:
            return True
        if text in {"false", "0", "no", "n"}:
            return False
        return None

    @staticmethod
    def _parse_date(value: Any) -> datetime | None:
        return _parse_hs_datetime(value)

    @staticmethod
    def _first_association_id(block: Any) -> str | None:
        if isinstance(block, dict):
            results = block.get("results")
            if isinstance(results, list) and results:
                first = results[0]
                if isinstance(first, dict):
                    return str(first.get("id")) if first.get("id") else None
        return None

    def _extract_metrics(self, properties: dict[str, Any]) -> dict[str, Any]:
        keys = [
            "hs_arr",
            "hs_mrr",
            "hs_annual_contract_value",
            "hs_deal_stage_probability",
            "hs_forecast_amount",
            "hs_forecast_probability",
        ]
        return {key: properties[key] for key in keys if properties.get(key) is not None}

    @staticmethod
    def _extract_contact_metrics(properties: dict[str, Any]) -> dict[str, Any]:
        keys = [
            "hs_lead_score",
            "hs_email_open_rate",
            "hs_email_click_rate",
            "hs_email_sends",
            "hs_email_opens",
            "hs_email_clicks",
        ]
        metrics: dict[str, Any] = {}
        for key in keys:
            value = properties.get(key)
            if value is None or value == "":
                continue
            numeric = CRMSyncService._safe_float(value)
            metrics[key] = numeric if numeric is not None else value
        return metrics

    @staticmethod
    def _parse_email_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value).strip()
        if not text:
            return []
        separators = [",", ";", "|"]
        for sep in separators:
            if sep in text:
                return [part.strip() for part in text.split(sep) if part.strip()]
        return [text]

    @staticmethod
    def _combine_name(
        first: str | None,
        last: str | None,
        fallback: str | None = None,
    ) -> str | None:
        parts = [part for part in (first, last) if part]
        if parts:
            return " ".join(parts)
        return fallback

    def _extract_participants(
        self,
        deal_record: CRMDeal,
        associations: dict[str, Any],
    ) -> list[tuple[CRMDeal, dict[str, Any], CRMParticipantType]]:
        participants: list[tuple[CRMDeal, dict[str, Any], CRMParticipantType]] = []
        mapping: dict[str, CRMParticipantType] = {
            "contacts": CRMParticipantType.CONTACT,
            "companies": CRMParticipantType.COMPANY,
            "tickets": CRMParticipantType.TICKET,
            "line_items": CRMParticipantType.LINE_ITEM,
        }
        for key, participant_type in mapping.items():
            block = associations.get(key)
            if not isinstance(block, dict):
                continue
            results = block.get("results")
            if not isinstance(results, list):
                continue
            for res in results:
                if not isinstance(res, dict):
                    continue
                related_id = res.get("id")
                if not related_id:
                    continue
                external_id = f"{deal_record.external_id}:{participant_type.value}:{related_id}"
                payload = {
                    "external_id": external_id,
                    "related_external_id": str(related_id),
                    "relationship_role": res.get("type") or key.rstrip("s"),
                    "related_name": res.get("properties", {}).get("name")
                    if isinstance(res.get("properties"), dict)
                    else None,
                    "related_email": res.get("properties", {}).get("email")
                    if isinstance(res.get("properties"), dict)
                    else None,
                    "raw": res,
                    "source_modified_at": _parse_hs_datetime(res.get("updatedAt")),
                }
                participants.append((deal_record, payload, participant_type))
        # owners are not returned via associations; derive from deal fields
        if deal_record.owner_external_id:
            external_id = f"{deal_record.external_id}:owner:{deal_record.owner_external_id}"
            payload = {
                "external_id": external_id,
                "related_external_id": deal_record.owner_external_id,
                "relationship_role": "owner",
                "related_name": deal_record.owner_name,
                "related_email": deal_record.owner_email,
                "raw": None,
                "source_modified_at": None,
            }
            participants.append((deal_record, payload, CRMParticipantType.USER))

        return participants

    def _extract_stage_history(
        self,
        deal_record: CRMDeal,
        deal_payload: dict[str, Any],
        stage_map: dict[str, CRMDealStage],
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        history_map = deal_payload.get("propertiesWithHistory") or {}
        stage_history = history_map.get("dealstage")
        if not isinstance(stage_history, list):
            return [], None

        events = []
        for entry in stage_history:
            if not isinstance(entry, dict):
                continue
            timestamp = _parse_hs_datetime(entry.get("timestamp") or entry.get("timestampMillis"))
            value = entry.get("value")
            if not timestamp or not value:
                continue
            events.append(
                {
                    "timestamp": timestamp,
                    "stage": value,
                    "raw": entry,
                }
            )

        if not events:
            return [], None

        events.sort(key=lambda e: e["timestamp"])
        history_entries: list[dict[str, Any]] = []
        for idx, event in enumerate(events):
            stage_row = stage_map.get(event["stage"])
            entered_at = event["timestamp"]
            exited_at = events[idx + 1]["timestamp"] if idx + 1 < len(events) else None
            elapsed_seconds = None
            if entered_at and exited_at:
                elapsed_seconds = int((exited_at - entered_at).total_seconds())
            external_id = (
                f"{deal_record.external_id}:{event['stage']}:{int(entered_at.timestamp())}"
            )
            history_entries.append(
                {
                    "external_id": external_id,
                    "deal_id": deal_record.deal_id,
                    "stage_id": stage_row.stage_id if stage_row else None,
                    "stage_external_id": event["stage"],
                    "from_stage_external_id": events[idx - 1]["stage"] if idx > 0 else None,
                    "entered_at": entered_at,
                    "exited_at": exited_at,
                    "elapsed_seconds": elapsed_seconds,
                    "amount_at_stage": deal_record.amount,
                    "probability_at_stage": deal_record.probability,
                    "source": event["raw"].get("sourceType"),
                    "raw": event["raw"],
                    "source_modified_at": entered_at,
                }
            )

        latest_entry = history_entries[-1] if history_entries else None
        return history_entries, latest_entry
