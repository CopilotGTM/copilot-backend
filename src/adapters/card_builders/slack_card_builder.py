"""Slack Block Kit card builders.

Renders channel-agnostic definitions into Slack Block Kit payloads.
Since Slack does not support native chart elements, visualizations are
rendered as formatted text tables with bar indicators.
"""

from __future__ import annotations

import json
from typing import Any

from src.adapters.card_builders.base import (
    FormCardBuilder,
    FormDefinition,
    VisualizationCardBuilder,
    VisualizationDefinition,
    VisualizationPoint,
)


def _coerce_numeric(value: float | int | str) -> float | None:
    """Coerce a point value to float."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _bar_indicator(value: float, max_value: float, width: int = 10) -> str:
    """Create a text-based bar indicator."""
    if max_value <= 0:
        return ""
    ratio = min(abs(value) / max_value, 1.0)
    filled = round(ratio * width)
    return "\u2588" * filled + "\u2591" * (width - filled)


def _format_number(value: float) -> str:
    """Format a number for display (compact notation)."""
    abs_val = abs(value)
    sign = "-" if value < 0 else ""
    if abs_val >= 1_000_000_000:
        return f"{sign}{abs_val / 1_000_000_000:.1f}B"
    if abs_val >= 1_000_000:
        return f"{sign}{abs_val / 1_000_000:.1f}M"
    if abs_val >= 1_000:
        return f"{sign}{abs_val / 1_000:.1f}K"
    if abs_val == int(abs_val):
        return f"{sign}{int(abs_val)}"
    return f"{sign}{abs_val:,.2f}"


class SlackVisualizationCardBuilder(VisualizationCardBuilder):
    """Renders visualizations as text-based Block Kit messages.

    Since Slack has no native chart elements, charts are rendered as
    formatted text with bar indicators and aligned columns.
    """

    def build_visualization(self, visualization: VisualizationDefinition) -> dict[str, Any]:
        """Build Slack Block Kit payload from a VisualizationDefinition."""
        blocks: list[dict[str, Any]] = []

        # Title
        blocks.append(
            {
                "type": "header",
                "text": {"type": "plain_text", "text": visualization.title[:150]},
            }
        )

        # Subtitle
        if visualization.subtitle:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": visualization.subtitle},
                }
            )

        # Render chart data as text
        chart_text = self._render_chart_text(visualization)
        if chart_text:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": chart_text},
                }
            )

        # Insight
        if visualization.insight_text:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": f":bulb: {visualization.insight_text}"},
                    ],
                }
            )

        return {"blocks": blocks, "text": visualization.title}

    def _render_chart_text(self, viz: VisualizationDefinition) -> str:
        """Render chart data as formatted text with bar indicators."""
        if not viz.series:
            return ""

        chart_type = viz.chart_type
        multi_series = len(viz.series) > 1

        if chart_type in ("pie", "donut"):
            return self._render_pie_text(viz)
        elif multi_series or chart_type in (
            "grouped_bar",
            "stacked_bar",
            "stacked_horizontal_bar",
            "line",
        ):
            return self._render_multi_series_text(viz)
        else:
            return self._render_single_series_text(viz)

    def _render_single_series_text(self, viz: VisualizationDefinition) -> str:
        """Render a single-series bar/horizontal_bar chart as text."""
        if not viz.series or not viz.series[0].points:
            return ""

        points = viz.series[0].points[:20]
        numeric_points = self._to_numeric_points(points)
        if not numeric_points:
            return ""

        max_val = max(abs(v) for _, v in numeric_points)
        max_label_len = max(len(label) for label, _ in numeric_points)

        lines: list[str] = []
        for label, value in numeric_points:
            bar = _bar_indicator(value, max_val)
            formatted = _format_number(value)
            lines.append(f"`{label:<{max_label_len}}` {bar} {formatted}")

        return "\n".join(lines)

    def _render_pie_text(self, viz: VisualizationDefinition) -> str:
        """Render a pie/donut chart as percentage breakdown."""
        if not viz.series or not viz.series[0].points:
            return ""

        points = viz.series[0].points[:12]
        numeric_points = self._to_numeric_points(points)
        if not numeric_points:
            return ""

        total = sum(abs(v) for _, v in numeric_points)
        if total == 0:
            return ""

        lines: list[str] = []
        for label, value in numeric_points:
            pct = (abs(value) / total) * 100
            bar = _bar_indicator(abs(value), total)
            lines.append(f"`{label}` {bar} {pct:.1f}% ({_format_number(value)})")

        return "\n".join(lines)

    def _render_multi_series_text(self, viz: VisualizationDefinition) -> str:
        """Render multi-series data as a table."""
        if not viz.series:
            return ""

        # Collect all labels across series
        all_labels: list[str] = []
        seen: set[str] = set()
        for s in viz.series:
            for p in s.points:
                if p.label not in seen:
                    all_labels.append(p.label)
                    seen.add(p.label)

        all_labels = all_labels[:15]
        series_list = viz.series[:5]

        # Build header
        series_names = [s.name for s in series_list]
        header = " | ".join(["*Label*"] + [f"*{n}*" for n in series_names])

        # Build rows
        rows: list[str] = []
        for label in all_labels:
            values: list[str] = [label]
            for s in series_list:
                point_map = {p.label: p for p in s.points}
                point = point_map.get(label)
                if point:
                    numeric = _coerce_numeric(point.value)
                    values.append(
                        _format_number(numeric) if numeric is not None else str(point.value)
                    )
                else:
                    values.append("-")
            rows.append(" | ".join(values))

        return header + "\n" + "\n".join(rows)

    @staticmethod
    def _to_numeric_points(points: list[VisualizationPoint]) -> list[tuple[str, float]]:
        """Convert points to (label, numeric_value) tuples, skipping non-numeric."""
        result: list[tuple[str, float]] = []
        for p in points:
            numeric = _coerce_numeric(p.value)
            if numeric is not None:
                result.append((p.label, numeric))
        return result


class SlackFormCardBuilder(FormCardBuilder):
    """Renders form definitions as Slack Block Kit input blocks."""

    def build(self, form: FormDefinition) -> dict[str, Any]:
        """Build Slack Block Kit payload from a FormDefinition."""
        blocks: list[dict[str, Any]] = []
        action_value = self._build_action_value(form.metadata)

        # Title
        blocks.append(
            {
                "type": "header",
                "text": {"type": "plain_text", "text": form.title[:150]},
            }
        )

        if form.subtitle:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": form.subtitle},
                }
            )

        # Fields
        for field in form.fields:
            block = self._build_field(field)
            if block:
                blocks.append(block)

        # Actions
        elements: list[dict[str, Any]] = [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": form.submit_label},
                "style": "primary",
                "action_id": form.submit_action,
                "value": action_value,
            }
        ]
        if form.cancel_action:
            elements.append(
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": form.cancel_label},
                    "style": "danger",
                    "action_id": form.cancel_action,
                    "value": action_value,
                }
            )
        blocks.append({"type": "actions", "elements": elements})

        return {"blocks": blocks, "text": form.title}

    @staticmethod
    def _build_field(field) -> dict[str, Any] | None:
        """Build a single Block Kit input block from a FormField."""
        if field.field_type == "choice" and field.choices:
            options = [
                {
                    "text": {"type": "plain_text", "text": c.get("title", c.get("label", ""))},
                    "value": str(c.get("value", c.get("id", ""))),
                }
                for c in field.choices
            ]
            if not options:
                return None
            element: dict[str, Any] = {
                "type": "static_select",
                "action_id": field.id,
                "placeholder": {"type": "plain_text", "text": field.placeholder or "Select"},
                "options": options,
            }
            if field.value is not None:
                initial_option = next((o for o in options if o["value"] == str(field.value)), None)
                if initial_option:
                    element["initial_option"] = initial_option
            return {
                "type": "input",
                "block_id": f"input_{field.id}",
                "label": {"type": "plain_text", "text": field.label},
                "optional": not field.required,
                "element": element,
            }
        elif field.field_type == "date":
            return {
                "type": "input",
                "block_id": f"input_{field.id}",
                "label": {"type": "plain_text", "text": field.label},
                "optional": not field.required,
                "element": {
                    "type": "datepicker",
                    "action_id": field.id,
                    **({"initial_date": str(field.value)} if field.value else {}),
                },
            }
        elif field.field_type == "toggle":
            is_checked = str(field.value).lower() in {"true", "1", "yes"} if field.value else False
            return {
                "type": "input",
                "block_id": f"input_{field.id}",
                "label": {"type": "plain_text", "text": field.label},
                "optional": not field.required,
                "element": {
                    "type": "checkboxes",
                    "action_id": field.id,
                    "options": [
                        {"text": {"type": "plain_text", "text": field.label}, "value": "true"}
                    ],
                    **(
                        {
                            "initial_options": [
                                {
                                    "text": {"type": "plain_text", "text": field.label},
                                    "value": "true",
                                }
                            ]
                        }
                        if is_checked
                        else {}
                    ),
                },
            }
        else:
            # text, number, email — all use plain_text_input
            return {
                "type": "input",
                "block_id": f"input_{field.id}",
                "label": {"type": "plain_text", "text": field.label},
                "optional": not field.required,
                "element": {
                    "type": "plain_text_input",
                    "action_id": field.id,
                    "multiline": field.field_type == "text" and field.multiline,
                    "placeholder": {
                        "type": "plain_text",
                        "text": field.placeholder or f"Enter {field.label}",
                    },
                    **({"initial_value": str(field.value)} if field.value is not None else {}),
                },
            }

    @staticmethod
    def _build_action_value(metadata: dict[str, str]) -> str:
        """Serialize form metadata into Slack button value."""
        try:
            return json.dumps(metadata or {}, separators=(",", ":"))
        except TypeError:
            return "{}"


__all__ = ["SlackVisualizationCardBuilder", "SlackFormCardBuilder"]
