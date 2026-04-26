"""Google Chat adapter for plain-text bot responses."""

from __future__ import annotations

from typing import Any

from src.adapters import AgentResponse, ChannelAdapter, ChannelContext, ChannelPayload, ResponseType
from src.adapters.whatsapp_adapter import _normalize_whatsapp_text
from src.infra.messaging.chat_stream_models import Frame

_STRUCTURED_RESULT_LIMIT = 8


class GoogleChatAdapter(ChannelAdapter):
    """Render agent responses into conservative Google Chat message payloads."""

    def __init__(self) -> None:
        super().__init__("google_chat")

    async def process(
        self,
        _message: str,
        _context: ChannelContext,
        frames: list[Frame],
    ) -> ChannelPayload:
        return self.render(AgentResponse.from_frames(frames))

    def render(self, response: AgentResponse) -> ChannelPayload:
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
        rendered = _normalize_whatsapp_text(text)
        return ChannelPayload(type="message", content={"text": rendered}, text_fallback=rendered)

    def render_question(
        self,
        prompt: str,
        fields: list[dict[str, Any]] | None,
        _data: dict[str, Any],
    ) -> ChannelPayload:
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
        return self.render_text(message)


def _format_record(record: Any, columns: list[str] | None = None) -> str:
    if not isinstance(record, dict):
        return str(record)
    keys = columns or list(record.keys())
    parts = [f"{key}: {record.get(key, '')}" for key in keys if record.get(key) not in (None, "")]
    return "; ".join(parts) if parts else "No details"
