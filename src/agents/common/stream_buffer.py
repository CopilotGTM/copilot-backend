from __future__ import annotations

from collections.abc import AsyncIterator

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

            state["log_buffer"] += chunk.content

            if state["json_part_started"]:
                state["buffer"] += chunk.content
                continue

            for result in self._process_chunk_for_separator(chunk.content, state):
                yield result

        buffer_content = str(state["buffer"])
        json_started = bool(state["json_part_started"])
        yield ("final", buffer_content, json_started)

    def _is_valid_chunk(self, chunk) -> bool:
        """Check if chunk should be processed.

        Filters out:
        - Chunks without content
        - Tool completion events (contain noise like "func(query=...) completed in 12.1234s.")
        """
        if not hasattr(chunk, "content") or not chunk.content:
            return False

        # Filter out Agno tool completion events - they inject tool execution text into content
        # These events have content like "search_internal_knowledge(query=...) completed in 12.1234s."
        chunk_event = getattr(chunk, "event", None)
        if chunk_event and chunk_event in TOOL_COMPLETION_EVENTS:
            self.logger.debug(f"Filtering tool event: {chunk_event} - {chunk.content[:50]}...")
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
