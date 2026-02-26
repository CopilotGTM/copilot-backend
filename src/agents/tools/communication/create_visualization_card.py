"""Create and send a visualization summary card.

Supports Teams (native Adaptive Card charts) and Slack (text-based tables).
Mirrors the existing form tool pattern:
- Build a channel-agnostic visualization definition
- Render via channel card builder
- Send via proactive payload using platform context metadata

The tool is intended for:
- Explicit user requests for charts/visualizations
- Situations where quantitative data is better communicated visually
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from src.adapters.card_builders import (
    VisualizationDefinition,
    VisualizationPoint,
    VisualizationSeries,
    get_visualization_card_builder,
)
from src.agents.tools.base import execute_with_capabilities

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

_CANONICAL_CHART_TYPES = {
    "bar",
    "horizontal_bar",
    "line",
    "pie",
    "donut",
    "grouped_bar",
    "stacked_bar",
    "stacked_horizontal_bar",
}
_CHART_TYPE_ALIASES = {
    "vertical_bar": "bar",
    "column": "bar",
    "funnel": "horizontal_bar",
    "table_combo": "grouped_bar",
    "timeline": "line",
    "doughnut": "donut",
    "stacked": "stacked_bar",
    "grouped": "grouped_bar",
    "stacked_horizontal": "stacked_horizontal_bar",
}
_SERIES_NAME_KEYS = ("name", "series_name", "title", "label")
_SERIES_POINTS_KEYS = ("points", "data", "values", "items", "rows")
_POINT_LABEL_KEYS = (
    "label",
    "x",
    "name",
    "category",
    "organisation",
    "organization",
    "org",
    "company",
    "account",
    "deal",
    "stage",
    "title",
)
_POINT_VALUE_KEYS = (
    "value",
    "y",
    "amount",
    "revenue",
    "deal_size",
    "size",
    "total",
    "sum",
    "count",
)


def _coerce_numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()

    text = text.replace(",", "")
    match = re.search(r"[-+]?\d*\.?\d+", text)
    if not match:
        return None

    numeric = float(match.group(0))
    suffix_match = re.search(rf"{re.escape(match.group(0))}\s*([kKmMbBtT])", text)
    if suffix_match:
        multiplier_map = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}
        numeric *= multiplier_map[suffix_match.group(1).lower()]

    if negative:
        numeric = -numeric

    return numeric


def _first_present(obj: dict[str, Any], keys: tuple[str, ...]) -> Any | None:
    for key in keys:
        value = obj.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _extract_point(point: dict[str, Any]) -> VisualizationPoint | None:
    label = _first_present(point, _POINT_LABEL_KEYS)
    value = _first_present(point, _POINT_VALUE_KEYS)

    # Fallback heuristic for slightly different point shapes from the agent:
    # first non-empty string-like field as label and first numeric-like field as value.
    if label is None:
        for candidate in point.values():
            if isinstance(candidate, str) and candidate.strip():
                label = candidate.strip()
                break

    if value is None:
        for candidate in point.values():
            if _coerce_numeric(candidate) is not None:
                value = candidate
                break

    if label is None or value is None:
        return None
    return VisualizationPoint(label=str(label), value=value)


def _normalize_series(series: list[dict[str, Any]]) -> list[VisualizationSeries]:
    normalized: list[VisualizationSeries] = []
    for idx, series_item in enumerate(series):
        if not isinstance(series_item, dict):
            continue

        series_name = str(_first_present(series_item, _SERIES_NAME_KEYS) or f"Series {idx + 1}")
        points_raw: Any = _first_present(series_item, _SERIES_POINTS_KEYS)
        points: list[VisualizationPoint] = []

        if isinstance(points_raw, list):
            for point in points_raw:
                if not isinstance(point, dict):
                    continue
                normalized_point = _extract_point(point)
                if normalized_point is None:
                    continue
                points.append(normalized_point)
        else:
            # Support payloads where the series item itself is a point-like object.
            normalized_point = _extract_point(series_item)
            if normalized_point is not None:
                points.append(normalized_point)

        if points:
            normalized.append(VisualizationSeries(name=series_name, points=points))

    return normalized


def _build_visualization_summary_text(
    *,
    title: str,
    chart_type: str,
    subtitle: str | None,
    insight_text: str | None,
    series: list[VisualizationSeries],
) -> str:
    lines: list[str] = [title.strip() or "Visualization", f"Chart type: {chart_type}"]
    if subtitle:
        lines.append(subtitle.strip())
    if insight_text:
        lines.append(f"Key insight: {insight_text.strip()}")

    top_points: list[str] = []
    for series_item in series:
        for point in series_item.points[:10]:
            numeric = _coerce_numeric(point.value)
            if numeric is None:
                continue
            top_points.append(f"- {point.label}: {numeric:,.2f}")
        if top_points:
            break

    if top_points:
        lines.append("Top points:")
        lines.extend(top_points)
    else:
        lines.append("No numeric points were available for chart rendering.")

    return "\n".join(lines)


def _normalize_chart_type(chart_type: str) -> str:
    normalized = (chart_type or "").strip().lower()
    if not normalized:
        return "bar"

    if normalized in _CHART_TYPE_ALIASES:
        normalized = _CHART_TYPE_ALIASES[normalized]
    if normalized in _CANONICAL_CHART_TYPES:
        return normalized
    return "bar"


def _has_numeric_points(series: list[VisualizationSeries]) -> bool:
    for series_item in series:
        for point in series_item.points:
            if _coerce_numeric(point.value) is not None:
                return True
    return False


async def create_visualization_card(
    *,
    context: AgentContext,
    title: str,
    chart_type: str = "bar",
    series: list[dict[str, Any]] | None = None,
    subtitle: str | None = None,
    insight_text: str | None = None,
) -> dict[str, Any] | None:
    """Create and send a visualization summary card (Teams or Slack).

    Args:
        context: Agent context containing org and channel metadata.
        title: Card/chart title.
        chart_type: Chart type (bar, horizontal_bar, line, pie, donut, grouped_bar,
            stacked_bar, stacked_horizontal_bar). Common aliases
            (funnel, timeline, table_combo, etc.) are normalized.
        series: Chart series list where each item is:
            {"name": "...", "points": [{"label"|"x": "...", "value"|"y": ...}, ...]}
        subtitle: Optional subtitle.
        insight_text: Optional short explanation of the key takeaway.
    """
    if not context.organisation_id:
        logger.warning("create_visualization_card: missing organisation_id in context")
        return None

    channel = getattr(context, "channel", "teams") or "teams"

    normalized_chart_type = _normalize_chart_type(chart_type)

    normalized_series = _normalize_series(series or [])
    if not normalized_series or not _has_numeric_points(normalized_series):
        fallback_info = (insight_text or subtitle or "").strip()
        message = (
            "Unable to create a chart because usable numeric chart points were not provided. "
            "I can still share the summary as text."
        )
        if fallback_info:
            return {
                "status": "no_data",
                "message": message,
                "fallback_text": fallback_info,
                "chart_type": normalized_chart_type,
            }
        return {
            "status": "no_data",
            "message": message,
            "chart_type": normalized_chart_type,
        }

    viz_def = VisualizationDefinition(
        title=title,
        chart_type=normalized_chart_type,
        subtitle=subtitle,
        insight_text=insight_text,
        series=normalized_series,
    )

    summary_text = _build_visualization_summary_text(
        title=title,
        chart_type=normalized_chart_type,
        subtitle=subtitle,
        insight_text=insight_text,
        series=normalized_series,
    )

    if channel == "slack":
        return await _send_slack_visualization(
            context, viz_def, summary_text, normalized_chart_type
        )

    return await _send_teams_visualization(context, viz_def, summary_text, normalized_chart_type)


async def _send_teams_visualization(
    context: AgentContext,
    viz_def: VisualizationDefinition,
    summary_text: str,
    chart_type: str,
) -> dict[str, Any]:
    """Send visualization via Teams Adaptive Card."""
    service_url = context.metadata.get("service_url", "")
    conversation_id = context.metadata.get("conversation_id", "")
    if not service_url or not conversation_id:
        logger.warning("create_visualization_card: missing Teams context metadata")
        return {
            "status": "error",
            "message": "Cannot send visualization card: missing Teams channel context.",
        }

    card_builder = get_visualization_card_builder("teams")
    chart_payload = card_builder.build_visualization(viz_def)

    from src.service.bots.teams_bot_service import TeamsBotService

    bot_service = TeamsBotService()
    sent = await bot_service.send_proactive_payload(
        service_url=service_url,
        conversation_id=conversation_id,
        payload=chart_payload,
        text_fallback=summary_text,
    )
    if not sent:
        logger.error("create_visualization_card: failed to send Teams payload")
        return {
            "status": "error",
            "message": "Failed to send visualization card. Please try again.",
        }

    return {"status": "sent", "message": "I sent a Teams chart card.", "chart_type": chart_type}


async def _send_slack_visualization(
    context: AgentContext,
    viz_def: VisualizationDefinition,
    summary_text: str,
    chart_type: str,
) -> dict[str, Any]:
    """Send visualization via Slack Block Kit."""
    channel_id = context.metadata.get("channel_id", "")
    team_id = context.metadata.get("team_id", "")

    if not channel_id:
        logger.warning("create_visualization_card: missing Slack channel_id in context metadata")
        return {
            "status": "error",
            "message": "Cannot send visualization card: missing Slack channel context.",
        }

    card_builder = get_visualization_card_builder("slack")
    chart_payload = card_builder.build_visualization(viz_def)

    from src.service.bots.slack_bot_service import SlackBotService

    bot_service = SlackBotService()
    blocks = chart_payload.get("blocks", [])
    if blocks and team_id:
        sent = await bot_service.send_block_kit_message(
            team_id=team_id,
            channel_id=channel_id,
            blocks=blocks,
            text_fallback=summary_text,
        )
    else:
        sent = (
            await bot_service._send_message(team_id, channel_id, summary_text) if team_id else False
        )

    if not sent:
        logger.error("create_visualization_card: failed to send Slack payload")
        return {
            "status": "error",
            "message": "Failed to send visualization card. Please try again.",
        }

    return {"status": "sent", "message": "I sent a Slack visualization.", "chart_type": chart_type}


def create_create_visualization_card(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create create_visualization_card tool bound to context."""

    async def create_visualization_card_tool(
        title: str,
        chart_type: str = "bar",
        series: list[dict[str, Any]] | None = None,
        subtitle: str | None = None,
        insight_text: str | None = None,
    ) -> dict[str, Any] | None:
        """Send a visualization summary card (Teams chart or Slack text table).

        Use this tool only when you have actual chart datapoints in `series`.
        If `series` is missing or empty, this tool returns `status=no_data`
        with a clear "unable to create chart" message and optional fallback text.
        """
        logger.info(
            "[TOOL CALLED] create_visualization_card - chart_type=%s series_count=%d",
            chart_type,
            len(series or []),
        )

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="CreateVisualizationCard",
            tool_func=create_visualization_card,
            parameters={
                "context": context,
                "title": title,
                "chart_type": chart_type,
                "series": series,
                "subtitle": subtitle,
                "insight_text": insight_text,
            },
            description="Sending Teams visualization summary card",
            fallback_value=None,
        )
        return dict(result) if result else None

    return create_visualization_card_tool


__all__ = ["create_visualization_card", "create_create_visualization_card"]
