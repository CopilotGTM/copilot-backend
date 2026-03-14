# Slack Markdown Block Rendering Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the manual `markdown_to_mrkdwn` conversion with Slack's native `markdown` block type for LLM text responses, and use Slack's `table` block for structured tabular data — so LLM output renders cleanly in Slack without lossy format translation.

**Architecture:** Two-pronged approach: (1) For general LLM text responses (which contain standard markdown), use Slack's `markdown` block type that natively handles standard markdown. (2) For structured table data (from `render_table`), convert to Slack's native `table` block via `attachments`. The existing `mrkdwn`-based rendering stays as fallback for interactive blocks (questions, forms, confirmations) that don't benefit from the markdown block. Fallback detection uses a dedicated `_send_block_kit_with_fallback()` method to avoid changing the return type of existing public methods.

**Tech Stack:** Python, Slack Block Kit API (`markdown` block, `table` block), existing `SlackAdapter` + `SlackBotService`

---

## Current State

### What exists today
- `src/adapters/slack_adapter.py` — converts LLM markdown to Slack's `mrkdwn` format via regex-based `markdown_to_mrkdwn()` function
- All text blocks use `{"type": "section", "text": {"type": "mrkdwn", "text": ...}}`
- Tables in LLM text are converted to either ASCII code blocks (narrow) or card-style layout (wide)
- `render_table()` for structured data uses pipe-separated `mrkdwn` text in section blocks
- `SlackBotService` sends via `chat.postMessage` with `blocks` payload
- `BotMessageProcessingService._send_slack_payload()` sends blocks or updates thinking message

### What changes
- `render_text()` will use `{"type": "markdown", "text": ...}` blocks instead of `{"type": "section", "text": {"type": "mrkdwn"}}` — Slack handles markdown-to-rendered translation natively
- `render_table()` will produce a Slack `table` block in `attachments` for structured data
- `markdown_to_mrkdwn()` stays for interactive blocks (forms, questions, confirmations, errors) where `mrkdwn` type is required
- `_send_slack_payload()` gains the ability to send `attachments` alongside `blocks`, and auto-fallback on `invalid_blocks` error
- `ChannelPayload` gets two new fields: `attachments` (for Slack table blocks) and `source_text` (original markdown for fallback re-rendering)

### Key constraints
- Slack `markdown` block has a **12,000 char cumulative limit** per payload
- Slack `table` block: max **100 rows**, **20 columns**, **1 table per message** (must go in `attachments`)
- `markdown` block type may not be available on older Slack workspaces — need graceful fallback
- Interactive blocks (inputs, actions, buttons) still require `mrkdwn` format — only text rendering changes
- **CRITICAL: `send_block_kit_message()` return type must remain `bool`** — 8+ callers across agents/tools/workers depend on it returning `bool`. The fallback mechanism uses a separate private method instead.

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `src/adapters/__init__.py` | Modify | Add `attachments` and `source_text` fields to `ChannelPayload` |
| `src/adapters/slack_adapter.py` | Modify | Add `_text_to_markdown_blocks()`, update `render_text()`, update `render_table()`, add `render_text_fallback()` |
| `src/service/bots/slack_bot_service.py` | Modify | Accept optional `attachments` in send/update methods, add `_post_message_raw()` for fallback error detection |
| `src/workers/chat_worker/services/bot_message_processing_service.py` | Modify | Pass `attachments` from payload, implement markdown-block fallback logic |
| `tests/service/bots/test_slack_adapter.py` | Modify | Add tests for new rendering methods |

---

## Chunk 1: Markdown Block for Text Responses

### Task 1: Add `attachments` and `source_text` fields to `ChannelPayload`

**Files:**
- Modify: `src/adapters/__init__.py:54-59`
- Test: `tests/service/bots/test_slack_adapter.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/service/bots/test_slack_adapter.py`:

```python
def test_channel_payload_supports_attachments_and_source_text() -> None:
    from src.adapters import ChannelPayload

    payload = ChannelPayload(
        type="message",
        content={"blocks": []},
        text_fallback="test",
        attachments=[{"blocks": [{"type": "table", "rows": []}]}],
        source_text="Hello **world**",
    )
    assert payload.attachments is not None
    assert len(payload.attachments) == 1
    assert payload.source_text == "Hello **world**"


def test_channel_payload_defaults_are_none() -> None:
    from src.adapters import ChannelPayload

    payload = ChannelPayload(type="message", content={})
    assert payload.attachments is None
    assert payload.source_text is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/service/bots/test_slack_adapter.py::test_channel_payload_supports_attachments_and_source_text -v`
Expected: FAIL with `TypeError: unexpected keyword argument`

- [ ] **Step 3: Add fields to `ChannelPayload`**

In `src/adapters/__init__.py`, modify the `ChannelPayload` dataclass:

```python
@dataclass
class ChannelPayload:
    """Rendered payload ready to send to a messaging channel."""

    type: str  # "message", "invoke", etc.
    content: dict[str, Any]
    text_fallback: str | None = None  # Fallback text for channels that don't support rich UI
    attachments: list[dict[str, Any]] | None = None  # Slack attachments (e.g. table blocks)
    source_text: str | None = None  # Original markdown text for fallback re-rendering
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/service/bots/test_slack_adapter.py::test_channel_payload_supports_attachments_and_source_text tests/service/bots/test_slack_adapter.py::test_channel_payload_defaults_are_none -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/adapters/__init__.py tests/service/bots/test_slack_adapter.py
git commit -m "feat: add attachments and source_text fields to ChannelPayload"
```

---

### Task 2: Add `_text_to_markdown_blocks()` method

**Files:**
- Modify: `src/adapters/slack_adapter.py`
- Test: `tests/service/bots/test_slack_adapter.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/service/bots/test_slack_adapter.py`:

```python
def test_text_to_markdown_blocks_returns_markdown_type() -> None:
    blocks = SlackAdapter._text_to_markdown_blocks("Hello **world**")
    assert len(blocks) == 1
    assert blocks[0]["type"] == "markdown"
    assert blocks[0]["text"] == "Hello **world**"


def test_text_to_markdown_blocks_splits_at_limit() -> None:
    from src.adapters.slack_adapter import _MARKDOWN_BLOCK_LIMIT

    text = "A" * (_MARKDOWN_BLOCK_LIMIT + 500)
    blocks = SlackAdapter._text_to_markdown_blocks(text)
    assert len(blocks) >= 2
    assert all(b["type"] == "markdown" for b in blocks)
    assert "".join(b["text"] for b in blocks) == text


def test_text_to_markdown_blocks_groups_small_paragraphs() -> None:
    """Short paragraphs should be grouped into a single block."""
    text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
    blocks = SlackAdapter._text_to_markdown_blocks(text)
    # All three paragraphs fit well under 4000 chars, so they group into one block
    assert len(blocks) == 1
    assert "First paragraph." in blocks[0]["text"]
    assert "Second paragraph." in blocks[0]["text"]
    assert "Third paragraph." in blocks[0]["text"]


def test_text_to_markdown_blocks_preserves_tables_in_text() -> None:
    text = (
        "Here are the results:\n\n"
        "| Name | Status |\n|------|--------|\n| Deal A | Won |\n| Deal B | Lost |\n\n"
        "That's all."
    )
    blocks = SlackAdapter._text_to_markdown_blocks(text)
    full_text = "\n\n".join(b["text"] for b in blocks)
    assert "| Name | Status |" in full_text
    assert "| Deal A | Won |" in full_text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/service/bots/test_slack_adapter.py -k "test_text_to_markdown_blocks" -v`
Expected: FAIL with `AttributeError: type object 'SlackAdapter' has no attribute '_text_to_markdown_blocks'`

- [ ] **Step 3: Implement `_text_to_markdown_blocks()`**

In `src/adapters/slack_adapter.py`, add the module constant near `_SECTION_TEXT_LIMIT`:

```python
# Safe per-block limit for Slack markdown blocks (cumulative payload limit is 12000)
_MARKDOWN_BLOCK_LIMIT = 4000
```

Then add the static method to `SlackAdapter` class (after `_text_to_section_blocks`):

```python
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
                    {"type": "markdown", "text": stripped[start : start + _MARKDOWN_BLOCK_LIMIT]}
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/service/bots/test_slack_adapter.py -k "test_text_to_markdown_blocks" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/adapters/slack_adapter.py tests/service/bots/test_slack_adapter.py
git commit -m "feat: add _text_to_markdown_blocks for native Slack markdown rendering"
```

---

### Task 3: Update `render_text()` to use markdown blocks + add `render_text_fallback()`

**Files:**
- Modify: `src/adapters/slack_adapter.py` (lines 277-315)
- Test: `tests/service/bots/test_slack_adapter.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/service/bots/test_slack_adapter.py`:

```python
def test_render_text_uses_markdown_blocks() -> None:
    adapter = SlackAdapter()
    payload = adapter.render_text("Hello **world**\n\nThis is a test.")

    blocks = payload.content["blocks"]
    assert all(b["type"] == "markdown" for b in blocks)
    # Text should NOT be converted to mrkdwn — passed through as standard markdown
    assert any("**world**" in b["text"] for b in blocks)


def test_render_text_stores_source_text() -> None:
    """source_text should contain the original markdown for fallback re-rendering."""
    adapter = SlackAdapter()
    payload = adapter.render_text("Hello **world**")
    assert payload.source_text == "Hello **world**"


def test_render_text_has_mrkdwn_fallback_text() -> None:
    """The text_fallback should be mrkdwn for Slack notification previews."""
    adapter = SlackAdapter()
    payload = adapter.render_text("Hello **world**")
    # Fallback text is mrkdwn-converted for notification previews
    assert "*world*" in payload.text_fallback


def test_render_text_fallback_produces_mrkdwn_section_blocks() -> None:
    """render_text_fallback() should produce legacy mrkdwn section blocks."""
    adapter = SlackAdapter()
    # Input ends with punctuation so _is_heading_paragraph returns False
    # and all blocks stay as section type (not promoted to header).
    payload = adapter.render_text_fallback("Hello **world**, welcome.\n\nA paragraph.")

    blocks = payload.content["blocks"]
    assert all(b["type"] == "section" for b in blocks)
    assert all(b["text"]["type"] == "mrkdwn" for b in blocks)
    # Bold should be converted to mrkdwn format
    assert any("*world*" in b["text"]["text"] for b in blocks)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/service/bots/test_slack_adapter.py -k "test_render_text_uses_markdown_blocks or test_render_text_stores_source_text or test_render_text_has_mrkdwn_fallback_text or test_render_text_fallback_produces" -v`
Expected: FAIL — current `render_text` produces `section` blocks

- [ ] **Step 3: Update `render()`, `render_text()`, and add `render_text_fallback()`**

In `src/adapters/slack_adapter.py`:

Replace `render()`:

```python
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
```

Replace `render_text()`:

```python
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
```

Add `render_text_fallback()` right after `render_text()`:

```python
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
```

- [ ] **Step 4: Run all adapter tests to verify they pass**

Run: `uv run pytest tests/service/bots/test_slack_adapter.py -v`
Expected: PASS — existing `_text_to_section_blocks` tests still pass (they test the static method directly), new tests pass

- [ ] **Step 5: Commit**

```bash
git add src/adapters/slack_adapter.py tests/service/bots/test_slack_adapter.py
git commit -m "feat: use native Slack markdown blocks for text responses with fallback"
```

---

## Chunk 2: Table Block + Transport Layer

### Task 4: Update `render_table()` to produce Slack table blocks

**Files:**
- Modify: `src/adapters/slack_adapter.py` (lines 597-656)
- Test: `tests/service/bots/test_slack_adapter.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/service/bots/test_slack_adapter.py`:

```python
def test_render_table_produces_table_block_in_attachments() -> None:
    adapter = SlackAdapter()
    payload = adapter.render_table(
        title="Deal Pipeline",
        columns=["Name", "Stage", "Amount"],
        rows=[
            {"Name": "Acme Corp", "Stage": "Negotiation", "Amount": "$50k"},
            {"Name": "Beta Inc", "Stage": "Closed Won", "Amount": "$120k"},
        ],
        items=None,
    )

    assert payload.attachments is not None
    assert len(payload.attachments) == 1
    table_block = payload.attachments[0]["blocks"][0]
    assert table_block["type"] == "table"

    # Header row + 2 data rows = 3 rows
    assert len(table_block["rows"]) == 3

    header_cells = table_block["rows"][0]["cells"]
    assert header_cells[0]["text"] == "Name"
    assert header_cells[1]["text"] == "Stage"

    data_cells = table_block["rows"][1]["cells"]
    assert data_cells[0]["text"] == "Acme Corp"


def test_render_table_with_title_has_header_block() -> None:
    adapter = SlackAdapter()
    payload = adapter.render_table(
        title="My Table",
        columns=["A", "B"],
        rows=[{"A": "1", "B": "2"}],
        items=None,
    )

    blocks = payload.content["blocks"]
    assert blocks[0]["type"] == "header"
    assert blocks[0]["text"]["text"] == "My Table"


def test_render_table_items_still_uses_fields() -> None:
    """Key-value items should still use section fields (no table block)."""
    adapter = SlackAdapter()
    payload = adapter.render_table(
        title="Summary",
        columns=None,
        rows=None,
        items=[{"Key": "Value"}, {"Status": "Active"}],
    )

    assert payload.attachments is None
    blocks = payload.content["blocks"]
    assert any(b["type"] == "section" and "fields" in b for b in blocks)


def test_render_table_respects_100_row_limit() -> None:
    adapter = SlackAdapter()
    rows = [{"Col": f"row_{i}"} for i in range(150)]
    payload = adapter.render_table(
        title=None,
        columns=["Col"],
        rows=rows,
        items=None,
    )

    table_block = payload.attachments[0]["blocks"][0]
    # 1 header + 99 data rows = 100 (Slack max)
    assert len(table_block["rows"]) <= 100


def test_render_table_no_title_no_items_only_rows_still_sends() -> None:
    """Table with only rows (no title, no items) should still produce a valid payload."""
    adapter = SlackAdapter()
    payload = adapter.render_table(
        title=None,
        columns=["A"],
        rows=[{"A": "1"}],
        items=None,
    )

    # Attachments should have the table
    assert payload.attachments is not None
    # blocks may be empty but content should still have 'blocks' key
    assert "blocks" in payload.content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/service/bots/test_slack_adapter.py -k "test_render_table" -v`
Expected: FAIL — current `render_table` returns no `attachments` field

- [ ] **Step 3: Implement updated `render_table()`**

Add module constants near the top of `src/adapters/slack_adapter.py`:

```python
# Slack table block limits
_TABLE_MAX_ROWS = 100
_TABLE_MAX_COLS = 20
```

Replace the `render_table` method:

```python
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

        header_row = {
            "cells": [{"type": "raw_text", "text": col} for col in cols]
        }

        # Data rows (Slack max 100 total rows including header)
        max_data_rows = _TABLE_MAX_ROWS - 1
        data_rows = []
        for row in rows[:max_data_rows]:
            data_rows.append(
                {
                    "cells": [
                        {"type": "raw_text", "text": str(row.get(col, ""))}
                        for col in cols
                    ]
                }
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
```

**Note:** Always include `"blocks": blocks` in content (even if empty list) so the send layer always finds a `blocks` key.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/service/bots/test_slack_adapter.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/adapters/slack_adapter.py tests/service/bots/test_slack_adapter.py
git commit -m "feat: use native Slack table block for structured tabular data"
```

---

### Task 5: Wire `attachments` and fallback through the transport layer

This task updates the send methods to pass `attachments` and adds automatic fallback when the `markdown` block type is rejected by a Slack workspace.

**Design decision:** The `send_block_kit_message()` return type stays `bool` — 8+ callers depend on it (create_task, create_reminder, update_deal_stage, create_deal, add_stakeholder, add_deal_note, create_visualization_card, reminder_execution_service). Instead, we add a private `_post_message_raw()` method that returns the error code, used only by the fallback logic in `_send_slack_payload()`.

**Files:**
- Modify: `src/service/bots/slack_bot_service.py`
- Modify: `src/workers/chat_worker/services/bot_message_processing_service.py`

- [ ] **Step 1: Add `attachments` param to `send_block_kit_message()` and `_update_message_blocks()`**

In `src/service/bots/slack_bot_service.py`, update both methods to accept optional `attachments` and include them in the API payload. **Return type stays `bool`.**

For `send_block_kit_message`, change the signature and payload:

```python
async def send_block_kit_message(
    self,
    team_id: str,
    channel_id: str,
    blocks: list[dict[str, Any]],
    text_fallback: str,
    attachments: list[dict[str, Any]] | None = None,
) -> bool:
    """Send a Block Kit message to a Slack channel.

    Looks up the per-workspace bot token from bot_platform_installations.
    """
    bot_token = await self._get_workspace_token(team_id)
    if not bot_token:
        logger.error("Cannot send Block Kit message: no token for team=%s", team_id)
        return False

    url = f"{SLACK_API_BASE}/chat.postMessage"
    headers = {
        "Authorization": f"Bearer {bot_token}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "channel": channel_id,
        "blocks": blocks,
        "text": self._fallback_text(text_fallback),
    }
    if attachments:
        payload["attachments"] = attachments

    try:
        async with (
            aiohttp.ClientSession() as http_session,
            http_session.post(url, json=payload, headers=headers) as response,
        ):
            result = await response.json()
            if result.get("ok"):
                logger.info(
                    "Slack Block Kit message sent to channel=%s team=%s", channel_id, team_id
                )
                return True
            else:
                logger.error(
                    "Slack Block Kit API error: %s (team=%s)",
                    result.get("error", "unknown"),
                    team_id,
                )
                return False
    except Exception as e:
        logger.error("Failed to send Slack Block Kit message: %s", e, exc_info=True)
        return False
```

For `_update_message_blocks`, same pattern — add `attachments: list[dict[str, Any]] | None = None` param and include in payload:

```python
async def _update_message_blocks(
    self,
    team_id: str,
    channel_id: str,
    message_ts: str,
    blocks: list[dict[str, Any]],
    text_fallback: str,
    attachments: list[dict[str, Any]] | None = None,
) -> bool:
    """Update an existing Slack message with Block Kit blocks."""
    bot_token = await self._get_workspace_token(team_id)
    if not bot_token:
        logger.warning("Cannot update Slack message blocks: no token for team=%s", team_id)
        return False

    url = f"{SLACK_API_BASE}/chat.update"
    payload: dict[str, Any] = {
        "channel": channel_id,
        "ts": message_ts,
        "text": self._fallback_text(text_fallback),
        "blocks": blocks,
    }
    if attachments:
        payload["attachments"] = attachments

    try:
        async with (
            aiohttp.ClientSession() as http_session,
            http_session.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {bot_token}",
                    "Content-Type": "application/json",
                },
            ) as resp,
        ):
            data = await resp.json()
            if not data.get("ok"):
                logger.warning(
                    "Slack chat.update (blocks) failed: %s (team=%s, ts=%s, block_count=%d)",
                    data.get("error", "unknown"),
                    team_id,
                    message_ts,
                    len(blocks),
                )
            return data.get("ok", False)
    except Exception:
        logger.warning("Failed to update Slack message with blocks", exc_info=True)
        return False
```

- [ ] **Step 2: Add private `_post_message_raw()` method for fallback error detection**

Add to `SlackBotService` (this method returns the error code so the caller can detect `invalid_blocks`):

```python
async def _post_message_raw(
    self,
    team_id: str,
    channel_id: str,
    payload: dict[str, Any],
) -> tuple[bool, str]:
    """Low-level message post that returns (success, error_code).

    Used by the fallback logic to detect specific Slack API errors
    (e.g. invalid_blocks when markdown block type is unsupported).
    Caller provides blocks/text/attachments; this method adds channel.
    """
    bot_token = await self._get_workspace_token(team_id)
    if not bot_token:
        return False, "no_token"

    url = f"{SLACK_API_BASE}/chat.postMessage"
    api_payload = {**payload, "channel": channel_id}
    headers = {
        "Authorization": f"Bearer {bot_token}",
        "Content-Type": "application/json",
    }

    try:
        async with (
            aiohttp.ClientSession() as http_session,
            http_session.post(url, json=api_payload, headers=headers) as response,
        ):
            result = await response.json()
            if result.get("ok"):
                return True, ""
            error = result.get("error", "unknown")
            logger.warning("Slack API error: %s (team=%s)", error, team_id)
            return False, error
    except Exception as e:
        logger.error("Slack API call failed: %s", e, exc_info=True)
        return False, "exception"


async def _update_message_raw(
    self,
    team_id: str,
    channel_id: str,
    message_ts: str,
    payload: dict[str, Any],
) -> tuple[bool, str]:
    """Low-level message update that returns (success, error_code).

    Caller provides the payload dict with blocks/text/attachments.
    This method adds channel + ts and sends to chat.update.
    """
    bot_token = await self._get_workspace_token(team_id)
    if not bot_token:
        return False, "no_token"

    url = f"{SLACK_API_BASE}/chat.update"
    # Build final payload — caller should NOT include channel/ts
    api_payload = {**payload, "channel": channel_id, "ts": message_ts}
    headers = {
        "Authorization": f"Bearer {bot_token}",
        "Content-Type": "application/json",
    }

    try:
        async with (
            aiohttp.ClientSession() as http_session,
            http_session.post(url, json=api_payload, headers=headers) as response,
        ):
            result = await response.json()
            if result.get("ok"):
                return True, ""
            error = result.get("error", "unknown")
            logger.warning("Slack chat.update error: %s (team=%s)", error, team_id)
            return False, error
    except Exception as e:
        logger.error("Slack chat.update failed: %s", e, exc_info=True)
        return False, "exception"
```

- [ ] **Step 3: Update `_send_slack_payload()` with attachments + fallback logic**

In `src/workers/chat_worker/services/bot_message_processing_service.py`, replace `_send_slack_payload`:

```python
async def _send_slack_payload(self, reply_context: dict, payload: ChannelPayload) -> bool:
    """Send rendered payload to Slack via Web API.

    If reply_context contains a thinking_message_ts, the thinking placeholder
    is updated in-place via chat.update instead of posting a new message.

    If the Slack workspace rejects 'markdown' blocks (invalid_blocks error),
    automatically retries with legacy mrkdwn section blocks using the
    original markdown text stored in payload.source_text.
    """
    from src.service.bots.slack_bot_service import SlackBotService

    channel_id = reply_context.get("channel_id", "")
    team_id = reply_context.get("team_id", "")
    thinking_ts = reply_context.get("thinking_message_ts", "")

    if not channel_id or not team_id:
        logger.error(
            "Missing Slack reply context: channel_id=%s team_id=%s",
            bool(channel_id),
            bool(team_id),
        )
        return False

    bot_service = SlackBotService()
    blocks = payload.content.get("blocks") or []
    text = payload.content.get("text") or payload.text_fallback or ""
    attachments = payload.attachments

    # Check if this payload uses markdown blocks (needs fallback capability)
    has_markdown_blocks = any(b.get("type") == "markdown" for b in blocks)

    # Build the API payload (channel is added by _post_message_raw / _update_message_raw)
    api_payload: dict[str, Any] = {
        "text": bot_service._fallback_text(text),
    }
    if blocks:
        api_payload["blocks"] = blocks
    if attachments:
        api_payload["attachments"] = attachments

    # Send using raw methods (which return error codes) when we need fallback
    if has_markdown_blocks:
        if thinking_ts:
            success, error = await bot_service._update_message_raw(
                team_id, channel_id, thinking_ts, api_payload,
            )
        else:
            success, error = await bot_service._post_message_raw(
                team_id, channel_id, api_payload,
            )

        if success:
            return True

        # Fallback: if markdown blocks were rejected, retry with legacy mrkdwn
        if error in ("invalid_blocks", "invalid_block"):
            logger.warning(
                "Slack rejected markdown blocks, retrying with mrkdwn fallback (team=%s)",
                team_id,
            )
            source_text = payload.source_text
            if source_text:
                from src.adapters.slack_adapter import SlackAdapter

                adapter = SlackAdapter()
                fb_payload = adapter.render_text_fallback(source_text)
                fb_blocks = fb_payload.content.get("blocks") or []
                fb_text = fb_payload.content.get("text") or fb_payload.text_fallback or ""

                fb_api_payload: dict[str, Any] = {
                    "text": bot_service._fallback_text(fb_text),
                    "blocks": fb_blocks,
                }

                if thinking_ts:
                    success, _ = await bot_service._update_message_raw(
                        team_id, channel_id, thinking_ts, fb_api_payload,
                    )
                else:
                    success, _ = await bot_service._post_message_raw(
                        team_id, channel_id, fb_api_payload,
                    )
                return success

        return False

    # Non-markdown-block payloads: use existing methods (no fallback needed)
    if thinking_ts:
        if blocks or attachments:
            # Use _update_message_blocks when we have blocks OR attachments
            # (table-only payloads have empty blocks but non-null attachments)
            updated = await bot_service._update_message_blocks(
                team_id=team_id,
                channel_id=channel_id,
                message_ts=thinking_ts,
                blocks=blocks,
                text_fallback=text,
                attachments=attachments,
            )
        else:
            updated = await bot_service._update_message(
                team_id=team_id,
                channel_id=channel_id,
                message_ts=thinking_ts,
                text=text,
            )
        if updated:
            return True
        logger.warning("chat.update failed, falling back to new message")

    if blocks or attachments:
        return await bot_service.send_block_kit_message(
            team_id=team_id,
            channel_id=channel_id,
            blocks=blocks,
            text_fallback=text,
            attachments=attachments,
        )
    else:
        return await bot_service._send_message(team_id, channel_id, text)
```

**Key design choices:**
- `send_block_kit_message` return type stays `bool` — no existing callers break
- New `_post_message_raw` / `_update_message_raw` return `tuple[bool, str]` — used only by fallback logic
- Fallback uses `payload.source_text` (original markdown), not `text_fallback` (already-converted mrkdwn) — avoids double-conversion
- When `blocks` is empty but `attachments` exists (table-only payload), uses `send_block_kit_message` which handles both

- [ ] **Step 4: Run all tests**

Run: `uv run pytest tests/service/bots/ -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/service/bots/slack_bot_service.py src/workers/chat_worker/services/bot_message_processing_service.py
git commit -m "feat: wire attachments and markdown-block fallback through Slack transport"
```

---

## Chunk 3: Final Verification

### Task 6: Lint, type check, and verify

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest tests/service/bots/ tests/adapters/ -v`
Expected: All PASS

- [ ] **Step 2: Run linter**

Run: `uv run ruff check src/adapters/slack_adapter.py src/adapters/__init__.py src/service/bots/slack_bot_service.py src/workers/chat_worker/services/bot_message_processing_service.py`
Expected: No errors

- [ ] **Step 3: Run type checker**

Run: `uv run mypy src/adapters/slack_adapter.py src/adapters/__init__.py src/service/bots/slack_bot_service.py src/workers/chat_worker/services/bot_message_processing_service.py`
Expected: No errors (or only pre-existing issues)

- [ ] **Step 4: Final commit if any lint/type fixes needed**

```bash
git add -A
git commit -m "chore: lint and type fixes for Slack markdown block rendering"
```

---

## Summary of Changes

| Before | After |
|--------|-------|
| LLM text → `markdown_to_mrkdwn()` regex conversion → `section` blocks with `mrkdwn` type | LLM text → passed directly → `markdown` blocks (Slack handles rendering) |
| Tables in LLM text → ASCII code blocks or card-style layout | Tables in LLM text → rendered natively by Slack's markdown block |
| Structured table data → pipe-separated text in `section` blocks | Structured table data → native `table` block in `attachments` |
| No fallback if rendering breaks | Automatic fallback to legacy `mrkdwn` via `source_text` if workspace rejects `markdown` blocks |
| Interactive blocks (forms/questions) use `mrkdwn` | Unchanged — interactive blocks still use `mrkdwn` |
| `send_block_kit_message` returns `bool` | Still returns `bool` — no callers broken |

## What NOT to change

- `markdown_to_mrkdwn()` function — still needed for interactive blocks, fallback, and notification text
- `_text_to_section_blocks()` — still needed for fallback path
- `_convert_markdown_table()` and related helpers — still needed for fallback path
- Scorecard detection logic — still needed for fallback path
- Teams adapter — unaffected (uses Adaptive Cards)
- `create_task.py` card builders — these build Block Kit directly, not through `render_text()`
- Return types of any existing public methods
