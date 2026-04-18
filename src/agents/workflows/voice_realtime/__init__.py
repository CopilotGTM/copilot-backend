"""Voice realtime workflow package."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agents.workflows.voice_realtime.agent import VoiceRealtimeAgent

__all__ = ["VoiceRealtimeAgent"]


def __getattr__(name: str):
    if name == "VoiceRealtimeAgent":
        from src.agents.workflows.voice_realtime.agent import VoiceRealtimeAgent

        return VoiceRealtimeAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
