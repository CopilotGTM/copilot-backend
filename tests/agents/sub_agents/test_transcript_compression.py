"""Tests for the transcript compression sub-agent.

Run with: uv run pytest tests/agents/sub_agents/test_transcript_compression.py -v
"""

import pytest

pytestmark = pytest.mark.unit


class TestTranscriptCompressionAgentImport:
    """Test that the compression agent can be imported and instantiated."""

    def test_import_compression_agent(self):
        """TranscriptCompressionAgent can be imported."""
        from src.agents.sub_agents.transcript_compression import (
            TranscriptCompressionAgent,
        )

        assert TranscriptCompressionAgent is not None

    def test_instantiate_compression_agent(self):
        """TranscriptCompressionAgent can be instantiated with default org name."""
        from src.agents.sub_agents.transcript_compression import (
            TranscriptCompressionAgent,
        )

        agent = TranscriptCompressionAgent()
        assert agent.agent is not None

    def test_instantiate_with_custom_org_name(self):
        """TranscriptCompressionAgent accepts custom org name."""
        from src.agents.sub_agents.transcript_compression import (
            TranscriptCompressionAgent,
        )

        agent = TranscriptCompressionAgent(org_name="Acme Corp")
        assert agent.agent is not None


class TestCompressionPrompt:
    """Test prompt generation."""

    def test_prompt_contains_org_name(self):
        """System prompt replaces org name placeholder."""
        from src.agents.sub_agents.transcript_compression.prompts import (
            get_compression_system_prompt,
        )

        prompt = get_compression_system_prompt("Acme Corp")
        assert "Acme Corp" in prompt
        assert "{{ORG_NAME}}" not in prompt

    def test_prompt_default_org_name(self):
        """System prompt uses default when empty org name given."""
        from src.agents.sub_agents.transcript_compression.prompts import (
            get_compression_system_prompt,
        )

        prompt = get_compression_system_prompt("  ")
        assert "Our Organization" in prompt

    def test_prompt_contains_output_format(self):
        """System prompt includes the expected output sections."""
        from src.agents.sub_agents.transcript_compression.prompts import (
            get_compression_system_prompt,
        )

        prompt = get_compression_system_prompt("Test Org")
        assert "### Key Dialogue" in prompt
        assert "### Decisions" in prompt
        assert "### Action Items" in prompt
        assert "### Unresolved" in prompt


class TestSegmentFormatting:
    """Test the segment formatting helper."""

    def test_format_segments_basic(self):
        """Segments are formatted with speaker and timestamp."""
        from src.agents.tools.intelligence.get_compressed_transcript import (
            _format_segments_for_compression,
        )

        segments = [
            {"speaker_name": "Alice", "content": "Hello", "start_time_seconds": 65},
            {"speaker_name": "Bob", "content": "Hi there", "start_time_seconds": 70},
        ]

        result = _format_segments_for_compression(segments)
        assert "Alice [00:01:05]: Hello" in result
        assert "Bob [00:01:10]: Hi there" in result

    def test_format_segments_missing_speaker(self):
        """Missing speaker name defaults to 'Unknown Speaker'."""
        from src.agents.tools.intelligence.get_compressed_transcript import (
            _format_segments_for_compression,
        )

        segments = [
            {"speaker_name": None, "content": "Hello", "start_time_seconds": 0},
        ]

        result = _format_segments_for_compression(segments)
        assert "Unknown Speaker" in result

    def test_format_segments_empty_content_skipped(self):
        """Segments with empty content are skipped."""
        from src.agents.tools.intelligence.get_compressed_transcript import (
            _format_segments_for_compression,
        )

        segments = [
            {"speaker_name": "Alice", "content": "", "start_time_seconds": 0},
            {"speaker_name": "Bob", "content": "Hello", "start_time_seconds": 5},
        ]

        result = _format_segments_for_compression(segments)
        assert "Alice" not in result
        assert "Bob" in result

    def test_format_segments_no_timestamp(self):
        """Segments without timestamps omit the timestamp bracket."""
        from src.agents.tools.intelligence.get_compressed_transcript import (
            _format_segments_for_compression,
        )

        segments = [
            {"speaker_name": "Alice", "content": "Hello", "start_time_seconds": None},
        ]

        result = _format_segments_for_compression(segments)
        assert "Alice" in result
        assert "Hello" in result
        # No timestamp bracket present
        assert "[" not in result


class TestToolRegistration:
    """Test that the tool is properly registered."""

    def test_compressed_transcript_in_deal_memory_tools(self):
        """GET_COMPRESSED_TRANSCRIPT is in DealMemoryAgent's tool set."""
        from src.agents.tools.registry import AGENT_TOOLS, ToolName

        deal_memory_tools = AGENT_TOOLS["DealMemoryAgent"]
        assert ToolName.GET_COMPRESSED_TRANSCRIPT in deal_memory_tools

    def test_raw_transcript_not_in_deal_memory_tools(self):
        """GET_MEETING_TRANSCRIPT is NOT in DealMemoryAgent's tool set."""
        from src.agents.tools.registry import AGENT_TOOLS, ToolName

        deal_memory_tools = AGENT_TOOLS["DealMemoryAgent"]
        assert ToolName.GET_MEETING_TRANSCRIPT not in deal_memory_tools

    def test_raw_transcript_still_in_other_agents(self):
        """GET_MEETING_TRANSCRIPT is still available for other agents."""
        from src.agents.tools.registry import AGENT_TOOLS, ToolName

        assert ToolName.GET_MEETING_TRANSCRIPT in AGENT_TOOLS["GeneralAgent"]
        assert ToolName.GET_MEETING_TRANSCRIPT in AGENT_TOOLS["SummaryAgent"]

    def test_compressed_transcript_is_available_for_summary_agent(self):
        """SummaryAgent can use condensed transcript context for long meetings."""
        from src.agents.tools.registry import AGENT_TOOLS, ToolName

        assert ToolName.GET_COMPRESSED_TRANSCRIPT in AGENT_TOOLS["SummaryAgent"]

    def test_general_agent_has_read_only_context_resolvers(self):
        """GeneralAgent can resolve common GTM shorthand before asking."""
        from src.agents.tools.registry import AGENT_TOOLS, ToolName

        general_tools = AGENT_TOOLS["GeneralAgent"]
        assert ToolName.GET_COMPRESSED_TRANSCRIPT in general_tools
        assert ToolName.LIST_MEETINGS in general_tools
        assert ToolName.LIST_MY_TASKS in general_tools
        assert ToolName.LIST_DEAL_TASKS in general_tools
        assert ToolName.LIST_CHAT_ATTACHMENTS in general_tools

    def test_tool_factory_import(self):
        """create_get_compressed_transcript can be imported."""
        from src.agents.tools.intelligence import create_get_compressed_transcript

        assert callable(create_get_compressed_transcript)


class TestAgentConfig:
    """Test compression agent config."""

    def test_config_exists(self):
        """transcript_compression_agent config is registered."""
        from src.infra.config.agent_config import agent_config

        config = agent_config.get_config("transcript_compression_agent")
        assert config.model == "gpt-5-mini"
        assert config.timeout_seconds == 300
