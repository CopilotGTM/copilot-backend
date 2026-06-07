"""Upsert Salesforce stage history rows into crm_deal_stage_history."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStageHistory
from src.domain.models.organisation_tables.sync.crm.source_mappings import (
    CRMSourceObjectType,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.common.query_chunks import (
    LOCAL_ID_QUERY_CHUNK_SIZE,
    iter_chunks,
)
from src.service.integrations.crm.records.common.source_mapping import (
    CRMSourceMappingHelper,
    IntegrationMappingUpsert,
)
from src.service.integrations.crm.records.salesforce.deals.deal_types import (
    DimensionHit,
    NormalizedSalesforceStageTransition,
    StageHistoryWriteResult,
)

logger = get_logger(__name__)


class SalesforceDealStageHistoryWriter:
    def __init__(
        self,
        *,
        session: AsyncSession,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.integration_id = integration_id
        self.source_mappings = CRMSourceMappingHelper(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            provider="salesforce",
        )

    async def apply(
        self,
        *,
        transitions: list[NormalizedSalesforceStageTransition],
        deal_pk_map: dict[str, UUID],
        stage_map: dict[str, DimensionHit],
    ) -> StageHistoryWriteResult:
        result = StageHistoryWriteResult()
        if not transitions:
            await self.session.flush()
            return result

        now = datetime.now(UTC)
        existing_by_external = await self._load_existing_rows(
            external_ids={_synthetic_external_id(transition) for transition in transitions}
        )
        mapping_rows: list[IntegrationMappingUpsert] = []

        for transition in transitions:
            deal_id = deal_pk_map.get(transition.deal_external_id)
            if deal_id is None:
                continue

            external_id = _synthetic_external_id(transition)
            stage_hit = stage_map.get(transition.stage_external_id)
            if stage_hit is None:
                result.unresolved_stage += 1
                resolved_stage_id: UUID | None = None
            else:
                resolved_stage_id = stage_hit.internal_id

            resolved_from_stage_id: UUID | None = None
            if transition.from_stage_external_id:
                from_hit = stage_map.get(transition.from_stage_external_id)
                if from_hit is None:
                    result.unresolved_from_stage += 1
                else:
                    resolved_from_stage_id = from_hit.internal_id

            existing = existing_by_external.get(external_id)
            if existing is None:
                row = self._new_row(
                    transition=transition,
                    deal_id=deal_id,
                    stage_id=resolved_stage_id,
                    from_stage_id=resolved_from_stage_id,
                    now=now,
                )
                self.session.add(row)
                existing_by_external[external_id] = row
                mapping_rows.append(
                    self._source_mapping_row(
                        external_id=external_id,
                        transition=transition,
                        local_history_id=row.history_id,
                        now=now,
                    )
                )
                result.stage_history_created += 1
            else:
                self._apply_updates(
                    existing,
                    transition=transition,
                    stage_id=resolved_stage_id,
                    from_stage_id=resolved_from_stage_id,
                    now=now,
                )
                existing.is_deleted = False
                mapping_rows.append(
                    self._source_mapping_row(
                        external_id=external_id,
                        transition=transition,
                        local_history_id=existing.history_id,
                        now=now,
                    )
                )
                result.stage_history_updated += 1

        if mapping_rows:
            await self.source_mappings.bulk_upsert_integration_mappings(mapping_rows)

        await self.session.flush()
        logger.info(
            "Salesforce stage history writer apply complete",
            extra={
                "organisation_id": str(self.organisation_id),
                "integration_id": str(self.integration_id),
                "stage_history_created": result.stage_history_created,
                "stage_history_updated": result.stage_history_updated,
                "unresolved_stage": result.unresolved_stage,
                "unresolved_from_stage": result.unresolved_from_stage,
            },
        )
        return result

    async def _load_existing_rows(
        self,
        external_ids: set[str] | None = None,
    ) -> dict[str, CRMDealStageHistory]:
        external_to_local = await self.source_mappings.load_local_id_map(
            object_type=CRMSourceObjectType.STAGE_HISTORY,
            external_ids=external_ids,
            include_deleted=True,
        )
        if not external_to_local:
            return {}
        rows: list[CRMDealStageHistory] = []
        for local_ids in iter_chunks(set(external_to_local.values()), LOCAL_ID_QUERY_CHUNK_SIZE):
            stmt = select(CRMDealStageHistory).where(
                col(CRMDealStageHistory.organisation_id) == self.organisation_id,
                col(CRMDealStageHistory.integration_id) == self.integration_id,
                col(CRMDealStageHistory.history_id).in_(local_ids),
            )
            rows.extend((await self.session.execute(stmt)).scalars().all())
        local_to_external = _local_to_external(external_to_local)
        return {
            local_to_external[row.history_id]: row
            for row in rows
            if row.history_id in local_to_external
        }

    def _source_mapping_row(
        self,
        *,
        external_id: str,
        transition: NormalizedSalesforceStageTransition,
        local_history_id: UUID,
        now: datetime,
    ) -> IntegrationMappingUpsert:
        return IntegrationMappingUpsert(
            external_object_type=CRMSourceObjectType.STAGE_HISTORY,
            external_id=external_id,
            local_object_type=CRMSourceObjectType.STAGE_HISTORY,
            local_object_id=local_history_id,
            data_payload=transition.data_payload,
            source_created_at=transition.entered_at,
            source_modified_at=transition.exited_at,
            synced_at=now,
        )

    def _new_row(
        self,
        *,
        transition: NormalizedSalesforceStageTransition,
        deal_id: UUID,
        stage_id: UUID | None,
        from_stage_id: UUID | None,
        now: datetime,
    ) -> CRMDealStageHistory:
        return CRMDealStageHistory(
            organisation_id=self.organisation_id,
            integration_id=self.integration_id,
            deal_id=deal_id,
            stage_id=stage_id,
            from_stage_id=from_stage_id,
            entered_at=transition.entered_at,
            exited_at=transition.exited_at,
            elapsed_seconds=transition.elapsed_seconds,
            amount_at_stage=transition.amount_at_stage,
            probability_at_stage=transition.probability_at_stage,
            source=transition.source,
            synced_at=now,
            last_seen_at=now,
        )

    def _apply_updates(
        self,
        row: CRMDealStageHistory,
        *,
        transition: NormalizedSalesforceStageTransition,
        stage_id: UUID | None,
        from_stage_id: UUID | None,
        now: datetime,
    ) -> None:
        row.stage_id = stage_id
        row.from_stage_id = from_stage_id
        row.exited_at = transition.exited_at
        row.elapsed_seconds = transition.elapsed_seconds
        row.amount_at_stage = transition.amount_at_stage
        row.probability_at_stage = transition.probability_at_stage
        row.source = transition.source
        row.synced_at = now
        row.last_seen_at = now


def _synthetic_external_id(transition: NormalizedSalesforceStageTransition) -> str:
    return (
        f"{transition.deal_external_id}:{transition.stage_external_id}:"
        f"{transition.entered_at.isoformat()}"
    )


def _local_to_external(external_to_local: dict[str, UUID]) -> dict[UUID, str]:
    return {local_id: external_id for external_id, local_id in external_to_local.items()}
