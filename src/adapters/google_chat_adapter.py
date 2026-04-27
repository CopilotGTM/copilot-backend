"""Convert bot responses into Google Chat-compatible plain text.

This adapter owns the Google Chat render boundary and intentionally flattens rich
agent outputs into text because the bot channel does not support the richer
widgets that other channels can render.
"""

from __future__ import annotations

from typing import Any

from src.adapters import AgentResponse, ChannelAdapter, ChannelContext, ChannelPayload, ResponseType
from src.adapters.whatsapp_adapter import _normalize_whatsapp_text
from src.infra.messaging.chat_stream_models import Frame

_STRUCTURED_RESULT_LIMIT = 8
# Keep the first screen payload readable on mobile and desktop chat clients.


class GoogleChatAdapter(ChannelAdapter):
    """Render normalized AgentResponse objects for the Google Chat channel.

    Channel assumptions:
    - Plain text payload only under `content["text"]`.
    - No cards / attachment widgets at adapter level.
    - Large result sets are truncated for readability before sending.
    """

    def __init__(self) -> None:
        super().__init__("google_chat")

    async def process(
        self,
        _message: str,
        _context: ChannelContext,
        frames: list[Frame],
    ) -> ChannelPayload:
        """Render response frames directly into a Google Chat payload."""
        return self.render(AgentResponse.from_frames(frames))

    def render(self, response: AgentResponse) -> ChannelPayload:
        """Dispatch by response type into channel-safe text shapes."""
        if response.type == ResponseType.QUESTION:
            return self.render_question(response.text, response.data.get("fields"), response.data)
        if response.type in (ResponseType.TABLE, ResponseType.LIST):
            return self.render_table(
                response.data.get("title"),
                response.data.get("columns"),
                response.data.get("rows"),
                response.data.get("items"),
            )
        if response.type == ResponseType.ERROR:
            return self.render_error(response.text)
        return self.render_text(response.text)

    def render_text(self, text: str) -> ChannelPayload:
        """Return a normalized text message payload."""
        rendered = _normalize_whatsapp_text(text)
        return ChannelPayload(type="message", content={"text": rendered}, text_fallback=rendered)

    def render_question(
        self,
        prompt: str,
        fields: list[dict[str, Any]] | None,
        _data: dict[str, Any],
    ) -> ChannelPayload:
        """Render questions as prompt text plus plain-text field options."""
        field_lines = [f"- {field.get('label') or field.get('name')}" for field in fields or []]
        text = prompt
        if field_lines:
            text = f"{prompt}\n\n" + "\n".join(field_lines)
        return self.render_text(text)

    def render_table(
        self,
        title: str | None,
        columns: list[str] | None,
        rows: list[dict[str, Any]] | None,
        items: list[dict[str, Any]] | None,
    ) -> ChannelPayload:
        """Render tables/lists as short, numbered records.

        A small cap keeps the initial response legible in chat threads and avoids
        overwhelming the Google Chat UI.
        """
        lines: list[str] = []
        if title:
            lines.append(title)
        if items:
            visible_items = items[:_STRUCTURED_RESULT_LIMIT]
            for index, item in enumerate(visible_items, start=1):
                lines.append(f"{index}. {_format_record(item)}")
            if len(items) > _STRUCTURED_RESULT_LIMIT:
                lines.append(f"{len(items) - _STRUCTURED_RESULT_LIMIT} more results not shown.")
        elif rows and columns:
            visible_rows = rows[:_STRUCTURED_RESULT_LIMIT]
            for index, row in enumerate(visible_rows, start=1):
                lines.append(f"{index}. {_format_record(row, columns)}")
            if len(rows) > _STRUCTURED_RESULT_LIMIT:
                lines.append(f"{len(rows) - _STRUCTURED_RESULT_LIMIT} more results not shown.")
        return self.render_text("\n".join(lines).strip() or "No results.")

    def render_error(self, message: str) -> ChannelPayload:
        """Render error text without structural formatting to preserve visibility."""
        return self.render_text(message)


def _format_record(record: Any, columns: list[str] | None = None) -> str:
    """Flatten one structured record into one compact semicolon-delimited line."""
    if not isinstance(record, dict):
        return str(record)
    keys = columns or list(record.keys())
    parts = [f"{key}: {record.get(key, '')}" for key in keys if record.get(key) not in (None, "")]
    return "; ".join(parts) if parts else "No details"
