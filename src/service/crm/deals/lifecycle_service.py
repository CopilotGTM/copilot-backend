"""Lifecycle helpers for closing and reopening internal CRM deals."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealStatus
from src.service.crm.deals.contracts import DealLifecycleChangeResult
from src.service.crm.deals.mutation_event_service import changed_field


class DealLifecycleService:
    """Owns close/reopen field changes once the write service loads a deal."""

    def close_loaded_deal(
        self,
        *,
        deal: CRMDeal,
        status: CRMDealStatus,
        closed_reason: str | None = None,
    ) -> DealLifecycleChangeResult:
        """Apply lifecycle fields to a loaded deal and return the changed fields."""
        old_status = deal.deal_status
        old_won_at = deal.closed_won_at
        old_lost_at = deal.closed_lost_at
        old_reason = deal.closed_reason
        now = datetime.now(UTC)

        deal.deal_status = status
        deal.close_date = now
        if status == CRMDealStatus.WON:
            deal.closed_won_at = now
            deal.closed_lost_at = None
        elif status == CRMDealStatus.LOST:
            deal.closed_lost_at = now
            deal.closed_won_at = None
        deal.closed_reason = closed_reason if status == CRMDealStatus.LOST else None

        changed_fields: dict[str, dict[str, Any]] = {}
        for name, old, new in (
            ("deal_status", old_status, deal.deal_status),
            ("closed_won_at", old_won_at, deal.closed_won_at),
            ("closed_lost_at", old_lost_at, deal.closed_lost_at),
            ("closed_reason", old_reason, deal.closed_reason),
        ):
            if change := changed_field(old, new):
                changed_fields[name] = change

        return DealLifecycleChangeResult(
            deal=deal,
            changed_fields=changed_fields,
            semantic_event_type="crm.deal.closed" if changed_fields else None,
            changed=bool(changed_fields),
        )
