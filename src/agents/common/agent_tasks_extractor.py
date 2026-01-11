"""Extractor for inline AGENT_TASKS blocks during streaming.

The agent emits task tracking blocks inline in its response:
    ⟦AGENT_TASKS⟧
    [{"id": "1", "task": "Search knowledge base", "status": "in_progress"}, ...]
    ⟦/AGENT_TASKS⟧

This extractor:
1. Detects these blocks during streaming
2. Extracts and parses the JSON content
3. Returns clean answer content (without the blocks) and parsed task frames
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from src.api.schemas.chat_stream_models import TaskItem, TaskListFrame, TaskUpdate
from src.infra.utils.logger import get_logger

logger = get_logger()

# Sentinels for inline task blocks
AGENT_TASKS_START = "⟦AGENT_TASKS⟧"
AGENT_TASKS_END = "⟦/AGENT_TASKS⟧"


@dataclass
class ExtractionResult:
    """Result of processing content for AGENT_TASKS blocks."""

    clean_content: str  # Content with AGENT_TASKS blocks removed
    frames: list[TaskListFrame]  # Parsed task frames


class AgentTasksExtractor:
    """Extract and parse inline AGENT_TASKS blocks from streaming content.

    Designed to work with incremental streaming:
    - Accumulates content until a complete block is found
    - Strips blocks from answer content
    - Emits TaskListFrame for each complete block
    """

    def __init__(self):
        self._pending_buffer: str = ""

    def process(self, content: str) -> ExtractionResult:
        """Process incoming content, extract complete AGENT_TASKS blocks.

        Args:
            content: New content chunk to process

        Returns:
            ExtractionResult with clean content and any parsed frames
        """
        # Add to pending buffer
        self._pending_buffer += content

        frames: list[TaskListFrame] = []
        clean_parts: list[str] = []

        # Process all complete blocks
        while AGENT_TASKS_START in self._pending_buffer:
            start_idx = self._pending_buffer.find(AGENT_TASKS_START)

            # Check if we have the closing tag
            end_idx = self._pending_buffer.find(AGENT_TASKS_END, start_idx)
            if end_idx == -1:
                # Block not complete yet - keep content before start, buffer the rest
                clean_parts.append(self._pending_buffer[:start_idx])
                self._pending_buffer = self._pending_buffer[start_idx:]
                break

            # Extract the complete block
            block_end = end_idx + len(AGENT_TASKS_END)

            # Add content before the block to clean output
            clean_parts.append(self._pending_buffer[:start_idx])

            # Parse the block content
            block_content = self._pending_buffer[
                start_idx + len(AGENT_TASKS_START) : end_idx
            ].strip()
            frame = self._parse_block(block_content)
            if frame:
                frames.append(frame)

            # Continue processing after this block
            self._pending_buffer = self._pending_buffer[block_end:]

        # If no complete blocks found/remaining, check for partial sentinel matches
        if AGENT_TASKS_START not in self._pending_buffer:
            # Check if buffer ends with a partial sentinel prefix that we should keep
            kept = self._keep_partial_sentinel(self._pending_buffer)
            if kept:
                # Send everything except the partial match
                clean_parts.append(self._pending_buffer[: -len(kept)])
                self._pending_buffer = kept
            else:
                clean_parts.append(self._pending_buffer)
                self._pending_buffer = ""

        return ExtractionResult(
            clean_content="".join(clean_parts),
            frames=frames,
        )

    def flush(self) -> ExtractionResult:
        """Flush any remaining buffered content.

        Call this at the end of streaming to get any remaining content.
        Incomplete AGENT_TASKS blocks are discarded with a warning.
        """
        if not self._pending_buffer:
            return ExtractionResult(clean_content="", frames=[])

        # Check for incomplete block
        if AGENT_TASKS_START in self._pending_buffer:
            logger.warning(
                "Incomplete AGENT_TASKS block at end of stream, discarding: %s",
                self._pending_buffer[:100],
            )
            # Strip the incomplete block
            start_idx = self._pending_buffer.find(AGENT_TASKS_START)
            clean = self._pending_buffer[:start_idx]
        else:
            clean = self._pending_buffer

        self._pending_buffer = ""
        return ExtractionResult(clean_content=clean, frames=[])

    def _parse_block(self, content: str) -> TaskListFrame | None:
        """Parse AGENT_TASKS block content into TaskListFrame.

        Supports two formats:
        1. Full list: [{"id": "1", "task": "...", "status": "pending"}, ...]
        2. Delta updates: ["1:completed", "2:in_progress", "+3:New task:pending"]
        """
        if not content:
            return None

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            logger.warning("Failed to parse AGENT_TASKS JSON: %s", content[:200])
            return None

        if not isinstance(data, list):
            logger.warning("AGENT_TASKS content is not a list: %s", type(data))
            return None

        if not data:
            return None

        # Detect format by first element
        first = data[0]

        if isinstance(first, dict):
            # Full task list format
            tasks = self._parse_full_list(data)
            if tasks:
                return TaskListFrame(tasks=tasks)

        elif isinstance(first, str):
            # Delta update format
            updates = self._parse_delta_updates(data)
            if updates:
                return TaskListFrame(updates=updates)

        return None

    def _parse_full_list(self, data: list) -> list[TaskItem]:
        """Parse full task list format."""
        tasks = []
        for idx, item in enumerate(data):
            if not isinstance(item, dict):
                continue

            task_id = str(item.get("id", f"task_{idx + 1}"))
            task_desc = item.get("task", "")
            if not task_desc:
                continue

            status = item.get("status", "pending")
            if status not in ("pending", "in_progress", "completed"):
                status = "pending"

            tasks.append(TaskItem(id=task_id, task=task_desc, status=status))
        return tasks

    def _keep_partial_sentinel(self, buffer: str) -> str:
        """Check if buffer ends with a partial sentinel prefix.

        Returns the suffix to keep in the buffer if it could be the start of a sentinel,
        or empty string if no partial match found.
        """
        if not buffer:
            return ""

        # Check both START and END sentinels for partial matches
        # We need to check if the end of the buffer matches any prefix of the sentinels
        for sentinel in [AGENT_TASKS_START, AGENT_TASKS_END]:
            # Check each possible prefix of the sentinel (from longest to shortest)
            # e.g., for "⟦AGENT_TASKS⟧" check "⟦AGENT_TASKS", "⟦AGENT_TASK", etc.
            for prefix_len in range(len(sentinel) - 1, 0, -1):
                prefix = sentinel[:prefix_len]
                if buffer.endswith(prefix):
                    return prefix

        return ""

    def _parse_delta_updates(self, data: list) -> list[TaskUpdate]:
        """Parse delta update format.

        Format: ["1:completed", "2:in_progress", "+3:New task:pending"]
        - "id:status" for status changes
        - "+id:task:status" for new tasks
        """
        updates = []
        for item in data:
            if not isinstance(item, str):
                continue

            item = item.strip()
            if not item:
                continue

            # New task format: "+id:task:status"
            if item.startswith("+"):
                parts = item[1:].split(":", 2)
                if len(parts) >= 3:
                    task_id, task_desc, status = parts[0], parts[1], parts[2]
                    if status not in ("pending", "in_progress", "completed"):
                        status = "pending"
                    updates.append(
                        TaskUpdate(id=task_id, task=task_desc, status=status, is_new=True)
                    )
            else:
                # Status update format: "id:status"
                parts = item.split(":", 1)
                if len(parts) == 2:
                    task_id, status = parts[0], parts[1]
                    if status in ("pending", "in_progress", "completed"):
                        updates.append(TaskUpdate(id=task_id, status=status))

        return updates
