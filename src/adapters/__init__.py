"""Channel adapters for rendering agent responses in channel-specific formats.

This module provides adapters for different messaging platforms (Teams, Slack, Web)
to render agent responses in channel-appropriate formats.

Architecture:
- ChannelAdapter: Abstract base class for all adapters
- TeamsAdapter: Renders responses as Adaptive Cards for Microsoft Teams
- SlackAdapter: Renders responses as Block Kit for Slack
- WebAdapter: Renders responses as React components for web chat

Usage:
    from src.adapters import get_adapter

    adapter = get_adapter("teams", agent)
    result = await adapter.process(user_message, channel_context)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from src.infra.messaging.chat_stream_models import Frame


class ResponseType(Enum):
    """Classification of agent response types for channel-specific rendering."""

    TEXT = "text"
    QUESTION = "question"
    FORM = "form"
    LIST = "list"
    TABLE = "table"
    CONFIRMATION = "confirmation"
    ERROR = "error"
    LOADING = "loading"


@dataclass
class ChannelContext:
    """Context information for channel-specific rendering."""

    channel: str  # "teams", "slack", "web"
    user_id: str
    conversation_id: str
    service_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChannelPayload:
    """Rendered payload ready to send to a messaging channel."""

    type: str  # "message", "invoke", etc.
    content: dict[str, Any]
    text_fallback: str | None = None  # Fallback text for channels that don't support rich UI
    attachments: list[dict[str, Any]] | None = None  # Slack attachments (e.g. table blocks)
    source_text: str | None = None  # Original markdown text for fallback re-rendering


@dataclass
class AgentResponse:
    """Unified agent response that can be rendered by any channel adapter."""

    text: str
    type: ResponseType
    confidence: float | None = None
    data: dict[str, Any] = field(default_factory=dict)
    sources: list[dict[str, Any]] | None = None
    suggestions: list[str] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_frames(cls, frames: list[Frame]) -> AgentResponse:
        """Build AgentResponse from accumulated frames."""
        text_parts: list[str] = []
        response_type = ResponseType.TEXT
        data: dict[str, Any] = {}
        sources: list[dict[str, Any]] = []
        suggestions: list[str] = []
        confidence: float | None = None

        for frame in frames:
            frame_type = frame.type

            if frame_type in ("answer.delta", "answer.replace"):
                content = getattr(frame, "content", "")
                if content:
                    text_parts.append(content)

            elif frame_type == "question.prompt":
                response_type = ResponseType.QUESTION
                data["prompt"] = getattr(frame, "prompt", "")
                data["fields"] = getattr(frame, "fields", [])
                data["await_user_response"] = getattr(frame, "await_user_response", True)

            elif frame_type == "structured_data":
                response_type = ResponseType.TABLE
                data["data_type"] = getattr(frame, "data_type", "list")
                data["title"] = getattr(frame, "title", None)
                data["columns"] = getattr(frame, "columns", None)
                data["rows"] = getattr(frame, "rows", None)
                data["items"] = getattr(frame, "items", None)

            elif frame_type == "sources.replace":
                srcs = getattr(frame, "sources", [])
                sources = [s.model_dump() if hasattr(s, "model_dump") else s for s in srcs]

            elif frame_type == "suggestions.replace":
                items = getattr(frame, "items", [])
                suggestions = [s.label if hasattr(s, "label") else str(s) for s in items]

            elif frame_type == "answer.metrics":
                confidence_val = getattr(frame, "confidence", None)
                if confidence_val:
                    confidence_map = {"high": 1.0, "medium": 0.7, "low": 0.4, "neutral": 0.5}
                    confidence = confidence_map.get(confidence_val, 0.5)

        return cls(
            text="".join(text_parts),
            type=response_type,
            confidence=confidence,
            data=data,
            sources=sources,
            suggestions=suggestions,
        )


class ChannelAdapter(ABC):
    """Abstract base class for channel adapters.

    All channel adapters must implement:
    - process(): Convert user message to channel payload
    - render(): Convert AgentResponse to ChannelPayload
    """

    def __init__(self, channel: str):
        self.channel = channel

    @abstractmethod
    async def process(
        self,
        message: str,
        context: ChannelContext,
        frames: list[Frame],
    ) -> ChannelPayload:
        """Process a message and return channel-specific payload."""
        ...

    @abstractmethod
    def render(self, response: AgentResponse) -> ChannelPayload:
        """Render an AgentResponse into a channel-specific payload."""
        ...

    @abstractmethod
    def render_text(self, text: str) -> ChannelPayload:
        """Render plain text response."""
        ...

    @abstractmethod
    def render_question(
        self,
        prompt: str,
        fields: list[dict[str, Any]] | None,
        data: dict[str, Any],
    ) -> ChannelPayload:
        """Render a question that requires user input."""
        ...

    @abstractmethod
    def render_table(
        self,
        title: str | None,
        columns: list[str] | None,
        rows: list[dict[str, Any]] | None,
        items: list[dict[str, Any]] | None,
    ) -> ChannelPayload:
        """Render structured data (table, list, comparison)."""
        ...

    @abstractmethod
    def render_error(self, message: str) -> ChannelPayload:
        """Render an error message."""
        ...


def get_adapter(channel: str, *args, **kwargs) -> ChannelAdapter:
    """Factory function to get the appropriate adapter for a channel.

    Args:
        channel: Channel identifier ("teams", "slack", "web")
        *args, **kwargs: Arguments to pass to the adapter constructor

    Returns:
        ChannelAdapter instance for the specified channel
    """
    channel = channel.lower()

    if channel == "teams":
        from src.adapters.teams_adapter import TeamsAdapter

        return TeamsAdapter(*args, **kwargs)
    elif channel == "slack":
        from src.adapters.slack_adapter import SlackAdapter

        return SlackAdapter(*args, **kwargs)
    else:
        raise ValueError(f"Unsupported channel: {channel}. Supported channels: teams, slack")


__all__ = [
    "ChannelAdapter",
    "ChannelContext",
    "ChannelPayload",
    "ResponseType",
    "AgentResponse",
    "get_adapter",
]
