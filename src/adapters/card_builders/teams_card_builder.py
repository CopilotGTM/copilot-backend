"""Teams Adaptive Card builder for interactive forms.

Converts channel-agnostic FormDefinition objects into Microsoft Teams
Adaptive Card JSON payloads using Action.Execute (Universal Actions).

Uses Action.Execute with Action.Submit fallback for backward compatibility
with older Teams clients. Action.Execute enables in-place card replacement
after form submission, preventing duplicate submissions.
"""

from __future__ import annotations

from typing import Any

from src.adapters.card_builders.base import (
    FormCardBuilder,
    FormDefinition,
    FormField,
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
    "build_confirmation_card",
    "build_error_card",
]
