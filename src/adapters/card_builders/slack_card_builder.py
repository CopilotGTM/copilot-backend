"""Slack Block Kit card builders.

Renders channel-agnostic definitions into Slack Block Kit payloads.
"""

from __future__ import annotations

import json
from typing import Any

from src.adapters.card_builders.base import (
    FormCardBuilder,
    FormDefinition,
)


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


__all__ = ["SlackFormCardBuilder"]
