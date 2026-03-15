"""Tests for Slack adapter text rendering behavior."""

from __future__ import annotations

from src.adapters.slack_adapter import (
    _SECTION_TEXT_LIMIT,
    SlackAdapter,
    _convert_wide_table_to_cards,
)


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

    assert payload.attachments is not None
    assert "blocks" in payload.content


def test_text_to_section_blocks_splits_oversized_single_paragraph() -> None:
    text = "A" * (_SECTION_TEXT_LIMIT * 2 + 17)

    blocks = SlackAdapter._text_to_section_blocks(text)
    block_texts = [block["text"]["text"] for block in blocks]

    assert len(blocks) == 3
    assert all(len(t) <= _SECTION_TEXT_LIMIT for t in block_texts)
    assert "".join(block_texts) == text


def test_text_to_section_blocks_promotes_heading_like_paragraphs() -> None:
    blocks = SlackAdapter._text_to_section_blocks(
        "*Overall Assessment*\n\nThis is the summary paragraph."
    )

    assert blocks[0]["type"] == "header"
    assert blocks[0]["text"]["text"] == "Overall Assessment"
    assert blocks[1]["type"] == "section"
    assert blocks[1]["text"]["text"] == "This is the summary paragraph."


def test_convert_wide_table_to_cards_labels_secondary_columns() -> None:
    rendered = _convert_wide_table_to_cards(
        headers=["Dimension", "Rating", "Confidence", "Evidence"],
        body=[
            [
                "Budget",
                "Weak",
                "Medium",
                "The deal has a recorded amount but no buyer-confirmed spending range.",
            ]
        ],
    )

    assert "*Budget*" in rendered
    assert "_Rating:_ Weak" in rendered
    assert "_Confidence:_ Medium" in rendered


def test_text_to_section_blocks_converts_scorecard_rows_to_sections() -> None:
    blocks = SlackAdapter._text_to_section_blocks(
        "_Dimension_  ·  _Rating_  ·  _Confidence_\n"
        "*Budget*  ·  _Rating:_ Weak  ·  _Confidence:_ Medium\n"
        "The deal has a recorded amount but no buyer-confirmed spending range.\n\n"
        "*Authority*  ·  _Rating:_ Weak  ·  _Confidence:_ High\n"
        "Aditya is engaged, but no buyer authority is confirmed."
    )

    assert blocks[0]["type"] == "section"
    assert blocks[0]["text"]["text"].startswith("*Budget*")
    assert "_Rating:_ Weak" in blocks[0]["text"]["text"]
    assert "_Confidence:_ Medium" in blocks[0]["text"]["text"]
    assert "buyer-confirmed spending range" in blocks[0]["text"]["text"]
    assert blocks[1]["type"] == "section"
    assert blocks[1]["text"]["text"].startswith("*Authority*")


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


def test_channel_payload_defaults_are_none() -> None:
    from src.adapters import ChannelPayload

    payload = ChannelPayload(type="message", content={})
    assert payload.attachments is None
    assert payload.source_text is None


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
    assert "*world*" in payload.text_fallback


def test_render_text_fallback_produces_mrkdwn_section_blocks() -> None:
    """render_text_fallback() should produce legacy mrkdwn section blocks."""
    adapter = SlackAdapter()
    # Input ends with punctuation so _is_heading_paragraph returns False
    payload = adapter.render_text_fallback("Hello **world**, welcome.\n\nA paragraph.")

    blocks = payload.content["blocks"]
    assert all(b["type"] == "section" for b in blocks)
    assert all(b["text"]["type"] == "mrkdwn" for b in blocks)
    assert any("*world*" in b["text"]["text"] for b in blocks)


def test_text_to_section_blocks_preserves_score_label_for_medpic_rows() -> None:
    blocks = SlackAdapter._text_to_section_blocks(
        "_Component_  ·  _Score_  ·  _Confidence_\n"
        "*Metrics*  ·  _Score:_ Weak  ·  _Confidence:_ Medium\n"
        "There are business-value signals, but no buyer-validated measurable outcome."
    )

    assert blocks[0]["type"] == "section"
    assert "_Score:_ Weak" in blocks[0]["text"]["text"]
    assert "_Rating:_" not in blocks[0]["text"]["text"]
