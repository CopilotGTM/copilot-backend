"""Tests for Slack bot service helpers."""

from src.service.bots.slack_bot_service import SlackBotService


def test_fallback_text_truncates_long_strings() -> None:
    text = "A" * 4000

    truncated = SlackBotService._fallback_text(text, limit=3000)

    assert len(truncated) == 3000
    assert truncated.endswith("…")


def test_fallback_text_preserves_short_strings() -> None:
    text = "short text"

    assert SlackBotService._fallback_text(text, limit=3000) == text
