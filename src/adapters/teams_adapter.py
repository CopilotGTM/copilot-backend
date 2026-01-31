"""Microsoft Teams adapter for rendering agent responses as Adaptive Cards.

This adapter converts AgentResponse objects into Teams-specific payloads
using Adaptive Cards for rich UI elements like forms, tables, and questions.
"""

from __future__ import annotations

from typing import Any

from src.adapters import AgentResponse, ChannelAdapter, ChannelContext, ChannelPayload, ResponseType
from src.infra.messaging.chat_stream_models import Frame


class TeamsAdapter(ChannelAdapter):
    """Adapter for Microsoft Teams using Adaptive Cards.

    Supports:
    - Text responses with Markdown
    - Questions with input fields
    - Forms with multiple inputs
    - Tables and lists
    - Suggestions as quick replies
    - Error messages
    """

    def __init__(self):
        super().__init__("teams")

    async def process(
        self,
        _message: str,
        _context: ChannelContext,
        frames: list[Frame],
    ) -> ChannelPayload:
        """Process a message and return Teams-specific payload."""
        response = AgentResponse.from_frames(frames)
        return self.render(response)

    def render(self, response: AgentResponse) -> ChannelPayload:
        """Render an AgentResponse into a Teams Adaptive Card payload."""
        if response.type == ResponseType.QUESTION:
            return self.render_question(response.text, response.data.get("fields"), response.data)
        elif response.type == ResponseType.FORM:
            return self.render_form(response.text, response.data.get("fields", []))
        elif response.type in (ResponseType.TABLE, ResponseType.LIST):
            return self.render_table(
                response.data.get("title"),
                response.data.get("columns"),
                response.data.get("rows"),
                response.data.get("items"),
            )
        elif response.type == ResponseType.ERROR:
            return self.render_error(response.text)
        elif response.type == ResponseType.CONFIRMATION:
            return self.render_confirmation(response.text)
        else:
            return self.render_text(response.text)

    def render_text(self, text: str) -> ChannelPayload:
        """Render plain text response with optional suggestions."""
        return ChannelPayload(
            type="message",
            content={"text": text},
            text_fallback=text,
        )

    def render_question(
        self,
        prompt: str,
        fields: list[dict[str, Any]] | None,
        _data: dict[str, Any],
    ) -> ChannelPayload:
        """Render a question that needs user input as an Adaptive Card."""
        body: list[dict[str, Any]] = [{"type": "TextBlock", "text": prompt, "wrap": True}]

        for field in fields or []:
            body.append(self._create_input_for_field(field))

        actions = [
            {
                "type": "Action.Submit",
                "title": "Submit",
                "style": "positive",
                "data": {"action": "answer_question"},
            }
        ]

        card = self._build_adaptive_card(body, actions)

        return ChannelPayload(
            type="message",
            content={
                "text": " ",  # Required but not displayed
                "attachments": [
                    {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
                ],
            },
            text_fallback=prompt,
        )

    def render_form(self, prompt: str, fields: list[dict[str, Any]]) -> ChannelPayload:
        """Render a multi-field form as an Adaptive Card."""
        body: list[dict[str, Any]] = [
            {"type": "TextBlock", "text": prompt, "wrap": True, "weight": "Bolder"}
        ]

        for field in fields:
            body.append(self._create_input_for_field(field))

        actions = [
            {
                "type": "Action.Submit",
                "title": "Submit",
                "style": "positive",
                "data": {"action": "form_submit"},
            },
            {
                "type": "Action.Submit",
                "title": "Cancel",
                "style": "destructive",
                "data": {"action": "form_cancel"},
            },
        ]

        card = self._build_adaptive_card(body, actions)

        return ChannelPayload(
            type="message",
            content={
                "text": " ",
                "attachments": [
                    {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
                ],
            },
            text_fallback=prompt,
        )

    def render_table(
        self,
        title: str | None,
        columns: list[str] | None,
        rows: list[dict[str, Any]] | None,
        items: list[dict[str, Any]] | None,
    ) -> ChannelPayload:
        """Render structured data as an Adaptive Card with FactSet or Table."""
        body: list[dict[str, Any]] = []

        if title:
            body.append({"type": "TextBlock", "text": title, "size": "Medium", "weight": "Bolder"})

        if items:
            facts = []
            for item in items:
                if isinstance(item, dict):
                    for key, value in item.items():
                        facts.append({"title": str(key), "value": str(value)})
            if facts:
                body.append({"type": "FactSet", "facts": facts})

        if rows and columns:
            container_items = []
            for col in columns:
                container_items.append(
                    {"type": "TextBlock", "text": col, "weight": "Bolder", "size": "Small"}
                )

            for row in rows:
                for col in columns:
                    value = row.get(col, "")
                    container_items.append(
                        {"type": "TextBlock", "text": str(value), "wrap": True, "size": "Small"}
                    )

            body.append({"type": "Container", "items": container_items})

        card = self._build_adaptive_card(body)

        return ChannelPayload(
            type="message",
            content={
                "text": " ",
                "attachments": [
                    {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
                ],
            },
            text_fallback=title or "Here are the results",
        )

    def render_confirmation(self, prompt: str) -> ChannelPayload:
        """Render a yes/no confirmation question."""
        body = [{"type": "TextBlock", "text": prompt, "wrap": True}]

        actions = [
            {
                "type": "Action.Submit",
                "title": "Yes",
                "style": "positive",
                "data": {"action": "confirm_yes"},
            },
            {
                "type": "Action.Submit",
                "title": "No",
                "style": "destructive",
                "data": {"action": "confirm_no"},
            },
        ]

        card = self._build_adaptive_card(body, actions)

        return ChannelPayload(
            type="message",
            content={
                "text": " ",
                "attachments": [
                    {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
                ],
            },
            text_fallback=prompt,
        )

    def render_error(self, message: str) -> ChannelPayload:
        """Render an error message."""
        body = [{"type": "TextBlock", "text": message, "wrap": True, "color": "Attention"}]

        card = self._build_adaptive_card(body)

        return ChannelPayload(
            type="message",
            content={
                "text": message,
                "attachments": [
                    {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
                ],
            },
            text_fallback=message,
        )

    def _create_input_for_field(self, field: dict[str, Any]) -> dict[str, Any]:
        """Create an Adaptive Card input element based on field type."""
        field_type = field.get("type", "text")
        field_id = field.get("id", field.get("name", ""))
        label = field.get("label", field.get("title", ""))

        if field_type == "text":
            return {
                "type": "Input.Text",
                "id": field_id,
                "label": label,
                "placeholder": field.get("placeholder", ""),
                "isMultiline": field.get("multiline", False),
            }
        elif field_type == "email":
            return {
                "type": "Input.Text",
                "id": field_id,
                "label": label,
                "placeholder": field.get("placeholder", ""),
                "style": "email",
            }
        elif field_type == "number":
            return {
                "type": "Input.Number",
                "id": field_id,
                "label": label,
                "placeholder": field.get("placeholder", ""),
                "min": field.get("min"),
                "max": field.get("max"),
            }
        elif field_type == "choice":
            choices = field.get("options", field.get("choices", []))
            choice_list = []
            for choice in choices:
                if isinstance(choice, dict):
                    choice_list.append(
                        {
                            "title": choice.get("label", choice.get("title", "")),
                            "value": choice.get("value", choice.get("id", "")),
                        }
                    )
                else:
                    choice_list.append({"title": str(choice), "value": str(choice)})

            return {
                "type": "Input.ChoiceSet",
                "id": field_id,
                "label": label,
                "choices": choice_list,
                "placeholder": field.get("placeholder", "Select an option"),
                "isMultiSelect": field.get("multi_select", False),
            }
        elif field_type == "date":
            return {"type": "Input.Date", "id": field_id, "label": label}
        elif field_type == "toggle":
            return {
                "type": "Input.Toggle",
                "id": field_id,
                "title": label,
                "valueOn": "true",
                "valueOff": "false",
            }
        else:
            return {
                "type": "Input.Text",
                "id": field_id,
                "label": label,
                "placeholder": field.get("placeholder", ""),
            }

    def _build_adaptive_card(
        self,
        body: list[dict[str, Any]],
        actions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Build a complete Adaptive Card payload."""
        card: dict[str, Any] = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.5",
            "body": body,
        }

        if actions:
            card["actions"] = actions

        return card


__all__ = ["TeamsAdapter"]
