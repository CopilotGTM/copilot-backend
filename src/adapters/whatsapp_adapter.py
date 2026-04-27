"""Convert bot responses into WhatsApp-compatible plain text.

This adapter owns the WhatsApp render boundary for bot replies.
It removes markdown constructs that WhatsApp either strips partially
or renders inconsistently, then flattens structured rows into readable,
numbered bullet-like lines.
"""

from __future__ import annotations

import re
from typing import Any

from src.adapters import AgentResponse, ChannelAdapter, ChannelContext, ChannelPayload, ResponseType
from src.infra.messaging.chat_stream_models import Frame

_STRUCTURED_RESULT_LIMIT = 8
# Limit structured payload size to avoid overlong WhatsApp mobile messages.
_MARKDOWN_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


class WhatsAppAdapter(ChannelAdapter):
    """Render normalized AgentResponse objects for the WhatsApp channel.

    Channel contracts:
    - No custom cards/buttons in transport payload.
    - Plain text only in `content["text"]`.
    - Structured tables/lists should be compacted before transmission.
    """

    def __init__(self) -> None:
        super().__init__("whatsapp")

    async def process(
        self,
        _message: str,
        _context: ChannelContext,
        frames: list[Frame],
    ) -> ChannelPayload:
        """Render response frames directly into a WhatsApp payload."""
        return self.render(AgentResponse.from_frames(frames))

    def render(self, response: AgentResponse) -> ChannelPayload:
        """Dispatch rendering by response type.

        WhatsApp does not support separate rendering widgets, so each response
        type maps to the closest text-only representation.
        """
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
        """Convert raw response text into WhatsApp-safe text payload."""
        rendered = _normalize_whatsapp_text(text)
        return ChannelPayload(type="message", content={"text": rendered}, text_fallback=rendered)

    def render_question(
        self,
        prompt: str,
        fields: list[dict[str, Any]] | None,
        _data: dict[str, Any],
    ) -> ChannelPayload:
        """Render a question plus selectable fields as plain lines."""
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
        """Render tables/lists as numbered short rows.

        A small fixed limit keeps the first page usable on small WhatsApp
        screens; callers can ask follow-ups for more rows if needed.
        """
        lines: list[str] = []
        if title:
            lines.append(title)
        if items:
            visible_items = items[:_STRUCTURED_RESULT_LIMIT]
            for index, item in enumerate(visible_items, start=1):
                if isinstance(item, dict):
                    lines.append(f"{index}. {_format_record(item)}")
                else:
                    lines.append(f"{index}. {item}")
            if len(items) > _STRUCTURED_RESULT_LIMIT:
                lines.append(f"{len(items) - _STRUCTURED_RESULT_LIMIT} more results not shown.")
        elif rows and columns:
            visible_rows = rows[:_STRUCTURED_RESULT_LIMIT]
            for index, row in enumerate(visible_rows, start=1):
                lines.append(f"{index}. {_format_record(row, columns)}")
            if len(rows) > _STRUCTURED_RESULT_LIMIT:
                lines.append(f"{len(rows) - _STRUCTURED_RESULT_LIMIT} more results not shown.")
        return self.render_text("\n".join(lines).strip() or "No results.")

    def render_confirmation(self, prompt: str) -> ChannelPayload:
        """Render confirmation prompts with a simple yes/no call-to-action."""
        return self.render_text(f"{prompt}\n\nReply yes or no.")

    def render_error(self, message: str) -> ChannelPayload:
        """Render errors as plain text for immediate user visibility."""
        return self.render_text(message)


def _normalize_whatsapp_text(text: str) -> str:
    text = _convert_markdown_tables(text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _MARKDOWN_LINK_RE.sub(r"\1 (\2)", text)
    text = re.sub(r"^```[^\n]*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"(?<!\*)\*\*([^*\n]+)\*\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!_)__([^_\n]+)__(?!_)", r"\1", text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)

    lines = [line.rstrip() for line in text.split("\n")]
    compact_lines: list[str] = []
    previous_blank = False
    for line in lines:
        stripped = line.strip()
        if _MARKDOWN_TABLE_SEPARATOR_RE.match(stripped):
            continue
        if not stripped:
            if not previous_blank and compact_lines:
                compact_lines.append("")
            previous_blank = True
            continue
        compact_lines.append(stripped)
        previous_blank = False

    return "\n".join(compact_lines).strip()


def _convert_markdown_tables(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    output: list[str] = []
    index = 0

    while index < len(lines):
        if (
            index + 1 < len(lines)
            and _is_table_row(lines[index])
            and _MARKDOWN_TABLE_SEPARATOR_RE.match(lines[index + 1].strip())
        ):
            headers = _split_table_row(lines[index])
            index += 2
            table_rows: list[list[str]] = []
            while index < len(lines) and _is_table_row(lines[index]):
                table_rows.append(_split_table_row(lines[index]))
                index += 1

            for row_number, row in enumerate(table_rows, start=1):
                row_record = {
                    header: row[column_index] if column_index < len(row) else ""
                    for column_index, header in enumerate(headers)
                    if header
                }
                output.append(f"{row_number}. {_format_record(row_record)}")
            continue

        output.append(lines[index])
        index += 1

    return "\n".join(output)


def _is_table_row(line: str) -> bool:
    stripped = line.strip()
    return "|" in stripped and stripped.count("|") >= 1


def _split_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _format_record(record: dict[str, Any], columns: list[str] | None = None) -> str:
    keys = columns or list(record.keys())
    parts = [f"{key}: {record.get(key, '')}" for key in keys if record.get(key) not in (None, "")]
    return "; ".join(parts) if parts else "No details"
