"""Idempotent upsert of CRMDealStageHistory rows from `propertiesWithHistory`.

Each transition becomes one row. We synthesise an `external_id` of
`{deal_external_id}:{stage_external_id}:{entered_at_iso}` so the same
transition observed across multiple syncs maps to the same row (HubSpot's
deal API doesn't expose a per-transition id in v3).

Resolution:

  * `deal_id`        ← `{deal_external_id → deal_id}` map produced after
                       deals are flushed.
  * `stage_id`       ← `stage_external_id` via `stage_map`.
  * `from_stage_id`  ← `from_stage_external_id` via `stage_map`.

Misses leave the FK NULL, keep the `*_external_id` populated, and tick the
matching `unresolved_*` counter on `StageHistoryWriteResult`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStageHistory
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.hubspot.deals.deal_types import (
    DimensionHit,
    NormalizedStageTransition,
    StageHistoryWriteResult,
)

logger = get_logger(__name__)


class HubSpotDealStageHistoryWriter:
    """Writer for `crm_deal_stage_history` rows."""

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

    async def apply(
        self,
        *,
        transitions: list[NormalizedStageTransition],
        deal_pk_map: dict[str, UUID],
        stage_map: dict[str, DimensionHit],
    ) -> StageHistoryWriteResult:
        result = StageHistoryWriteResult()
        if not transitions:
            await self.session.flush()
            return result

        now = datetime.now(UTC)
        existing_by_external = await self._load_existing_rows()

        for transition in transitions:
            deal_id = deal_pk_map.get(transition.deal_external_id)
            if deal_id is None:
                # Deal write must have failed for this row (PK not yet known
                # in this session). Skip — next sync will pick it up.
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
                self.session.add(
                    self._new_row(
                        transition=transition,
                        external_id=external_id,
                        deal_id=deal_id,
                        stage_id=resolved_stage_id,
                        from_stage_id=resolved_from_stage_id,
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
                result.stage_history_updated += 1

        await self.session.flush()
        logger.info(
            "Stage history writer apply complete",
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

    async def _load_existing_rows(self) -> dict[str, CRMDealStageHistory]:
        stmt = select(CRMDealStageHistory).where(
            col(CRMDealStageHistory.organisation_id) == self.organisation_id,
            col(CRMDealStageHistory.integration_id) == self.integration_id,
            col(CRMDealStageHistory.is_deleted) == False,  # noqa: E712
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return {r.external_id: r for r in rows if r.external_id is not None}

    def _new_row(
        self,
        *,
        transition: NormalizedStageTransition,
        external_id: str,
        deal_id: UUID,
        stage_id: UUID | None,
        from_stage_id: UUID | None,
        now: datetime,
    ) -> CRMDealStageHistory:
        return CRMDealStageHistory(
            organisation_id=self.organisation_id,
            integration_id=self.integration_id,
            external_id=external_id,
            deal_id=deal_id,
            stage_id=stage_id,
            from_stage_id=from_stage_id,
            stage_external_id=transition.stage_external_id,
            from_stage_external_id=transition.from_stage_external_id,
            entered_at=transition.entered_at,
            exited_at=transition.exited_at,
            elapsed_seconds=transition.elapsed_seconds,
            amount_at_stage=_decimal_to_float(transition.amount_at_stage),
            probability_at_stage=transition.probability_at_stage,
            source=transition.source,
            synced_at=now,
            last_seen_at=now,
            data_payload={
                "stage_external_id": transition.stage_external_id,
                "from_stage_external_id": transition.from_stage_external_id,
                "entered_at": transition.entered_at.isoformat(),
                "exited_at": (transition.exited_at.isoformat() if transition.exited_at else None),
                "source": transition.source,
            },
        )

    def _apply_updates(
        self,
        row: CRMDealStageHistory,
        *,
        transition: NormalizedStageTransition,
        stage_id: UUID | None,
        from_stage_id: UUID | None,
        now: datetime,
    ) -> None:
        # entered_at is part of the synthetic key — never re-write it.
        # Everything else can shift if HubSpot revises history retroactively.
        row.stage_id = stage_id
        row.from_stage_id = from_stage_id
        row.from_stage_external_id = transition.from_stage_external_id
        row.exited_at = transition.exited_at
        row.elapsed_seconds = transition.elapsed_seconds
        row.amount_at_stage = _decimal_to_float(transition.amount_at_stage)
        row.probability_at_stage = transition.probability_at_stage
        row.source = transition.source
        row.synced_at = now
        row.last_seen_at = now


def _synthetic_external_id(transition: NormalizedStageTransition) -> str:
    """Stable id across re-syncs for the same transition.

    HubSpot's v3 deal API doesn't expose a per-transition identifier; the
    `(deal, stage, entered_at)` triple is unique because HubSpot timestamps
    transitions at millisecond granularity and a deal can't simultaneously
    transition into the same stage twice.
    """
    return (
        f"{transition.deal_external_id}:{transition.stage_external_id}:"
        f"{transition.entered_at.isoformat()}"
    )


def _decimal_to_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)  # type: ignore[arg-type]
