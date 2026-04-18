from __future__ import annotations

from typing import Any

from src.service.voice_realtime.tool_manifest import (
    VOICE_REALTIME_SUPPORTED_TOOLS,
    build_voice_allowed_tools,
    build_voice_tool_schemas,
)

VOICE_REALTIME_QUEUE = "voice_realtime"
VOICE_REALTIME_TASK_NAME = "chat.voice_realtime_sideband"


def build_voice_realtime_allowed_tools() -> list[str]:
    return build_voice_allowed_tools()


def build_voice_realtime_tool_schemas() -> list[dict[str, Any]]:
    return build_voice_tool_schemas()


__all__ = [
    "VOICE_REALTIME_QUEUE",
    "VOICE_REALTIME_SUPPORTED_TOOLS",
    "VOICE_REALTIME_TASK_NAME",
    "build_voice_realtime_allowed_tools",
    "build_voice_realtime_tool_schemas",
]
