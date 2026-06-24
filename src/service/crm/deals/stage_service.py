"""Stage transition helpers for internal CRM deals."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealStage,
    CRMDealStageHistory,
)
from src.service.crm.deals.contracts import DealStageChangeResult
from src.service.crm.deals.mutation_event_service import changed_field


class DealStageService:
    """Owns stage timestamp and stage-history mutations for internal deals."""

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id

    async def apply_stage(
        self,
        *,
        deal: CRMDeal,
        stage: CRMDealStage,
        source: str = "api",
    ) -> DealStageChangeResult:
        """Move a loaded deal to a loaded stage without loading either row again."""
        if deal.stage_id == stage.stage_id:
            return DealStageChangeResult(
                deal=deal,
                changed_fields={},
                changed=False,
                stage_history=None,
            )

        old_stage_id = deal.stage_id
        old_stage_name = deal.stage_name
        now = datetime.now(UTC)
        self._apply_stage_update(deal=deal, stage=stage, now=now)
        history = self._record_stage_history(
            deal=deal,
            stage=stage,
            now=now,
            source=source,
            from_stage_id=old_stage_id,
        )

        changed_fields: dict[str, dict[str, Any]] = {}
        if change := changed_field(old_stage_id, stage.stage_id):
            changed_fields["stage_id"] = change
        if change := changed_field(old_stage_name, stage.name):
            changed_fields["stage_name"] = change

        await self.session.flush()
        return DealStageChangeResult(
            deal=deal,
            changed_fields=changed_fields,
            changed=True,
            stage_history=history,
        )

    @staticmethod
    def _apply_stage_update(*, deal: CRMDeal, stage: CRMDealStage, now: datetime) -> None:
        deal.stage_id = stage.stage_id
        deal.stage_name = stage.name
        deal.stage_entered_at = now
        deal.stage_updated_at = now
        deal.source_modified_at = now
        deal.synced_at = now
        deal.last_seen_at = now

    def _record_stage_history(
        self,
        *,
        deal: CRMDeal,
        stage: CRMDealStage,
        now: datetime,
        source: str,
        from_stage_id: UUID | None,
    ) -> CRMDealStageHistory:
        history = CRMDealStageHistory(
            history_id=uuid4(),
            organisation_id=self.organisation_id,
            integration_id=deal.integration_id,
            deal_id=deal.deal_id,
            stage_id=stage.stage_id,
            from_stage_id=from_stage_id,
            entered_at=now,
            source=source,
            synced_at=now,
            last_seen_at=now,
        )
        self.session.add(history)
        return history
