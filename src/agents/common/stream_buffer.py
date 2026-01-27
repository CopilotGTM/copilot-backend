from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from src.infra.utils.logger import get_logger

# Agno ModelResponseEvent values that contain tool execution noise, not actual answer content
# These are emitted by Agno when tools complete and contain text like:
# "search_internal_knowledge(query=...) completed in 12.1234s."
TOOL_COMPLETION_EVENTS = frozenset(
    {
        "ToolCallStarted",
        "ToolCallCompleted",
        "ToolCallPaused",
    }
)


def _get_chunk_attr(chunk: Any, attr: str, default: Any = None) -> Any:
    """Get attribute from chunk, handling both dict and object formats.

    Agno V2 may return chunks as dicts or objects depending on the context.
    """
    if isinstance(chunk, dict):
        return chunk.get(attr, default)
    return getattr(chunk, attr, default)


class StreamingAnswerBuffer:
    """Buffer streaming chunks and split answer vs metadata."""

    def __init__(self, separators: list[str], *, logger=None):
        self.separators = separators
        self.logger = logger or get_logger()
        self.max_sep_len = max((len(s) for s in separators), default=0)

    async def stream(self, chat_response) -> AsyncIterator[tuple[str, str, bool]]:
        state = {
            "buffer": "",
            "json_part_started": False,
            "log_buffer": "",
        }

        async for chunk in chat_response:
            if not self._is_valid_chunk(chunk):
                # Yield a "tick" to allow caller to process queued frames (e.g., tool.usage)
                # This happens when we filter tool completion events
                yield ("tick", "", False)
                continue

            content = _get_chunk_attr(chunk, "content", "")
            state["log_buffer"] += content

            if state["json_part_started"]:
                state["buffer"] += content
                continue

            for result in self._process_chunk_for_separator(content, state):
                yield result

        buffer_content = str(state["buffer"])
        json_started = bool(state["json_part_started"])
        yield ("final", buffer_content, json_started)

    def _is_valid_chunk(self, chunk) -> bool:
        """Check if chunk should be processed.

        Filters out:
        - Chunks without content
        - Tool completion events (contain noise like "func(query=...) completed in 12.1234s.")

        Handles both dict and object formats (Agno V2 compatibility).
        """
        content = _get_chunk_attr(chunk, "content")
        if not content:
            return False

        # Filter out Agno tool completion events - they inject tool execution text into content
        # These events have content like "search_internal_knowledge(query=...) completed in 12.1234s."
        chunk_event = _get_chunk_attr(chunk, "event")
        if chunk_event and chunk_event in TOOL_COMPLETION_EVENTS:
            self.logger.debug(f"Filtering tool event: {chunk_event} - {content[:50]}...")
            return False

        return True

    def _process_chunk_for_separator(self, content: str, state: dict):
        state["buffer"] += content

        separator_hit = self._find_earliest_separator(state["buffer"])

        if separator_hit:
            for result in self._handle_separator_found(separator_hit, state):
                yield result
        else:
            for result in self._handle_no_separator(state):
                yield result

    def _find_earliest_separator(self, buffer: str):
        candidates = [(sep, buffer.find(sep)) for sep in self.separators]
        valid_candidates = [(sep, idx) for sep, idx in candidates if idx != -1]

        return min(valid_candidates, key=lambda t: t[1]) if valid_candidates else None

    def _handle_separator_found(self, separator_hit, state: dict):
        sep, idx = separator_hit
        answer_part = state["buffer"][:idx]
        json_part = state["buffer"][idx + len(sep) :]

        if answer_part:
            yield ("answer", answer_part, state["json_part_started"])

        state["json_part_started"] = True
        state["buffer"] = json_part

    def _handle_no_separator(self, state: dict):
        safe_len = len(state["buffer"]) - (self.max_sep_len - 1)

        if safe_len > 0:
            to_send = state["buffer"][:safe_len]
            state["buffer"] = state["buffer"][safe_len:]
            yield ("answer", to_send, state["json_part_started"])
