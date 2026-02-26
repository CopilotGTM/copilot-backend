"""Tests for Slack adapter text chunking behavior."""

from __future__ import annotations

from src.adapters.slack_adapter import _SECTION_TEXT_LIMIT, SlackAdapter


def test_text_to_section_blocks_splits_oversized_single_paragraph() -> None:
    text = "A" * (_SECTION_TEXT_LIMIT * 2 + 17)

    blocks = SlackAdapter._text_to_section_blocks(text)
    block_texts = [block["text"]["text"] for block in blocks]

    assert len(blocks) == 3
    assert all(len(t) <= _SECTION_TEXT_LIMIT for t in block_texts)
    assert "".join(block_texts) == text
