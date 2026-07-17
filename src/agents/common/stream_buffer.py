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

# Agno skill tools registered via Skills.get_tools().
# These are called by the LLM just like regular tools but don't go through
# AgentCapabilities, so we surface them explicitly in the streaming pipeline.
SKILL_TOOL_NAMES = frozenset(
    {
        "get_skill_instructions",
        "get_skill_reference",
        "get_skill_script",
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

    async def stream(self, chat_response) -> AsyncIterator[tuple[str, Any, bool]]:
        state: dict[str, Any] = {
            "buffer": "",
            "json_part_started": False,
            "log_buffer": "",
            "run_completed_event": None,
        }

        async for chunk in chat_response:
            # Surface skill tool events so the caller can emit ToolUsage frames.
            skill_event = self._extract_skill_tool_event(chunk)
            if skill_event:
                yield ("skill_tool", skill_event, False)

            # Capture RunCompleted event for token metrics before filtering
            chunk_event = _get_chunk_attr(chunk, "event")
            if chunk_event == "RunCompleted":
                state["run_completed_event"] = chunk
                self.logger.info("StreamingAnswerBuffer captured RunCompleted event")

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

        # Yield the RunCompleted event (if captured) so callers can extract metrics
        if state.get("run_completed_event") is not None:
            self.logger.info("StreamingAnswerBuffer yielding run_completed event to caller")
            yield ("run_completed", state["run_completed_event"], False)
        else:
            self.logger.warning("StreamingAnswerBuffer: no RunCompleted event captured from stream")

    def _extract_skill_tool_event(self, chunk) -> dict[str, Any] | None:
        """Extract skill tool call data from Agno tool events.

        Returns a dict with tool_name, tool_args, and status when the chunk
        is a ToolCallStarted or ToolCallCompleted event for a skill tool.
        Returns None for non-skill or non-tool events.
        """
        chunk_event = _get_chunk_attr(chunk, "event")
        if not chunk_event or chunk_event not in TOOL_COMPLETION_EVENTS:
            return None

        tool = _get_chunk_attr(chunk, "tool")
        if not tool:
            return None

        tool_name = _get_chunk_attr(tool, "tool_name")
        if not tool_name or tool_name not in SKILL_TOOL_NAMES:
            return None

        tool_args = _get_chunk_attr(tool, "tool_args") or {}
        if chunk_event == "ToolCallStarted":
            status = "starting"
        elif chunk_event == "ToolCallCompleted":
            status = "completed"
        else:
            status = "in_progress"

        self.logger.info("Skill tool %s: %s(%s)", status, tool_name, tool_args)
        return {"tool_name": tool_name, "tool_args": tool_args, "status": status}

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

        chunk_event = _get_chunk_attr(chunk, "event")
        # RunCompleted may repeat the provider's full answer. It is captured
        # separately above for token metrics and must never re-enter the text stream.
        if chunk_event == "RunCompleted":
            return False

        # Filter out Agno tool completion events - they inject tool execution text into content
        # These events have content like "search_internal_knowledge(query=...) completed in 12.1234s."
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
