"""Boundary tests proving manual sync suppresses auto contact research dispatch."""

from uuid import uuid4

import pytest
from src.workers.deal_worker.services.deal_mapper_services.deal_email_matcher import (
    DealEmailMatcher,
)
from src.workers.deal_worker.services.deal_mapper_services.deal_meeting_matcher import (
    DealMeetingMatcher,
)
from src.workers.deal_worker.services.deal_mapper_services.stakeholder_sync_service import (
    StakeholderSyncService,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "service_cls",
    [DealEmailMatcher, DealMeetingMatcher, StakeholderSyncService],
)
async def test_manual_stakeholder_processing_can_suppress_auto_contact_research(
    service_cls,
    monkeypatch,
):
    """Visible stakeholder-created processing must not start contact research."""
    calls = []

    async def fake_dispatch_contact_research_for_contact(**kwargs):
        """Capture any call that would trigger async research dispatch."""
        calls.append(kwargs)

    monkeypatch.setattr(
        "src.workers.deal_worker.utils.contact_research_dispatch.dispatch_contact_research_for_contact",
        fake_dispatch_contact_research_for_contact,
    )

    service = service_cls(uuid4(), suppress_research_dispatch=True)
    service._pending_research.append(uuid4())

    await service._dispatch_pending_research()

    assert calls == []
    assert service._pending_research == []
