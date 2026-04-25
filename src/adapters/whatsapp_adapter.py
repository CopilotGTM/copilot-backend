"""WhatsApp adapter for plain-text bot responses."""

from __future__ import annotations

from typing import Any

from src.adapters import AgentResponse, ChannelAdapter, ChannelContext, ChannelPayload, ResponseType
from src.infra.messaging.chat_stream_models import Frame


class WhatsAppAdapter(ChannelAdapter):
    """Render bot responses into WhatsApp-friendly plain text."""

    def __init__(self) -> None:
        super().__init__("whatsapp")

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
        if response.type == ResponseType.CONFIRMATION:
            return self.render_confirmation(response.text)
        return self.render_text(response.text)

    def render_text(self, text: str) -> ChannelPayload:
        return ChannelPayload(type="message", content={"text": text}, text_fallback=text)

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
            for item in items:
                if isinstance(item, dict):
                    lines.extend(f"{key}: {value}" for key, value in item.items())
        elif rows and columns:
            for row in rows:
                lines.append(" | ".join(f"{col}: {row.get(col, '')}" for col in columns))
        return self.render_text("\n".join(lines).strip() or "No results.")

    def render_confirmation(self, prompt: str) -> ChannelPayload:
        return self.render_text(f"{prompt}\n\nReply yes or no.")

    def render_error(self, message: str) -> ChannelPayload:
        return self.render_text(message)
