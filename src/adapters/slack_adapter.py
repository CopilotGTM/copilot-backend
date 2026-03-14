"""Slack adapter for rendering agent responses as Block Kit.

This adapter converts AgentResponse objects into Slack-specific payloads
using Block Kit for rich UI elements like forms, tables, and questions.

Block Kit reference: https://api.slack.com/block-kit
"""

from __future__ import annotations

import re
from typing import Any

from src.adapters import AgentResponse, ChannelAdapter, ChannelContext, ChannelPayload, ResponseType
from src.infra.messaging.chat_stream_models import Frame

# Slack section block text limit is 3000 chars
_SECTION_TEXT_LIMIT = 3000

# Body cells wider than this trigger card-style layout instead of code block
_MAX_NARROW_CELL_WIDTH = 60

# Safe per-block limit for Slack markdown blocks (cumulative payload limit is 12000)
_MARKDOWN_BLOCK_LIMIT = 4000

# Slack table block limits
_TABLE_MAX_ROWS = 100
_TABLE_MAX_COLS = 20


def _strip_md_formatting(text: str) -> str:
    """Strip Markdown bold/italic/strike markers for code block display."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    return text


def _convert_markdown_table(table_text: str) -> str:
    """Convert a Markdown table into Slack-friendly format.

    Narrow tables (all body cells <= 60 chars) -> monospace code block.
    Wide tables (any body cell > 60 chars) -> card-style layout that
    preserves mrkdwn formatting for readability in Slack.
    """
    lines = table_text.strip().split("\n")
    data_lines: list[list[str]] = []

    for line in lines:
        stripped = line.strip()
        # Skip separator rows like |---|---|---| or |:---|---:|
        if re.match(r"^\|[\s\-:|]+\|$", stripped):
            continue
        # Only process lines with pipe delimiters
        if "|" not in stripped:
            continue
        # Strip outer pipes and split on inner pipes
        inner = stripped
        if inner.startswith("|"):
            inner = inner[1:]
        if inner.endswith("|"):
            inner = inner[:-1]
        cells = [c.strip() for c in inner.split("|")]
        if cells:
            data_lines.append(cells)

    if len(data_lines) < 2:
        return table_text

    headers = data_lines[0]
    body = data_lines[1:]

    # Wide table check: any body cell exceeds the narrow threshold
    max_cell_len = max(
        (len(cell) for row in body for cell in row),
        default=0,
    )

    if max_cell_len > _MAX_NARROW_CELL_WIDTH:
        return _convert_wide_table_to_cards(headers, body)

    return _convert_narrow_table_to_codeblock(data_lines)


def _convert_narrow_table_to_codeblock(data_lines: list[list[str]]) -> str:
    """Narrow table -> aligned monospace code block.

    Strips Markdown formatting (bold, italic) since code blocks
    render text literally.
    """
    col_count = max(len(row) for row in data_lines)
    col_widths = [0] * col_count

    plain_lines: list[list[str]] = []
    for row in data_lines:
        plain_row = [_strip_md_formatting(row[i]) if i < len(row) else "" for i in range(col_count)]
        plain_lines.append(plain_row)
        for i, cell in enumerate(plain_row):
            col_widths[i] = max(col_widths[i], len(cell))

    formatted: list[str] = []
    for idx, row in enumerate(plain_lines):
        padded = [row[i].ljust(col_widths[i]) for i in range(col_count)]
        formatted.append("  ".join(padded))
        # Add a separator after the header row
        if idx == 0:
            formatted.append("  ".join("─" * w for w in col_widths))

    return "```\n" + "\n".join(formatted) + "\n```"


def _convert_wide_table_to_cards(headers: list[str], body: list[list[str]]) -> str:
    """Wide table -> card-style layout for readability.

    Short cells (<= 60 chars) are joined on one line as a heading.
    Long cells follow on the next line as body text.

    Example output for a MEDPIC scorecard::

        *M - Metrics*  ·  *Moderate*
        Inbound close rate is ~60%...

        *E - Economic Buyer*  ·  *Weak / Unknown*
        Exec "Venky" is referenced...
    """
    parts: list[str] = []

    # Identify which columns are "long" vs "short" across all rows
    col_count = max((len(r) for r in body), default=0)
    long_col_indices: set[int] = set()
    for row in body:
        for i, cell in enumerate(row):
            if len(cell) > _MAX_NARROW_CELL_WIDTH:
                long_col_indices.add(i)

    # Add column header context for short columns
    short_headers = [
        headers[i] for i in range(min(len(headers), col_count)) if i not in long_col_indices
    ]
    if short_headers and len(short_headers) >= 2:
        parts.append("  ·  ".join(f"_{h}_" for h in short_headers))

    for row in body:
        short_cells: list[tuple[int, str]] = []
        long_cells: list[str] = []

        for i in range(min(len(row), col_count)):
            cell = row[i]
            if not cell.strip():
                continue
            if i in long_col_indices:
                long_cells.append(cell)
            else:
                short_cells.append((i, cell))

        row_lines: list[str] = []
        if short_cells:
            short_parts: list[str] = []
            for pos, (idx, cell) in enumerate(short_cells):
                header = headers[idx] if idx < len(headers) else ""
                if pos == 0:
                    short_parts.append(f"*{cell}*")
                elif header:
                    short_parts.append(f"_{header}:_ {cell}")
                else:
                    short_parts.append(cell)
            row_lines.append("  ·  ".join(short_parts))
        for long_text in long_cells:
            row_lines.append(long_text)

        if row_lines:
            parts.append("\n".join(row_lines))

    return "\n\n".join(parts)


# Regex to detect a Markdown table: 2+ consecutive lines starting/ending with |
_MD_TABLE_RE = re.compile(
    r"(?:^[ \t]*\|.+\|[ \t]*$\n?){2,}",
    re.MULTILINE,
)


def markdown_to_mrkdwn(text: str) -> str:
    """Convert standard Markdown to Slack mrkdwn syntax.

    The LLM agent produces standard Markdown. Slack uses its own "mrkdwn"
    format which differs in bold, italic, strikethrough, links, and headings.

    Handles:
        **bold** / __bold__  →  *bold*
        *italic* / _italic_  →  _italic_  (careful not to clash with bold)
        ~~strike~~           →  ~strike~
        [text](url)          →  <url|text>
        ![alt](url)          →  <url|alt> (images as links)
        ### Heading          →  *Heading*  (bold line)
        - item / * item      →  •  item
        | tables |           →  code block (narrow) or card layout (wide)
    """
    if not text:
        return text

    # Normalize line endings (LLM output may mix \r\n and \n)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Protect code blocks from conversion
    code_blocks: list[str] = []

    def _save_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(0))
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    # Save fenced code blocks
    text = re.sub(r"```[\s\S]*?```", _save_code_block, text)
    # Save inline code
    text = re.sub(r"`[^`]+`", _save_code_block, text)

    # Convert Markdown tables (before other conversions so bold/link
    # transforms don't mangle table cell content).
    # Wide tables -> card layout (preserves formatting for later conversion).
    # Narrow tables -> code blocks (protected below).
    text = _MD_TABLE_RE.sub(lambda m: _convert_markdown_table(m.group(0)), text)

    # Re-save any NEW code blocks created by narrow table conversion
    text = re.sub(r"```[\s\S]*?```", _save_code_block, text)

    # Images: ![alt](url) → <url|alt>
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"<\2|\1>", text)

    # Links: [text](url) → <url|text>
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)

    # Bold: **text** or __text__ → *text*
    # Must run before italic to avoid conflicts
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"__(.+?)__", r"*\1*", text)

    # Strikethrough: ~~text~~ → ~text~
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)

    # Headings: ### Heading → *Heading* (as bold text)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)

    # Unordered lists: - item or * item → •  item
    # Only match lines starting with - or * followed by a space (not bold markers)
    text = re.sub(r"^[\-\*]\s+", "•  ", text, flags=re.MULTILINE)

    # Horizontal rules: --- or *** or ___ → ───────
    text = re.sub(r"^[\-\*_]{3,}\s*$", "───────────────────", text, flags=re.MULTILINE)

    # Restore code blocks
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CODEBLOCK{i}\x00", block)

    return text


class SlackAdapter(ChannelAdapter):
    """Adapter for Slack using Block Kit.

    Supports:
    - Text responses with Markdown (mrkdwn)
    - Questions with input fields
    - Forms with multiple inputs
    - Tables and lists
    - Suggestions as buttons
    - Error messages
    """

    def __init__(self):
        super().__init__("slack")

    async def process(
        self,
        _message: str,
        _context: ChannelContext,
        frames: list[Frame],
    ) -> ChannelPayload:
        """Process a message and return Slack-specific payload."""
        response = AgentResponse.from_frames(frames)
        return self.render(response)

    def render(self, response: AgentResponse) -> ChannelPayload:
        """Render an AgentResponse into a Slack Block Kit payload.

        Text responses use Slack's native markdown block (no conversion needed).
        Interactive responses (questions, forms, confirmations) still use mrkdwn
        since input/action blocks require it.
        """
        if response.type == ResponseType.QUESTION:
            text = markdown_to_mrkdwn(response.text) if response.text else response.text
            return self.render_question(text, response.data.get("fields"), response.data)
        elif response.type == ResponseType.FORM:
            text = markdown_to_mrkdwn(response.text) if response.text else response.text
            return self.render_form(text, response.data.get("fields", []))
        elif response.type in (ResponseType.TABLE, ResponseType.LIST):
            return self.render_table(
                response.data.get("title"),
                response.data.get("columns"),
                response.data.get("rows"),
                response.data.get("items"),
            )
        elif response.type == ResponseType.ERROR:
            text = markdown_to_mrkdwn(response.text) if response.text else response.text
            return self.render_error(text)
        elif response.type == ResponseType.CONFIRMATION:
            text = markdown_to_mrkdwn(response.text) if response.text else response.text
            return self.render_confirmation(text)
        else:
            # TEXT responses use native markdown blocks — no mrkdwn conversion
            return self.render_text(response.text)

    def render_text(self, text: str) -> ChannelPayload:
        """Render plain text response using Slack's native markdown blocks.

        Uses the 'markdown' block type which renders standard markdown natively.
        The text fallback (for notifications) is still mrkdwn-converted.
        source_text stores the original markdown for fallback re-rendering.
        """
        blocks = self._text_to_markdown_blocks(text)
        fallback = markdown_to_mrkdwn(text)

        return ChannelPayload(
            type="message",
            content={"blocks": blocks, "text": fallback},
            text_fallback=fallback,
            source_text=text,
        )

    def render_text_fallback(self, text: str) -> ChannelPayload:
        """Render text using legacy mrkdwn section blocks.

        Used as fallback when the workspace doesn't support the 'markdown' block type.
        Expects the original markdown text (not already-converted mrkdwn).
        """
        converted = markdown_to_mrkdwn(text)
        blocks = self._text_to_section_blocks(converted)

        return ChannelPayload(
            type="message",
            content={"blocks": blocks, "text": converted},
            text_fallback=converted,
        )

    @staticmethod
    def _is_scorecard_column_header(paragraph: str) -> bool:
        """Return whether a paragraph is just the scorecard column header row."""
        normalized = re.sub(r"[_*\s]+", "", paragraph).lower()
        return normalized in {
            "dimension·rating·confidence",
            "dimension.rating.confidence",
            "dimension-rating-confidence",
            "component·score·confidence",
            "component.score.confidence",
            "component-score-confidence",
        }

    @staticmethod
    def _scorecard_value(text: str, label: str | None = None) -> str:
        """Normalize scorecard values extracted from converted mrkdwn."""
        value = text.strip().strip('"').strip("'").strip("“”")
        value = re.sub(r"^[*_`]+|[*_`]+$", "", value).strip()
        if label:
            value = re.sub(
                rf"^[*_`\s]*{re.escape(label)}[*_`\s]*:\s*",
                "",
                value,
                flags=re.IGNORECASE,
            ).strip()
        return re.sub(r"^[*_`]+|[*_`]+$", "", value).strip()

    @staticmethod
    def _scorecard_labeled_value(text: str) -> tuple[str, str] | None:
        """Extract a label/value pair from converted scorecard metadata."""
        normalized = text.strip().strip('"').strip("'").strip("“”")
        match = re.match(
            r"^[*_`\s]*(?P<label>[A-Za-z ]+?)[*_`\s]*:\s*(?P<value>.+)$",
            normalized,
            flags=re.IGNORECASE,
        )
        if not match:
            return None

        label = match.group("label").strip()
        value = SlackAdapter._scorecard_value(match.group("value"))
        if not label or not value:
            return None
        return label, value

    @staticmethod
    def _scorecard_meta_from_line(line: str) -> tuple[str, str, str, str, str] | None:
        """Parse a scorecard metadata row from a single converted line."""
        normalized = line.strip().strip('"').strip("'").strip("“”")
        if not normalized or SlackAdapter._is_scorecard_column_header(normalized):
            return None

        raw_parts = [
            part.strip() for part in re.split(r"\s*(?:·|•|\|)\s*", normalized) if part.strip()
        ]
        if len(raw_parts) < 3:
            return None

        component = SlackAdapter._scorecard_value(raw_parts[0])
        middle = SlackAdapter._scorecard_labeled_value(raw_parts[1])
        trailing = SlackAdapter._scorecard_labeled_value(" · ".join(raw_parts[2:]))

        if not component or not middle or not trailing:
            return None

        middle_label, middle_value = middle
        trailing_label, trailing_value = trailing

        return component, middle_label, middle_value, trailing_label, trailing_value

    @staticmethod
    def _scorecard_blocks_from_paragraph(paragraph: str) -> list[dict[str, Any]] | None:
        """Convert scorecard text into cleaner Slack section blocks."""
        lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
        if not lines:
            return None

        start_idx = 0
        while start_idx < len(lines) and SlackAdapter._is_scorecard_column_header(lines[start_idx]):
            start_idx += 1
        if start_idx >= len(lines):
            return None

        if not SlackAdapter._scorecard_meta_from_line(lines[start_idx]):
            return None

        blocks: list[dict[str, Any]] = []
        index = start_idx
        while index < len(lines):
            if SlackAdapter._is_scorecard_column_header(lines[index]):
                index += 1
                continue

            meta = SlackAdapter._scorecard_meta_from_line(lines[index])
            if not meta:
                return None

            component, middle_label, middle_value, trailing_label, trailing_value = meta
            body_lines: list[str] = []
            index += 1
            while index < len(lines):
                next_line = lines[index]
                if SlackAdapter._is_scorecard_column_header(next_line):
                    break
                if SlackAdapter._scorecard_meta_from_line(next_line):
                    break
                body_lines.append(next_line)
                index += 1

            section_lines = [
                f"*{component}*",
                f"_{middle_label}:_ {middle_value}  ·  _{trailing_label}:_ {trailing_value}",
            ]
            if body_lines:
                section_lines.append("\n".join(body_lines).strip())

            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "\n".join(section_lines)},
                }
            )

        return blocks

    @staticmethod
    def _is_heading_paragraph(paragraph: str) -> bool:
        """Return whether a paragraph should be promoted to a Slack header block."""
        stripped = paragraph.strip()
        if not stripped or "\n" in stripped:
            return False
        if len(stripped) > 120:
            return False
        if stripped.startswith(("•", "-", ">", "```", "`")):
            return False
        return not stripped.endswith((".", "!", "?", ":", ";"))

    @staticmethod
    def _heading_plain_text(paragraph: str) -> str:
        """Convert a short mrkdwn heading into plain text for Slack header blocks."""
        text = paragraph.strip()
        text = re.sub(r"^\*(.+)\*$", r"\1", text)
        text = re.sub(r"^_(.+)_$", r"\1", text)
        return text[:150]

    @staticmethod
    def _text_to_section_blocks(text: str) -> list[dict[str, Any]]:
        """Split text into section blocks respecting the 3000-char limit.

        Splits on paragraph boundaries (double newline) to keep blocks
        semantically coherent. Short heading-like paragraphs are promoted to
        header blocks so long answers scan better in Slack.
        """
        blocks: list[dict[str, Any]] = []
        paragraphs = text.split("\n\n")

        def _append_block(block_text: str) -> None:
            if block_text.strip():
                blocks.append(
                    {"type": "section", "text": {"type": "mrkdwn", "text": block_text.strip()}}
                )

        for para in paragraphs:
            stripped = para.strip()
            if not stripped:
                continue

            if SlackAdapter._is_scorecard_column_header(stripped):
                continue

            scorecard_blocks = SlackAdapter._scorecard_blocks_from_paragraph(stripped)
            if scorecard_blocks:
                blocks.extend(scorecard_blocks)
                continue

            if SlackAdapter._is_heading_paragraph(stripped):
                blocks.append(
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": SlackAdapter._heading_plain_text(stripped),
                        },
                    }
                )
                continue

            # If a single paragraph exceeds Slack's hard limit, split it deterministically.
            if len(stripped) > _SECTION_TEXT_LIMIT:
                start = 0
                while start < len(stripped):
                    _append_block(stripped[start : start + _SECTION_TEXT_LIMIT])
                    start += _SECTION_TEXT_LIMIT
                continue

            _append_block(stripped)

        return blocks or [
            {"type": "section", "text": {"type": "mrkdwn", "text": text[:_SECTION_TEXT_LIMIT]}}
        ]

    @staticmethod
    def _text_to_markdown_blocks(text: str) -> list[dict[str, Any]]:
        """Split text into Slack markdown blocks preserving paragraph boundaries.

        Uses Slack's native 'markdown' block type which renders standard
        markdown (bold, italic, tables, code, lists) without manual conversion.
        Groups short paragraphs into a single block to reduce block count.
        """
        blocks: list[dict[str, Any]] = []
        paragraphs = text.split("\n\n")

        current_chunk: list[str] = []
        current_len = 0

        def flush() -> None:
            nonlocal current_chunk, current_len
            if current_chunk:
                block_text = "\n\n".join(current_chunk)
                blocks.append({"type": "markdown", "text": block_text})
                current_chunk = []
                current_len = 0

        for para in paragraphs:
            stripped = para.strip()
            if not stripped:
                continue

            para_len = len(stripped)

            # Single paragraph exceeds limit — flush current, then chunk it
            if para_len > _MARKDOWN_BLOCK_LIMIT:
                flush()
                start = 0
                while start < para_len:
                    blocks.append(
                        {
                            "type": "markdown",
                            "text": stripped[start : start + _MARKDOWN_BLOCK_LIMIT],
                        }
                    )
                    start += _MARKDOWN_BLOCK_LIMIT
                continue

            # Adding this paragraph would exceed the limit — flush first
            if current_len + para_len + 2 > _MARKDOWN_BLOCK_LIMIT and current_chunk:
                flush()

            current_chunk.append(stripped)
            current_len += para_len + 2  # +2 for the \n\n separator

        flush()

        return blocks or [{"type": "markdown", "text": text[:_MARKDOWN_BLOCK_LIMIT]}]

    def render_question(
        self,
        prompt: str,
        fields: list[dict[str, Any]] | None,
        _data: dict[str, Any],
    ) -> ChannelPayload:
        """Render a question that needs user input."""
        blocks: list[dict[str, Any]] = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": prompt},
            }
        ]

        for field_def in fields or []:
            block = self._create_input_block(field_def)
            if block:
                blocks.append(block)

        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Submit"},
                        "style": "primary",
                        "action_id": "answer_question",
                    }
                ],
            }
        )

        return ChannelPayload(
            type="message",
            content={"blocks": blocks, "text": prompt},
            text_fallback=prompt,
        )

    def render_form(self, prompt: str, fields: list[dict[str, Any]]) -> ChannelPayload:
        """Render a multi-field form."""
        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": prompt[:150]},
            }
        ]

        for field_def in fields:
            block = self._create_input_block(field_def)
            if block:
                blocks.append(block)

        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Submit"},
                        "style": "primary",
                        "action_id": "form_submit",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Cancel"},
                        "style": "danger",
                        "action_id": "form_cancel",
                    },
                ],
            }
        )

        return ChannelPayload(
            type="message",
            content={"blocks": blocks, "text": prompt},
            text_fallback=prompt,
        )

    def render_table(
        self,
        title: str | None,
        columns: list[str] | None,
        rows: list[dict[str, Any]] | None,
        items: list[dict[str, Any]] | None,
    ) -> ChannelPayload:
        """Render structured data using Slack's native table block.

        Tabular data (columns + rows) uses the Slack table block in attachments.
        Key-value items still use section fields in blocks.
        """
        blocks: list[dict[str, Any]] = []
        attachments: list[dict[str, Any]] | None = None

        if title:
            blocks.append(
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": title[:150]},
                }
            )

        # Key-value items as section fields (unchanged)
        if items:
            fields = []
            for item in items:
                if isinstance(item, dict):
                    for key, value in item.items():
                        fields.append({"type": "mrkdwn", "text": f"*{key}*\n{value}"})
            for i in range(0, len(fields), 10):
                blocks.append({"type": "section", "fields": fields[i : i + 10]})

        # Tabular data as Slack table block
        if rows and columns:
            cols = columns[:_TABLE_MAX_COLS]

            header_row = {"cells": [{"type": "raw_text", "text": col} for col in cols]}

            # Data rows (Slack max 100 total rows including header)
            max_data_rows = _TABLE_MAX_ROWS - 1
            data_rows = []
            for row in rows[:max_data_rows]:
                data_rows.append(
                    {"cells": [{"type": "raw_text", "text": str(row.get(col, ""))} for col in cols]}
                )

            table_block = {
                "type": "table",
                "rows": [header_row] + data_rows,
                "column_settings": [{"align": "left"} for _ in cols],
            }

            attachments = [{"blocks": [table_block]}]

        fallback = title or "Here are the results"

        return ChannelPayload(
            type="message",
            content={"blocks": blocks, "text": fallback},
            text_fallback=fallback,
            attachments=attachments,
        )

    def render_confirmation(self, prompt: str) -> ChannelPayload:
        """Render a yes/no confirmation."""
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": prompt},
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Yes"},
                        "style": "primary",
                        "action_id": "confirm_yes",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "No"},
                        "style": "danger",
                        "action_id": "confirm_no",
                    },
                ],
            },
        ]

        return ChannelPayload(
            type="message",
            content={"blocks": blocks, "text": prompt},
            text_fallback=prompt,
        )

    def render_error(self, message: str) -> ChannelPayload:
        """Render an error message."""
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":warning: {message}",
                },
            }
        ]

        return ChannelPayload(
            type="message",
            content={"blocks": blocks, "text": message},
            text_fallback=message,
        )

    def _create_input_block(self, field_def: dict[str, Any]) -> dict[str, Any] | None:
        """Create a Block Kit input element based on field type."""
        field_type = field_def.get("type", "text")
        field_id = field_def.get("id", field_def.get("name", ""))
        label = field_def.get("label", field_def.get("title", ""))

        if field_type in ("text", "email", "number"):
            return {
                "type": "input",
                "block_id": f"input_{field_id}",
                "label": {"type": "plain_text", "text": label},
                "element": {
                    "type": "plain_text_input",
                    "action_id": field_id,
                    "placeholder": {
                        "type": "plain_text",
                        "text": field_def.get("placeholder", f"Enter {label}"),
                    },
                    "multiline": field_def.get("multiline", False),
                },
            }
        elif field_type == "choice":
            choices = field_def.get("options", field_def.get("choices", []))
            options = []
            for choice in choices:
                if isinstance(choice, dict):
                    options.append(
                        {
                            "text": {
                                "type": "plain_text",
                                "text": choice.get("label", choice.get("title", "")),
                            },
                            "value": str(choice.get("value", choice.get("id", ""))),
                        }
                    )
                else:
                    options.append(
                        {
                            "text": {"type": "plain_text", "text": str(choice)},
                            "value": str(choice),
                        }
                    )

            if not options:
                return None

            return {
                "type": "input",
                "block_id": f"input_{field_id}",
                "label": {"type": "plain_text", "text": label},
                "element": {
                    "type": "static_select",
                    "action_id": field_id,
                    "placeholder": {
                        "type": "plain_text",
                        "text": field_def.get("placeholder", "Select an option"),
                    },
                    "options": options,
                },
            }
        elif field_type == "date":
            return {
                "type": "input",
                "block_id": f"input_{field_id}",
                "label": {"type": "plain_text", "text": label},
                "element": {
                    "type": "datepicker",
                    "action_id": field_id,
                },
            }
        elif field_type == "toggle":
            return {
                "type": "input",
                "block_id": f"input_{field_id}",
                "label": {"type": "plain_text", "text": label},
                "element": {
                    "type": "checkboxes",
                    "action_id": field_id,
                    "options": [
                        {
                            "text": {"type": "plain_text", "text": label},
                            "value": "true",
                        }
                    ],
                },
            }
        else:
            return {
                "type": "input",
                "block_id": f"input_{field_id}",
                "label": {"type": "plain_text", "text": label},
                "element": {
                    "type": "plain_text_input",
                    "action_id": field_id,
                    "placeholder": {
                        "type": "plain_text",
                        "text": field_def.get("placeholder", ""),
                    },
                },
            }


__all__ = ["SlackAdapter"]
