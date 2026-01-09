"""
Unit tests for AgentTasksExtractor class.

These tests cover the extraction and parsing of inline AGENT_TASKS blocks
from streaming LLM responses.
"""

import pytest
from src.agents.common.agent_tasks_extractor import AgentTasksExtractor


class TestAgentTasksExtractor:
    """Test suite for AgentTasksExtractor."""

    @pytest.fixture
    def extractor(self):
        """Create a fresh AgentTasksExtractor instance for each test."""
        return AgentTasksExtractor()

    # =========================================================================
    # Basic Extraction Tests
    # =========================================================================

    def test_extract_single_complete_block(self, extractor):
        """Test extraction of a single complete AGENT_TASKS block."""
        content = """Let me help you.

⟦AGENT_TASKS⟧
[{"id": "1", "task": "Search knowledge base", "status": "in_progress"}]
⟦/AGENT_TASKS⟧

Searching now..."""

        result = extractor.process(content)

        assert len(result.frames) == 1
        assert result.frames[0].tasks is not None
        assert len(result.frames[0].tasks) == 1
        assert result.frames[0].tasks[0].id == "1"
        assert result.frames[0].tasks[0].task == "Search knowledge base"
        assert result.frames[0].tasks[0].status == "in_progress"
        assert "⟦AGENT_TASKS⟧" not in result.clean_content
        assert "Searching now..." in result.clean_content

    def test_extract_multiple_blocks(self, extractor):
        """Test extraction of multiple AGENT_TASKS blocks."""
        content = """⟦AGENT_TASKS⟧
[{"id": "1", "task": "Task A", "status": "in_progress"}]
⟦/AGENT_TASKS⟧

Working on task A...

⟦AGENT_TASKS⟧
["1:completed"]
⟦/AGENT_TASKS⟧

Done!"""

        result = extractor.process(content)

        assert len(result.frames) == 2
        assert result.frames[0].tasks is not None
        assert result.frames[1].updates is not None
        assert "Working on task A..." in result.clean_content
        assert "Done!" in result.clean_content

    def test_no_blocks_returns_original_content(self, extractor):
        """Test that content without blocks passes through unchanged."""
        content = "Just some regular content without any tasks."

        result = extractor.process(content)

        assert len(result.frames) == 0
        assert result.clean_content == content

    def test_incomplete_block_buffered(self, extractor):
        """Test that incomplete block is buffered for later completion."""
        # First chunk with incomplete block
        content1 = "Start\n⟦AGENT_TASKS⟧\n["
        result1 = extractor.process(content1)

        assert len(result1.frames) == 0
        assert result1.clean_content == "Start\n"

        # Complete the block
        content2 = '{"id": "1", "task": "Test", "status": "pending"}]\n⟦/AGENT_TASKS⟧\nEnd'
        result2 = extractor.process(content2)

        assert len(result2.frames) == 1
        assert result2.clean_content == "\nEnd"

    # =========================================================================
    # Full Task List Parsing
    # =========================================================================

    def test_parse_full_task_list(self, extractor):
        """Test parsing of full task list format."""
        content = """⟦AGENT_TASKS⟧
[{"id": "1", "task": "Search", "status": "completed"}, {"id": "2", "task": "Analyze", "status": "in_progress"}, {"id": "3", "task": "Report", "status": "pending"}]
⟦/AGENT_TASKS⟧"""

        result = extractor.process(content)

        assert len(result.frames) == 1
        tasks = result.frames[0].tasks
        assert len(tasks) == 3
        assert tasks[0].status == "completed"
        assert tasks[1].status == "in_progress"
        assert tasks[2].status == "pending"

    def test_parse_task_list_with_missing_id(self, extractor):
        """Test that tasks without id get auto-generated ids."""
        content = """⟦AGENT_TASKS⟧
[{"task": "Task without ID", "status": "pending"}]
⟦/AGENT_TASKS⟧"""

        result = extractor.process(content)

        assert len(result.frames) == 1
        assert result.frames[0].tasks[0].id == "task_1"

    def test_parse_task_list_with_invalid_status(self, extractor):
        """Test that invalid status defaults to pending."""
        content = """⟦AGENT_TASKS⟧
[{"id": "1", "task": "Test task", "status": "invalid_status"}]
⟦/AGENT_TASKS⟧"""

        result = extractor.process(content)

        assert result.frames[0].tasks[0].status == "pending"

    # =========================================================================
    # Delta Update Parsing
    # =========================================================================

    def test_parse_delta_status_updates(self, extractor):
        """Test parsing of delta update format for status changes."""
        content = """⟦AGENT_TASKS⟧
["1:completed", "2:in_progress"]
⟦/AGENT_TASKS⟧"""

        result = extractor.process(content)

        assert len(result.frames) == 1
        updates = result.frames[0].updates
        assert len(updates) == 2
        assert updates[0].id == "1"
        assert updates[0].status == "completed"
        assert updates[0].is_new is False
        assert updates[1].id == "2"
        assert updates[1].status == "in_progress"

    def test_parse_delta_new_task(self, extractor):
        """Test parsing of delta format for adding new tasks."""
        content = """⟦AGENT_TASKS⟧
["+3:Verify data:pending"]
⟦/AGENT_TASKS⟧"""

        result = extractor.process(content)

        updates = result.frames[0].updates
        assert len(updates) == 1
        assert updates[0].id == "3"
        assert updates[0].task == "Verify data"
        assert updates[0].status == "pending"
        assert updates[0].is_new is True

    def test_parse_delta_mixed_updates(self, extractor):
        """Test parsing of mixed status updates and new tasks."""
        content = """⟦AGENT_TASKS⟧
["1:completed", "+4:New task:in_progress", "2:completed"]
⟦/AGENT_TASKS⟧"""

        result = extractor.process(content)

        updates = result.frames[0].updates
        assert len(updates) == 3
        assert updates[0].is_new is False
        assert updates[1].is_new is True
        assert updates[1].task == "New task"
        assert updates[2].is_new is False

    def test_parse_delta_invalid_format_ignored(self, extractor):
        """Test that invalid delta formats are ignored."""
        content = """⟦AGENT_TASKS⟧
["1:completed", "invalid", "2:also_invalid_status", "3:pending"]
⟦/AGENT_TASKS⟧"""

        result = extractor.process(content)

        updates = result.frames[0].updates
        assert len(updates) == 2  # Only valid ones
        assert updates[0].id == "1"
        assert updates[1].id == "3"

    # =========================================================================
    # Error Handling
    # =========================================================================

    def test_invalid_json_logs_warning(self, extractor):
        """Test that invalid JSON is handled gracefully."""
        content = """⟦AGENT_TASKS⟧
{not valid json}
⟦/AGENT_TASKS⟧"""

        result = extractor.process(content)

        assert len(result.frames) == 0
        assert result.clean_content == ""

    def test_non_list_json_ignored(self, extractor):
        """Test that non-list JSON is ignored."""
        content = """⟦AGENT_TASKS⟧
{"not": "a list"}
⟦/AGENT_TASKS⟧"""

        result = extractor.process(content)

        assert len(result.frames) == 0

    def test_empty_list_returns_none(self, extractor):
        """Test that empty list returns no frame."""
        content = """⟦AGENT_TASKS⟧
[]
⟦/AGENT_TASKS⟧"""

        result = extractor.process(content)

        assert len(result.frames) == 0

    # =========================================================================
    # Flush Behavior
    # =========================================================================

    def test_flush_returns_remaining_content(self, extractor):
        """Test that flush returns any remaining buffered content."""
        extractor.process("Some content ")
        extractor.process("more content")

        # Simulate end of stream
        flush_result = extractor.flush()

        assert flush_result.clean_content == ""  # Already yielded
        assert len(flush_result.frames) == 0

    def test_flush_incomplete_block_discarded(self, extractor):
        """Test that incomplete blocks at end of stream are discarded."""
        extractor.process("Content\n⟦AGENT_TASKS⟧\n[incomplete")

        flush_result = extractor.flush()

        assert len(flush_result.frames) == 0
        assert "Content" in flush_result.clean_content or flush_result.clean_content == ""

    # =========================================================================
    # Streaming Simulation
    # =========================================================================

    def test_streaming_chunks(self, extractor):
        """Test processing content in small streaming chunks."""
        chunks = [
            "Let me ",
            "help you.\n\n",
            "⟦AGENT_TASKS⟧",
            "\n",
            '[{"id": "1", ',
            '"task": "Search", ',
            '"status": "in_progress"}]',
            "\n⟦/AGENT_TASKS⟧",
            "\n\nSearching...",
        ]

        all_clean = []
        all_frames = []

        for chunk in chunks:
            result = extractor.process(chunk)
            if result.clean_content:
                all_clean.append(result.clean_content)
            all_frames.extend(result.frames)

        # Flush remaining
        flush = extractor.flush()
        if flush.clean_content:
            all_clean.append(flush.clean_content)
        all_frames.extend(flush.frames)

        clean_content = "".join(all_clean)
        assert "Let me help you." in clean_content
        assert "Searching..." in clean_content
        assert "⟦AGENT_TASKS⟧" not in clean_content
        assert len(all_frames) == 1
        assert all_frames[0].tasks[0].task == "Search"
