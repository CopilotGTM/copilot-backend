"""Focused service tests for meeting-briefing context assembly."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.agents.workflows.briefing import BriefingData
from src.workers.deal_worker.services.meeting_insight_services.meeting_prep_service import (
    MeetingPrepService,
)


@pytest.mark.asyncio
async def test_generate_passes_seller_context_to_briefing_input(monkeypatch) -> None:
    """Configured organisation context must survive the worker-to-agent boundary."""
    organisation_id = uuid4()
    deal_id = uuid4()
    event_id = uuid4()
    captured: dict[str, object] = {}

    class FakeSession:
        async def commit(self) -> None:
            return None

    async def fake_master_sessions():
        yield FakeSession()

    async def fake_org_sessions(_organisation_id):
        yield FakeSession()

    class FakeBriefingAgent:
        last_token_usage = None

        def __init__(self, **kwargs):
            captured["agent_kwargs"] = kwargs

        async def generate(self, input_data):
            captured["input_data"] = input_data
            return BriefingData(briefing_overview="# Prep for Acme")

    event = SimpleNamespace(
        title="Acme discussion",
        description=None,
        start_at=None,
        end_at=None,
        organiser_name="Seller Rep",
        organiser_email="rep@seller.example",
        attendee_count=0,
        attendees=[],
    )
    deal = SimpleNamespace(name="Acme opportunity", company_name="Acme")

    module = "src.workers.deal_worker.services.meeting_insight_services.meeting_prep_service"
    monkeypatch.setattr(f"{module}.acquire_master_async_session", fake_master_sessions)
    monkeypatch.setattr(f"{module}.acquire_org_async_session", fake_org_sessions)
    monkeypatch.setattr(
        f"{module}.get_organisation_briefing_context",
        AsyncMock(
            return_value=(
                "Example Seller",
                ["https://seller.example"],
                "Seller helps revenue teams automate account workflows.",
            )
        ),
    )
    monkeypatch.setattr(f"{module}.get_deal", AsyncMock(return_value=deal))
    monkeypatch.setattr(f"{module}.get_event", AsyncMock(return_value=event))
    monkeypatch.setattr(f"{module}.get_association", AsyncMock(return_value=None))
    monkeypatch.setattr(f"{module}.count_prior_meetings_for_briefing", AsyncMock(return_value=0))
    monkeypatch.setattr(f"{module}.count_emails_for_briefing", AsyncMock(return_value=0))
    monkeypatch.setattr("src.agents.workflows.briefing.BriefingAgent", FakeBriefingAgent)

    result = await MeetingPrepService(
        organisation_id=organisation_id,
        deal_id=deal_id,
        event_id=event_id,
    ).generate()

    input_data = captured["input_data"]
    assert input_data.organisation_website_urls == ["https://seller.example"]
    assert input_data.organisation_summary == (
        "Seller helps revenue teams automate account workflows."
    )
    assert captured["agent_kwargs"]["org_name"] == "Example Seller"
    assert result["data"]["briefing_overview"] == "# Prep for Acme"
