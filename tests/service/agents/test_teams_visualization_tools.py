from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from src.adapters.card_builders import (
    TeamsVisualizationCardBuilder,
    VisualizationDefinition,
    VisualizationPoint,
    VisualizationSeries,
)
from src.agents.tools.communication.create_visualization_card import (
    create_create_visualization_card,
    create_visualization_card,
)
from src.agents.tools.registry import AGENT_TOOLS, CHANNEL_ONLY_TOOLS, ToolName


@dataclass
class _ContextStub:
    organisation_id: UUID
    channel: str = "teams"
    metadata: dict[str, str] = field(default_factory=dict)


def test_registry_exposes_single_visualization_tool_for_teams() -> None:
    general_tools = AGENT_TOOLS["GeneralAgent"]
    assert ToolName.CREATE_VISUALIZATION_CARD in general_tools
    assert CHANNEL_ONLY_TOOLS[ToolName.CREATE_VISUALIZATION_CARD] == {"teams"}


def test_teams_visualization_card_builder_renders_native_chart_and_optional_open_action() -> None:
    builder = TeamsVisualizationCardBuilder()
    payload = builder.build_visualization(
        VisualizationDefinition(
            title="Pipeline by Stage",
            chart_type="bar",
            subtitle="Current quarter",
            insight_text="Negotiation has the highest count.",
            series=[
                VisualizationSeries(
                    name="Deals",
                    points=[
                        VisualizationPoint(label="Prospecting", value="₹9.0M"),
                        VisualizationPoint(label="Negotiation", value="₹14.0M"),
                    ],
                )
            ],
            interactive_url="https://app.example.com/chat/visualizations?view_id=abc",
        )
    )

    attachment = payload["attachments"][0]
    card = attachment["content"]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert card["type"] == "AdaptiveCard"
    assert card["version"] == "1.5"
    assert card["actions"][0]["type"] == "Action.OpenUrl"
    assert card["actions"][0]["url"].startswith("https://app.example.com/")
    chart_elements = [
        block
        for block in card["body"]
        if isinstance(block, dict) and block.get("type", "").startswith("Chart.")
    ]
    assert chart_elements
    assert chart_elements[0]["type"] == "Chart.VerticalBar"


@pytest.mark.asyncio
async def test_create_visualization_card_requires_teams_reply_context() -> None:
    context = _ContextStub(organisation_id=uuid4(), metadata={})

    result = await create_visualization_card(
        context=context,  # type: ignore[arg-type]
        title="Deal Velocity",
        chart_type="line",
        series=[{"name": "Weekly", "points": [{"label": "W1", "value": 3}]}],
    )

    assert result is not None
    assert result["status"] == "error"
    assert "missing Teams channel context" in result["message"]


@pytest.mark.asyncio
async def test_create_visualization_tool_requires_series_and_returns_fallback() -> None:
    context = _ContextStub(
        organisation_id=uuid4(),
        metadata={"service_url": "https://smba.trafficmanager.net/amer/", "conversation_id": "abc"},
    )
    tool = create_create_visualization_card(context, lambda: None)  # type: ignore[arg-type]
    send_mock = AsyncMock(return_value=True)

    with patch(
        "src.service.bots.teams_bot_service.TeamsBotService.send_proactive_payload",
        new=send_mock,
    ):
        result = await tool(
            title="Open deals by stage",
            insight_text="Most active stage is Discovery/Proposal.",
        )

    assert result is not None
    assert result["status"] == "no_data"
    assert "Unable to create a chart" in result["message"]
    assert "Most active stage" in result["fallback_text"]
    assert result["chart_type"] == "bar"
    assert send_mock.await_count == 0


@pytest.mark.asyncio
async def test_create_visualization_tool_normalizes_organisation_amount_points() -> None:
    context = _ContextStub(
        organisation_id=uuid4(),
        metadata={"service_url": "https://smba.trafficmanager.net/amer/", "conversation_id": "abc"},
    )
    tool = create_create_visualization_card(context, lambda: None)  # type: ignore[arg-type]
    send_mock = AsyncMock(return_value=True)

    with patch(
        "src.service.bots.teams_bot_service.TeamsBotService.send_proactive_payload",
        new=send_mock,
    ):
        result = await tool(
            title="Top 10 revenue organisations",
            chart_type="bar",
            series=[
                {
                    "name": "Open pipeline (INR)",
                    "data": [
                        {"organisation": "Synergy systems & Solutions", "amount": "₹679.1M"},
                        {"organisation": "RVPNL", "amount": "₹350.0M"},
                    ],
                }
            ],
            insight_text="Revenue is concentrated in top accounts.",
        )

    assert result is not None
    assert result["status"] == "sent"
    payload = send_mock.await_args.kwargs["payload"]
    assert "attachments" in payload
    card = payload["attachments"][0]["content"]
    chart_elements = [
        block
        for block in card["body"]
        if isinstance(block, dict) and block.get("type", "").startswith("Chart.")
    ]
    assert chart_elements
    first_chart = chart_elements[0]
    assert first_chart["type"] == "Chart.VerticalBar"
    assert first_chart["data"][0]["x"] == "Synergy systems & Solutions"


@pytest.mark.asyncio
async def test_create_visualization_tool_supports_grouped_bar_chart_type() -> None:
    context = _ContextStub(
        organisation_id=uuid4(),
        metadata={"service_url": "https://smba.trafficmanager.net/amer/", "conversation_id": "abc"},
    )
    tool = create_create_visualization_card(context, lambda: None)  # type: ignore[arg-type]
    send_mock = AsyncMock(return_value=True)

    with patch(
        "src.service.bots.teams_bot_service.TeamsBotService.send_proactive_payload",
        new=send_mock,
    ):
        result = await tool(
            title="Quarterly Revenue Comparison",
            chart_type="grouped_bar",
            series=[
                {
                    "name": "Q1",
                    "points": [
                        {"label": "Axis Bank", "value": 120},
                        {"label": "RVPNL", "value": 80},
                    ],
                },
                {
                    "name": "Q2",
                    "points": [
                        {"label": "Axis Bank", "value": 140},
                        {"label": "RVPNL", "value": 95},
                    ],
                },
            ],
        )

    assert result is not None
    assert result["status"] == "sent"
    payload = send_mock.await_args.kwargs["payload"]
    card = payload["attachments"][0]["content"]
    chart_elements = [
        block
        for block in card["body"]
        if isinstance(block, dict) and block.get("type", "").startswith("Chart.")
    ]
    assert chart_elements[0]["type"] == "Chart.VerticalBar.Grouped"
    assert chart_elements[0]["data"][0]["legend"] == "Q1"


@pytest.mark.asyncio
async def test_create_visualization_tool_supports_donut_chart_type() -> None:
    context = _ContextStub(
        organisation_id=uuid4(),
        metadata={"service_url": "https://smba.trafficmanager.net/amer/", "conversation_id": "abc"},
    )
    tool = create_create_visualization_card(context, lambda: None)  # type: ignore[arg-type]
    send_mock = AsyncMock(return_value=True)

    with patch(
        "src.service.bots.teams_bot_service.TeamsBotService.send_proactive_payload",
        new=send_mock,
    ):
        result = await tool(
            title="Pipeline Share",
            chart_type="donut",
            series=[
                {
                    "name": "Open deals",
                    "points": [
                        {"label": "Discovery", "value": 11},
                        {"label": "Proposal", "value": 9},
                    ],
                }
            ],
        )

    assert result is not None
    assert result["status"] == "sent"
    payload = send_mock.await_args.kwargs["payload"]
    card = payload["attachments"][0]["content"]
    chart_elements = [
        block
        for block in card["body"]
        if isinstance(block, dict) and block.get("type", "").startswith("Chart.")
    ]
    assert chart_elements[0]["type"] == "Chart.Donut"


@pytest.mark.asyncio
async def test_create_visualization_tool_returns_no_data_when_values_not_numeric() -> None:
    context = _ContextStub(
        organisation_id=uuid4(),
        metadata={"service_url": "https://smba.trafficmanager.net/amer/", "conversation_id": "abc"},
    )
    tool = create_create_visualization_card(context, lambda: None)  # type: ignore[arg-type]
    send_mock = AsyncMock(return_value=True)

    with patch(
        "src.service.bots.teams_bot_service.TeamsBotService.send_proactive_payload",
        new=send_mock,
    ):
        result = await tool(
            title="Open deals by stage",
            chart_type="bar",
            series=[
                {
                    "name": "Open deals",
                    "points": [{"stage": "Discovery", "count": "N/A"}],
                }
            ],
            insight_text="Discovery is currently the most active stage.",
        )

    assert result is not None
    assert result["status"] == "no_data"
    assert "Unable to create a chart" in result["message"]
    assert result["fallback_text"] == "Discovery is currently the most active stage."
    assert send_mock.await_count == 0
