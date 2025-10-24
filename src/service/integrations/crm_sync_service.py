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
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMDealStage,
    CRMDealStageHistory,
    CRMDealStatus,
    CRMParticipantType,
    CRMPipeline,
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

    async def sync_pipelines(
        self,
        *,
        integration: KnowledgeIntegration,
        pipelines: Iterable[dict[str, Any]],
        provider: str,
    ) -> int:
        await self.begin_sync(integration)
        pipeline_list = [p for p in pipelines if isinstance(p, dict)]
        if not pipeline_list:
            return 0

        logger.info(
            "CRM sync pipelines: org=%s integration=%s provider=%s count=%d",
            self.organisation_id,
            integration.integration_id,
            provider,
            len(pipeline_list),
        )

        external_ids = [
            self._pipeline_external_id(p) for p in pipeline_list if self._pipeline_external_id(p)
        ]

        existing: dict[str, CRMPipeline] = {}
        if external_ids:
            stmt = select(CRMPipeline).where(
                CRMPipeline.integration_id == integration.integration_id,
                CRMPipeline.external_id.in_(external_ids),
            )
            result = await self.db.execute(stmt)
            existing = {row.external_id: row for row in result.scalars().all()}

        now = _now()
        processed = 0
        for pipeline_data in pipeline_list:
            external_id = self._pipeline_external_id(pipeline_data)
            if not external_id:
                self._increment_failed()
                continue

            record = existing.get(external_id)
            if not record:
                record = CRMPipeline(
                    organisation_id=self.organisation_id,
                    integration_id=integration.integration_id,
                    external_id=external_id,
                )

            record.name = pipeline_data.get("label") or pipeline_data.get("name") or external_id
            record.label = pipeline_data.get("label") or record.name
            record.object_type = pipeline_data.get("objectType") or "deals"
            record.is_default = bool(pipeline_data.get("default"))
            record.is_active = not bool(pipeline_data.get("archived"))
            record.display_order = pipeline_data.get("displayOrder")
            record.properties = pipeline_data
            record.synced_at = now
            record.last_seen_at = now
            record.source_modified_at = _parse_hs_datetime(pipeline_data.get("updatedAt"))
            record.data_payload = pipeline_data
            record.is_deleted = False

            self.db.add(record)
            await self._sync_stages_for_pipeline(integration, record, pipeline_data.get("stages"))
            self._increment_processed()
            processed += 1

        await self.db.commit()
        return processed

    async def _sync_stages_for_pipeline(
        self,
        integration: KnowledgeIntegration,
        pipeline_record: CRMPipeline,
        stages_payload: Any,
    ) -> None:
        if not isinstance(stages_payload, list):
            return

        stage_items = [s for s in stages_payload if isinstance(s, dict)]
        if not stage_items:
            return

        external_ids = [
            self._stage_external_id(stage)
            for stage in stage_items
            if self._stage_external_id(stage)
        ]
        existing: dict[str, CRMDealStage] = {}
        if external_ids:
            stmt = select(CRMDealStage).where(
                CRMDealStage.integration_id == integration.integration_id,
                CRMDealStage.external_id.in_(external_ids),
            )
            result = await self.db.execute(stmt)
            existing = {row.external_id: row for row in result.scalars().all()}

        now = _now()
        seen_external_ids: set[str] = set()
        for stage in stage_items:
            stage_external_id = self._stage_external_id(stage)
            if not stage_external_id:
                self._increment_failed()
                continue
            seen_external_ids.add(stage_external_id)
            record = existing.get(stage_external_id)
            if not record:
                record = CRMDealStage(
                    organisation_id=self.organisation_id,
                    integration_id=integration.integration_id,
                    external_id=stage_external_id,
                    pipeline_id=pipeline_record.pipeline_id,
                )

            record.pipeline_id = pipeline_record.pipeline_id
            record.name = stage.get("label") or stage.get("displayName") or stage_external_id
            record.label = stage.get("label")
            record.stage_key = stage.get("stageId") or stage.get("id")
            metadata = stage.get("metadata") if isinstance(stage.get("metadata"), dict) else {}
            probability = metadata.get("probability", stage.get("probability"))
            record.probability = self._normalize_probability(probability)

            record.stage_state = self._determine_stage_state(stage_external_id)
            record.forecast_category = (
                stage.get("metadata", {}).get("forecastCategory")
                if isinstance(stage.get("metadata"), dict)
                else stage.get("forecastCategory")
            )
            record.display_order = stage.get("displayOrder")
            record.is_closed = record.stage_state != "open"
            record.synced_at = now
            record.last_seen_at = now
            record.source_modified_at = _parse_hs_datetime(stage.get("updatedAt"))
            record.data_payload = stage
            record.is_deleted = False
            self.db.add(record)

        # Soft-delete stages missing in latest payload
        for ext_id, record in existing.items():
            if ext_id not in seen_external_ids:
                record.is_deleted = True
                record.last_seen_at = now
                self.db.add(record)

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
            stmt = select(CRMDeal).where(
                CRMDeal.integration_id == integration.integration_id,
                CRMDeal.external_id.in_(deal_external_ids),
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

                pipeline_external = properties.get("pipeline")
                stage_external = properties.get("dealstage")

                pipeline_record = pipeline_map.get(pipeline_external)
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

                stage_record = stage_map.get(stage_external)
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
                record.amount_home_currency = self._safe_float(properties.get("hs_forecast_amount"))
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
                record.last_activity_at = self._parse_date(properties.get("hs_lastmodifieddate"))
                record.stage_entered_at = None
                record.stage_updated_at = None
                record.source_created_at = self._parse_date(properties.get("createdate"))

                record.properties = {k: v for k, v in properties.items() if v is not None}
                record.metrics = self._extract_metrics(properties)
                record.associations_payload = associations

                record.synced_at = now
                record.last_seen_at = now
                record.source_modified_at = self._parse_date(properties.get("hs_lastmodifieddate"))
                record.data_payload = deal
                record.is_deleted = False

                self.db.add(record)
                deals_processed += 1
                self._increment_processed()

                participants_to_upsert.extend(self._extract_participants(record, associations))
                history_entries.extend(self._extract_stage_history(record, deal, stage_map))
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
        }

    async def _load_pipeline_map(self, integration_id: UUID) -> dict[str, CRMPipeline]:
        stmt = select(CRMPipeline).where(
            CRMPipeline.integration_id == integration_id,
            CRMPipeline.is_deleted.is_(False),
        )
        result = await self.db.execute(stmt)
        return {row.external_id: row for row in result.scalars().all()}

    async def _load_stage_map(self, integration_id: UUID) -> dict[str, CRMDealStage]:
        stmt = select(CRMDealStage).where(
            CRMDealStage.integration_id == integration_id,
            CRMDealStage.is_deleted.is_(False),
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
            stmt = select(CRMDealParticipant).where(
                CRMDealParticipant.integration_id == integration.integration_id,
                CRMDealParticipant.external_id.in_(ext_ids),
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
            record.related_external_id = payload.get("related_external_id")
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
            stmt = select(CRMDealStageHistory).where(
                CRMDealStageHistory.integration_id == integration.integration_id,
                CRMDealStageHistory.external_id.in_(ext_ids),
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
            record.entered_at = entry.get("entered_at")
            record.exited_at = entry.get("exited_at")
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

    @staticmethod
    def _pipeline_external_id(data: dict[str, Any]) -> str | None:
        return data.get("id") or data.get("pipelineId")

    @staticmethod
    def _stage_external_id(data: dict[str, Any]) -> str | None:
        return data.get("id") or data.get("stageId")

    @staticmethod
    def _determine_stage_state(stage_external_id: str | None) -> str:
        if not stage_external_id:
            return "open"
        slug = stage_external_id.lower()
        if "won" in slug:
            return CRMDealStatus.WON.value
        if "lost" in slug:
            return CRMDealStatus.LOST.value
        if "closed" in slug:
            return CRMDealStatus.CLOSED.value
        return CRMDealStatus.OPEN.value

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
        numeric = CRMSyncService._safe_float(value)
        if numeric is None:
            return None
        if 0 <= numeric <= 1:
            return numeric * 100
        return numeric

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
        ]
        return {key: properties[key] for key in keys if properties.get(key) is not None}

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
    ) -> list[dict[str, Any]]:
        history_map = deal_payload.get("propertiesWithHistory") or {}
        stage_history = history_map.get("dealstage")
        if not isinstance(stage_history, list):
            return []

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
            return []

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

        return history_entries
