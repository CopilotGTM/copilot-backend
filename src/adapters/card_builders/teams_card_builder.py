"""Teams Adaptive Card builder for interactive forms.

Converts channel-agnostic FormDefinition objects into Microsoft Teams
Adaptive Card JSON payloads using Action.Execute (Universal Actions).

Uses Action.Execute with Action.Submit fallback for backward compatibility
with older Teams clients. Action.Execute enables in-place card replacement
after form submission, preventing duplicate submissions.
"""

from __future__ import annotations

import re
from typing import Any

from src.adapters.card_builders.base import (
    FormCardBuilder,
    FormDefinition,
    FormField,
    VisualizationCardBuilder,
    VisualizationDefinition,
    VisualizationSeries,
)


class TeamsFormCardBuilder(FormCardBuilder):
    """Builds Teams Adaptive Card payloads from FormDefinition objects."""

    def build(self, form: FormDefinition) -> dict[str, Any]:
        """Convert a FormDefinition into a Teams Adaptive Card message payload.

        Returns a dict suitable for sending via Bot Framework v3 API,
        including the attachment wrapper.
        """
        body = self._build_body(form)
        actions = self._build_actions(form)
        card = self._build_card(body, actions)

        return {
            "text": " ",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": card,
                }
            ],
        }

    def _build_body(self, form: FormDefinition) -> list[dict[str, Any]]:
        """Build the Adaptive Card body elements."""
        body: list[dict[str, Any]] = []

        # Title
        body.append(
            {
                "type": "TextBlock",
                "text": form.title,
                "size": "Medium",
                "weight": "Bolder",
                "wrap": True,
            }
        )

        # Subtitle
        if form.subtitle:
            body.append(
                {
                    "type": "TextBlock",
                    "text": form.subtitle,
                    "size": "Small",
                    "isSubtle": True,
                    "wrap": True,
                    "spacing": "None",
                }
            )

        # Fields
        for field in form.fields:
            body.append(self._build_input(field))

        return body

    def _build_input(self, field: FormField) -> dict[str, Any]:
        """Convert a FormField into an Adaptive Card input element."""
        if field.field_type == "text":
            element: dict[str, Any] = {
                "type": "Input.Text",
                "id": field.id,
                "label": self._format_label(field),
                "placeholder": field.placeholder,
                "isMultiline": field.multiline,
                "isRequired": field.required,
            }
            if field.value is not None:
                element["value"] = str(field.value)
            return element

        elif field.field_type == "number":
            element = {
                "type": "Input.Number",
                "id": field.id,
                "label": self._format_label(field),
                "placeholder": field.placeholder,
                "isRequired": field.required,
            }
            if field.value is not None:
                element["value"] = field.value
            if field.min_val is not None:
                element["min"] = field.min_val
            if field.max_val is not None:
                element["max"] = field.max_val
            return element

        elif field.field_type == "date":
            element = {
                "type": "Input.Date",
                "id": field.id,
                "label": self._format_label(field),
                "isRequired": field.required,
            }
            if field.value is not None:
                element["value"] = str(field.value)
            return element

        elif field.field_type == "choice":
            choices = [
                {"title": c.get("title", ""), "value": c.get("value", "")} for c in field.choices
            ]
            element = {
                "type": "Input.ChoiceSet",
                "id": field.id,
                "label": self._format_label(field),
                "choices": choices,
                "placeholder": field.placeholder or "Select an option",
                "isRequired": field.required,
                "style": "compact",
            }
            if field.value is not None:
                element["value"] = str(field.value)
            return element

        elif field.field_type == "toggle":
            element = {
                "type": "Input.Toggle",
                "id": field.id,
                "title": field.label,
                "valueOn": "true",
                "valueOff": "false",
                "isRequired": field.required,
            }
            if field.value is not None:
                element["value"] = "true" if field.value else "false"
            return element

        else:
            # Fallback to text input
            element = {
                "type": "Input.Text",
                "id": field.id,
                "label": self._format_label(field),
                "placeholder": field.placeholder,
                "isRequired": field.required,
            }
            if field.value is not None:
                element["value"] = str(field.value)
            return element

    def _build_actions(self, form: FormDefinition) -> list[dict[str, Any]]:
        """Build the Adaptive Card action buttons.

        Uses Action.Execute (Universal Actions) with Action.Submit fallback.
        Action.Execute sends an invoke activity, allowing the bot to return
        a replacement card that Teams swaps in-place (preventing re-submission).
        """
        actions: list[dict[str, Any]] = []

        # Submit action - Action.Execute with Action.Submit fallback
        submit_fallback_data: dict[str, Any] = {"action": form.submit_action}
        submit_fallback_data.update(form.metadata)

        submit_execute_data: dict[str, Any] = dict(form.metadata)

        actions.append(
            {
                "type": "Action.Execute",
                "title": form.submit_label,
                "style": "positive",
                "verb": form.submit_action,
                "data": submit_execute_data,
                "fallback": {
                    "type": "Action.Submit",
                    "title": form.submit_label,
                    "style": "positive",
                    "data": submit_fallback_data,
                },
            }
        )

        # Cancel action (optional)
        if form.cancel_action:
            cancel_fallback_data: dict[str, Any] = {"action": form.cancel_action}
            cancel_fallback_data.update(form.metadata)

            cancel_execute_data: dict[str, Any] = dict(form.metadata)

            actions.append(
                {
                    "type": "Action.Execute",
                    "title": form.cancel_label,
                    "style": "destructive",
                    "verb": form.cancel_action,
                    "data": cancel_execute_data,
                    "fallback": {
                        "type": "Action.Submit",
                        "title": form.cancel_label,
                        "style": "destructive",
                        "data": cancel_fallback_data,
                    },
                }
            )

        return actions

    def _build_card(
        self,
        body: list[dict[str, Any]],
        actions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Build the complete Adaptive Card envelope."""
        return {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.4",
            "body": body,
            "actions": actions,
        }

    @staticmethod
    def _format_label(field: FormField) -> str:
        """Format field label with required indicator."""
        if field.required:
            return f"{field.label} *"
        return field.label


class TeamsVisualizationCardBuilder(VisualizationCardBuilder):
    """Builds Teams Adaptive Card payloads for visualization summaries."""

    def build_visualization(self, visualization: VisualizationDefinition) -> dict[str, Any]:
        """Build a Teams card with chart summary + optional interactive deep link."""
        body = self._build_body(visualization)
        actions = self._build_actions(visualization)
        card = self._build_card(body, actions)

        return {
            "text": " ",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": card,
                }
            ],
        }

    def _build_body(self, visualization: VisualizationDefinition) -> list[dict[str, Any]]:
        body: list[dict[str, Any]] = [
            {
                "type": "TextBlock",
                "text": visualization.title,
                "size": "Medium",
                "weight": "Bolder",
                "wrap": True,
            }
        ]

        if visualization.subtitle:
            body.append(
                {
                    "type": "TextBlock",
                    "text": visualization.subtitle,
                    "size": "Small",
                    "isSubtle": True,
                    "wrap": True,
                    "spacing": "None",
                }
            )

        body.append(
            {
                "type": "TextBlock",
                "text": f"Chart type: {visualization.chart_type}",
                "size": "Small",
                "isSubtle": True,
                "wrap": True,
            }
        )

        if visualization.insight_text:
            body.append(
                {
                    "type": "TextBlock",
                    "text": visualization.insight_text,
                    "wrap": True,
                    "spacing": "Medium",
                }
            )

        chart_element = self._build_chart_element(visualization.chart_type, visualization.series)
        if chart_element is not None:
            body.append(chart_element)
        else:
            body.extend(self._build_series_preview(visualization.series))
        return body

    def _build_series_preview(self, series: list[VisualizationSeries]) -> list[dict[str, Any]]:
        if not series:
            return [
                {
                    "type": "TextBlock",
                    "text": "No chart data points were provided.",
                    "isSubtle": True,
                    "wrap": True,
                }
            ]

        preview_blocks: list[dict[str, Any]] = []
        for current_series in series[:1]:
            preview_blocks.append(
                {
                    "type": "TextBlock",
                    "text": current_series.name,
                    "weight": "Bolder",
                    "wrap": True,
                    "spacing": "Medium",
                }
            )

            facts = [
                {
                    "title": str(point.label),
                    "value": str(point.value),
                }
                for point in current_series.points[:10]
            ]
            if facts:
                preview_blocks.append({"type": "FactSet", "facts": facts})
            else:
                preview_blocks.append(
                    {
                        "type": "TextBlock",
                        "text": "No points in this series.",
                        "isSubtle": True,
                        "wrap": True,
                        "spacing": "None",
                    }
                )

        return preview_blocks

    def _build_chart_element(
        self,
        chart_type: str,
        series: list[VisualizationSeries],
    ) -> dict[str, Any] | None:
        numeric_series = self._numeric_series(series)
        if not numeric_series:
            return None

        normalized_chart_type = (chart_type or "").strip().lower()
        if normalized_chart_type == "line":
            return self._build_line_chart(numeric_series)
        if normalized_chart_type == "pie":
            return self._build_pie_chart(numeric_series[0])
        if normalized_chart_type == "donut":
            return self._build_donut_chart(numeric_series[0])
        if normalized_chart_type == "funnel":
            return self._build_horizontal_bar_chart(numeric_series[0])
        if normalized_chart_type == "horizontal_bar":
            return self._build_horizontal_bar_chart(numeric_series[0])
        if normalized_chart_type == "grouped_bar":
            return self._build_grouped_vertical_bar_chart(numeric_series, stacked=False)
        if normalized_chart_type == "stacked_bar":
            return self._build_grouped_vertical_bar_chart(numeric_series, stacked=True)
        if normalized_chart_type == "stacked_horizontal_bar":
            return self._build_stacked_horizontal_bar_chart(numeric_series)
        if normalized_chart_type in {"bar", "table_combo", "timeline"}:
            return self._build_vertical_bar_chart(numeric_series[0])

        return self._build_vertical_bar_chart(numeric_series[0])

    def _numeric_series(
        self,
        series: list[VisualizationSeries],
    ) -> list[tuple[str, list[tuple[str, float]]]]:
        cleaned: list[tuple[str, list[tuple[str, float]]]] = []
        for current_series in series[:3]:
            points: list[tuple[str, float]] = []
            for point in current_series.points[:12]:
                numeric_value = self._coerce_numeric(point.value)
                if numeric_value is None:
                    continue
                points.append((str(point.label), numeric_value))
            if points:
                cleaned.append((current_series.name, points))
        return cleaned

    def _build_vertical_bar_chart(
        self, series_item: tuple[str, list[tuple[str, float]]]
    ) -> dict[str, Any]:
        _, points = series_item
        data = [
            {
                "x": label,
                "y": value,
                "dataPointTitle": f"{label}: {self._format_compact_number(value)}",
            }
            for label, value in points
        ]
        return {
            "type": "Chart.VerticalBar",
            "xAxisTitle": "Category",
            "yAxisTitle": "Value",
            "showDataLabel": True,
            "data": data,
            "fallback": self._build_fallback_factset(points),
        }

    def _build_horizontal_bar_chart(
        self, series_item: tuple[str, list[tuple[str, float]]]
    ) -> dict[str, Any]:
        _, points = series_item
        data = [
            {
                "x": label,
                "y": value,
                "dataPointTitle": f"{label}: {self._format_compact_number(value)}",
            }
            for label, value in points
        ]
        return {
            "type": "Chart.HorizontalBar",
            "xAxisTitle": "Value",
            "yAxisTitle": "Category",
            "showDataLabel": True,
            "data": data,
            "fallback": self._build_fallback_factset(points),
        }

    def _build_pie_chart(self, series_item: tuple[str, list[tuple[str, float]]]) -> dict[str, Any]:
        _, points = series_item
        data = [{"legend": label, "value": value} for label, value in points]
        return {
            "type": "Chart.Pie",
            "showDataLabel": True,
            "data": data,
            "fallback": self._build_fallback_factset(points),
        }

    def _build_donut_chart(
        self, series_item: tuple[str, list[tuple[str, float]]]
    ) -> dict[str, Any]:
        _, points = series_item
        data = [{"legend": label, "value": value} for label, value in points]
        return {
            "type": "Chart.Donut",
            "showDataLabel": True,
            "data": data,
            "fallback": self._build_fallback_factset(points),
        }

    def _build_line_chart(
        self, series: list[tuple[str, list[tuple[str, float]]]]
    ) -> dict[str, Any]:
        data = []
        for series_name, points in series:
            data.append(
                {
                    "legend": series_name,
                    "values": [{"x": label, "y": value} for label, value in points],
                }
            )

        first_series_points = series[0][1]
        return {
            "type": "Chart.Line",
            "xAxisTitle": "Category",
            "yAxisTitle": "Value",
            "showDataLabel": True,
            "data": data,
            "fallback": self._build_fallback_factset(first_series_points),
        }

    def _build_grouped_vertical_bar_chart(
        self,
        series: list[tuple[str, list[tuple[str, float]]]],
        *,
        stacked: bool,
    ) -> dict[str, Any]:
        data = []
        for series_name, points in series:
            data.append(
                {
                    "legend": series_name,
                    "values": [{"x": label, "y": value} for label, value in points],
                }
            )

        first_series_points = series[0][1]
        chart: dict[str, Any] = {
            "type": "Chart.VerticalBar.Grouped",
            "xAxisTitle": "Category",
            "yAxisTitle": "Value",
            "showBarValues": True,
            "data": data,
            "fallback": self._build_fallback_factset(first_series_points),
        }
        if stacked:
            chart["stacked"] = True
        return chart

    def _build_stacked_horizontal_bar_chart(
        self,
        series: list[tuple[str, list[tuple[str, float]]]],
    ) -> dict[str, Any]:
        data = []
        for series_name, points in series:
            data.append(
                {
                    "title": series_name,
                    "data": [{"legend": label, "value": value} for label, value in points],
                }
            )
        first_series_points = series[0][1]
        return {
            "type": "Chart.HorizontalBar.Stacked",
            "data": data,
            "fallback": self._build_fallback_factset(first_series_points),
        }

    def _build_fallback_factset(self, points: list[tuple[str, float]]) -> dict[str, Any]:
        return {
            "type": "FactSet",
            "facts": [
                {"title": label, "value": self._format_compact_number(value)}
                for label, value in points[:10]
            ],
        }

    @staticmethod
    def _format_compact_number(value: float) -> str:
        abs_val = abs(value)
        if abs_val >= 1_000_000_000:
            return f"{value / 1_000_000_000:.2f}B"
        if abs_val >= 1_000_000:
            return f"{value / 1_000_000:.1f}M"
        if abs_val >= 1_000:
            return f"{value:,.0f}"
        if float(value).is_integer():
            return f"{value:.0f}"
        return f"{value:.2f}"

    @staticmethod
    def _coerce_numeric(value: Any) -> float | None:
        """Coerce numeric-like values (including currency strings) to float."""
        if isinstance(value, bool):
            return None
        if isinstance(value, int | float):
            return float(value)
        if not isinstance(value, str):
            return None

        text = value.strip()
        if not text:
            return None

        # Handle accounting-style negatives: "(123.4M)"
        negative = text.startswith("(") and text.endswith(")")
        if negative:
            text = text[1:-1].strip()

        text = text.replace(",", "")
        number_match = re.search(r"[-+]?\d*\.?\d+", text)
        if not number_match:
            return None

        numeric = float(number_match.group(0))

        # Support compact suffixes such as K/M/B/T used in revenue displays.
        suffix_match = re.search(rf"{re.escape(number_match.group(0))}\s*([kKmMbBtT])", text)
        if suffix_match:
            multiplier_map = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}
            numeric *= multiplier_map[suffix_match.group(1).lower()]

        if negative:
            numeric = -numeric

        return numeric

    def _build_actions(self, visualization: VisualizationDefinition) -> list[dict[str, Any]]:
        if not visualization.interactive_url:
            return []

        return [
            {
                "type": "Action.OpenUrl",
                "title": visualization.open_label,
                "url": visualization.interactive_url,
            }
        ]

    def _build_card(
        self,
        body: list[dict[str, Any]],
        actions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        card: dict[str, Any] = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            # Chart elements require newer Adaptive Card schema support.
            "version": "1.5",
            "body": body,
        }
        if actions:
            card["actions"] = actions
        return card


def build_confirmation_card(message: str) -> dict[str, Any]:
    """Build a simple confirmation Adaptive Card (no actions).

    Used as a replacement card after successful form submission,
    preventing the user from re-submitting.
    """
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": "✅ Done",
                "weight": "Bolder",
                "size": "Medium",
                "color": "Good",
            },
            {
                "type": "TextBlock",
                "text": message,
                "wrap": True,
            },
        ],
    }


def build_error_card(message: str) -> dict[str, Any]:
    """Build an error Adaptive Card (no actions).

    Used as a replacement card after a failed form submission,
    preventing the user from re-submitting the same broken data.
    """
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": "❌ Error",
                "weight": "Bolder",
                "size": "Medium",
                "color": "Attention",
            },
            {
                "type": "TextBlock",
                "text": message,
                "wrap": True,
            },
        ],
    }


__all__ = [
    "TeamsFormCardBuilder",
    "TeamsVisualizationCardBuilder",
    "build_confirmation_card",
    "build_error_card",
]
