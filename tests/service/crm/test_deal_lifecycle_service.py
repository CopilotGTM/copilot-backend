from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStatus
from src.service.crm.deals.lifecycle_service import DealLifecycleService


def test_close_loaded_deal_as_won_clears_stale_lost_reason() -> None:
    lost_at = datetime(2026, 6, 19, 10, 0, tzinfo=UTC)
    deal = SimpleNamespace(
        deal_id=uuid4(),
        deal_status=CRMDealStatus.LOST,
        close_date=lost_at,
        closed_won_at=None,
        closed_lost_at=lost_at,
        closed_reason="No budget",
    )

    result = DealLifecycleService().close_loaded_deal(
        deal=deal,
        status=CRMDealStatus.WON,
        closed_reason="Contract signed",
    )

    assert deal.deal_status == CRMDealStatus.WON
    assert deal.closed_won_at is not None
    assert deal.closed_lost_at is None
    assert deal.closed_reason is None
    assert result.changed_fields["closed_reason"] == {"old": "No budget", "new": None}
