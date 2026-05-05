from __future__ import annotations

from src.agents.tools.registry import CHANNEL_ONLY_TOOLS, ToolName


def test_create_task_is_available_on_supported_chat_voice_and_web_channels() -> None:
    assert CHANNEL_ONLY_TOOLS[ToolName.CREATE_TASK] == {
        "teams",
        "slack",
        "whatsapp",
        "web",
        "voice",
    }
