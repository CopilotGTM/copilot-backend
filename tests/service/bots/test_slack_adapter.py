"""Tests for Slack adapter text rendering behavior."""

from __future__ import annotations

from src.adapters.slack_adapter import (
    _SECTION_TEXT_LIMIT,
    SlackAdapter,
    _convert_wide_table_to_cards,
)


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


def test_text_to_section_blocks_preserves_score_label_for_medpic_rows() -> None:
    blocks = SlackAdapter._text_to_section_blocks(
        "_Component_  ·  _Score_  ·  _Confidence_\n"
        "*Metrics*  ·  _Score:_ Weak  ·  _Confidence:_ Medium\n"
        "There are business-value signals, but no buyer-validated measurable outcome."
    )

    assert blocks[0]["type"] == "section"
    assert "_Score:_ Weak" in blocks[0]["text"]["text"]
    assert "_Rating:_" not in blocks[0]["text"]["text"]
