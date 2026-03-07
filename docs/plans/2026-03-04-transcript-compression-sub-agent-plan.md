# Transcript Compression Sub-Agent Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a transcript compression sub-agent that condenses raw meeting transcripts into speaker-attributed key points, exposed as a tool for the Deal Memory agent.

**Architecture:** A new `TranscriptCompressionAgent` in `src/agents/sub_agents/transcript_compression/` compresses transcripts using gpt-5o-mini. A new tool `get_compressed_transcript` in `src/agents/tools/intelligence/` wraps this agent. The tool replaces `GET_MEETING_TRANSCRIPT` in the Deal Memory agent's tool set.

**Tech Stack:** Python 3.11, Agno Agent framework, Azure OpenAI (gpt-5o-mini), SQLAlchemy async sessions.

---

### Task 1: Add agent config for transcript compression agent

**Files:**

- Modify: `src/infra/config/agent_config.py:87-131`

**Step 1: Add config vars after the Deal Memory Agent section (after line 90)**

Add these three class-level vars inside `AgentConfig`, after the Deal Memory block:

```python
    # Transcript Compression Sub-Agent - condenses transcripts for deal memory
    TRANSCRIPT_COMPRESSION_MODEL: str = os.getenv(
        "AGENT_TRANSCRIPT_COMPRESSION_MODEL", "gpt-5o-mini"
    )
    TRANSCRIPT_COMPRESSION_DEBUG: bool = (
        os.getenv("AGENT_TRANSCRIPT_COMPRESSION_DEBUG", "false").lower() == "true"
    )
    TRANSCRIPT_COMPRESSION_TIMEOUT: int = int(
        os.getenv("AGENT_TRANSCRIPT_COMPRESSION_TIMEOUT", "60")
    )
```

**Step 2: Add entry to `_CONFIG_MAP` (after the `deal_memory_agent` entry, around line 131)**

```python
        "transcript_compression_agent": (
            "TRANSCRIPT_COMPRESSION_MODEL",
            "TRANSCRIPT_COMPRESSION_DEBUG",
            "TRANSCRIPT_COMPRESSION_TIMEOUT",
        ),
```

**Step 3: Verify config works**

Run: `uv run python -c "from src.infra.config.agent_config import agent_config; c = agent_config.get_config('transcript_compression_agent'); print(c)"`

Expected: `AgentModelConfig(model='gpt-5o-mini', api_version='2024-12-01-preview', debug_mode=False, timeout_seconds=60)`

**Step 4: Commit**

```bash
git add src/infra/config/agent_config.py
git commit -m "feat(agents): add transcript compression agent config"
```

---

### Task 2: Create the compression prompt

**Files:**

- Create: `src/agents/sub_agents/__init__.py`
- Create: `src/agents/sub_agents/transcript_compression/__init__.py`
- Create: `src/agents/sub_agents/transcript_compression/prompts.py`

**Step 1: Create the directory structure and empty `__init__.py` files**

Create `src/agents/sub_agents/__init__.py` — empty file.

Create `src/agents/sub_agents/transcript_compression/__init__.py`:

```python
"""Transcript compression sub-agent.

Condenses raw meeting transcripts into speaker-attributed key points.
Used as a sub-agent inside the get_compressed_transcript tool.
"""

from src.agents.sub_agents.transcript_compression.agent import TranscriptCompressionAgent

__all__ = ["TranscriptCompressionAgent"]
```

**Step 2: Create the prompts file**

Create `src/agents/sub_agents/transcript_compression/prompts.py`:

```python
"""Prompts for the transcript compression sub-agent."""

from textwrap import dedent

COMPRESSION_SYSTEM_PROMPT_TEMPLATE = dedent("""\
You are a transcript compression specialist at {{ORG_NAME}}.

Your job: condense a raw meeting transcript into speaker-attributed key points.
You receive timestamped, speaker-labeled segments and return a structured summary.

## Rules

1. **Attribute every point to a speaker**: "Speaker Name: what they said/claimed/proposed"
2. **Keep only substantive content**: claims, decisions, commitments, concerns, questions, \
proposals, objections, technical details, action items
3. **Remove**: filler words, pleasantries, repetition, off-topic tangents, "um/uh", \
small talk, scheduling logistics
4. **Preserve exact meaning**: do not editorialize, interpret, or add information \
not in the transcript
5. **If a speaker's role is apparent** (e.g., they introduce themselves as VP Sales), \
include it in parentheses on first mention
6. **Keep chronological order** within each section
7. **Target output: ~1500 tokens maximum** — be ruthlessly concise

## Output Format

Use this exact structure:

### Key Dialogue
- Speaker Name (Role if known): What they said or proposed
- Speaker Name: Their response or counter-point
(List all substantive speaker-attributed exchanges)

### Decisions
- What was decided and by whom (or "None" if no decisions made)

### Action Items
- Who committed to do what, by when (or "None" if no action items)

### Unresolved
- Open questions, disagreements, or items deferred (or "None")
""")


def get_compression_system_prompt(org_name: str) -> str:
    """Build the system prompt for the transcript compression agent."""
    safe_org_name = org_name.strip() or "Our Organization"
    return COMPRESSION_SYSTEM_PROMPT_TEMPLATE.replace("{{ORG_NAME}}", safe_org_name)
```

**Step 3: Commit**

```bash
git add src/agents/sub_agents/
git commit -m "feat(agents): add transcript compression sub-agent prompts"
```

---

### Task 3: Create the TranscriptCompressionAgent class

**Files:**

- Create: `src/agents/sub_agents/transcript_compression/agent.py`

**Step 1: Create the agent file**

Create `src/agents/sub_agents/transcript_compression/agent.py`:

```python
"""TranscriptCompressionAgent — lightweight sub-agent that compresses transcripts.

No tools, no DB access. Receives formatted transcript text, returns condensed markdown.
Designed to run inside the get_compressed_transcript tool.
"""

from __future__ import annotations

import logging

from agno.agent import Agent
from agno.models.azure.openai_chat import AzureOpenAI

from src.agents.sub_agents.transcript_compression.prompts import (
    get_compression_system_prompt,
)
from src.infra.config.agent_config import agent_config

logger = logging.getLogger(__name__)


class TranscriptCompressionAgent:
    """Compresses a single meeting transcript into speaker-attributed key points.

    Stateless, no tools, no DB access. Created per-call inside the tool wrapper.
    """

    def __init__(self, org_name: str = "Our Organization"):
        config = agent_config.get_config("transcript_compression_agent")

        self.agent = Agent(
            model=AzureOpenAI(
                id=config.model,
                api_version=config.api_version,
            ),
            description=get_compression_system_prompt(org_name),
            markdown=True,
            debug_mode=config.debug_mode,
        )

    async def compress(
        self,
        meeting_title: str,
        meeting_date: str,
        segments_text: str,
    ) -> str:
        """Compress a formatted transcript into speaker-attributed key points.

        Args:
            meeting_title: Title of the meeting (e.g., "Q3 Planning Review")
            meeting_date: Date string (e.g., "2025-01-15")
            segments_text: Pre-formatted transcript segments with speaker labels

        Returns:
            Condensed markdown string with Key Dialogue, Decisions, Action Items, Unresolved.
        """
        user_prompt = (
            f"## Meeting: {meeting_title} ({meeting_date})\n\n"
            f"## Transcript\n\n{segments_text}"
        )

        logger.info(
            "TranscriptCompressionAgent compressing transcript for '%s' (%s), "
            "input length: %d chars",
            meeting_title,
            meeting_date,
            len(segments_text),
        )

        response = await self.agent.arun(user_prompt)
        content = response.content

        result = content if isinstance(content, str) else str(content)

        logger.info(
            "TranscriptCompressionAgent compressed '%s': %d → %d chars",
            meeting_title,
            len(segments_text),
            len(result),
        )

        return result
```

**Step 2: Verify import works**

Run: `uv run python -c "from src.agents.sub_agents.transcript_compression import TranscriptCompressionAgent; print('OK')"`

Expected: `OK`

**Step 3: Commit**

```bash
git add src/agents/sub_agents/transcript_compression/agent.py
git commit -m "feat(agents): add TranscriptCompressionAgent class"
```

---

### Task 4: Create the get_compressed_transcript tool

**Files:**

- Create: `src/agents/tools/intelligence/get_compressed_transcript.py`
- Modify: `src/agents/tools/intelligence/__init__.py:1-20`

This is the largest task. The tool follows the exact same factory pattern as `get_meeting_transcript.py`.

**Step 1: Create the tool file**

Create `src/agents/tools/intelligence/get_compressed_transcript.py`:

```python
"""Get compressed meeting transcript tool for agents.

Fetches transcript segments from DB, then uses TranscriptCompressionAgent
to condense them into speaker-attributed key points. The raw transcript
never enters the parent agent's context — only the compressed output.

SECURITY: organisation_id is taken from context (immutable).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.agents.sub_agents.transcript_compression import TranscriptCompressionAgent
from src.agents.tools.base import execute_with_capabilities
from src.domain.models.organisation_tables.sync.calendar import CalendarEvent
from src.domain.models.organisation_tables.sync.meeting_intelligence import (
    Meeting,
    MeetingStatus,
)
from src.service.integrations.transcripts import TranscriptService

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


def _format_segments_for_compression(segments: list[dict[str, Any]]) -> str:
    """Format transcript segments into a readable speaker-labeled text.

    Args:
        segments: List of segment dicts with speaker_name, content, start_time_seconds.

    Returns:
        Formatted string like "Speaker Name [00:03:15]: content\\n..."
    """
    lines = []
    for seg in segments:
        speaker = seg.get("speaker_name") or "Unknown Speaker"
        content = (seg.get("content") or "").strip()
        if not content:
            continue

        start_seconds = seg.get("start_time_seconds")
        if start_seconds is not None:
            minutes, secs = divmod(int(start_seconds), 60)
            hours, minutes = divmod(minutes, 60)
            timestamp = f"[{hours:02d}:{minutes:02d}:{secs:02d}]"
        else:
            timestamp = ""

        lines.append(f"{speaker} {timestamp}: {content}")

    return "\n".join(lines)


async def get_compressed_transcript(
    event_id: str,
    *,
    context: AgentContext,
    org_name: str = "Our Organization",
) -> dict[str, Any] | None:
    """Fetch transcript segments, compress via sub-agent, return condensed output.

    Workflow:
    1. Ensure transcript exists (fetch from provider if needed)
    2. Load transcript segments from DB (speaker-attributed)
    3. Release DB session
    4. Run TranscriptCompressionAgent to condense
    5. Return compressed result

    SECURITY: organisation_id comes from context, not parameters.
    """
    organisation_id = context.organisation_id
    if not organisation_id:
        logger.warning("get_compressed_transcript: missing organisation_id in context")
        return None

    try:
        parsed_event_id = UUID(event_id)
    except ValueError:
        logger.warning("get_compressed_transcript: invalid event_id: %s", event_id)
        return None

    # Step 1: Ensure transcript exists
    transcript_service = TranscriptService(organisation_id)
    try:
        await transcript_service.ensure_transcript(parsed_event_id)
    except Exception as e:
        logger.warning("get_compressed_transcript: ensure_transcript failed: %s", e)

    # Step 2: Fetch meeting with segments (fresh session)
    meeting_c = cast(Any, Meeting).__table__.c
    event_c = cast(Any, CalendarEvent).__table__.c

    segments_data: list[dict[str, Any]] = []
    title: str | None = None
    meeting_date: str | None = None

    async with context.acquire_session() as session:
        meeting_stmt = (
            select(Meeting)
            .options(selectinload(cast(Any, Meeting).transcript_segments))
            .where(
                meeting_c.organisation_id == organisation_id,
                meeting_c.calendar_event_id == parsed_event_id,
                meeting_c.is_deleted.is_(False),
            )
        )

        try:
            result = await session.execute(meeting_stmt)
            meeting = result.scalar_one_or_none()
        except Exception as e:
            logger.exception("get_compressed_transcript: error fetching meeting: %s", e)
            return None

        if not meeting:
            logger.info("get_compressed_transcript: no meeting found for event: %s", event_id)
            return None

        if meeting.status != MeetingStatus.READY:
            return {
                "event_id": event_id,
                "transcript_available": False,
                "status": (
                    meeting.status.value
                    if isinstance(meeting.status, MeetingStatus)
                    else meeting.status
                ),
            }

        # Extract segments while session is active
        for seg in sorted(meeting.transcript_segments, key=lambda s: s.segment_index):
            segments_data.append(
                {
                    "speaker_name": seg.speaker_name,
                    "content": seg.content,
                    "start_time_seconds": seg.start_time_seconds,
                }
            )

        # Fetch event for title/date
        event_stmt = select(CalendarEvent).where(event_c.event_id == parsed_event_id)
        try:
            event_result = await session.execute(event_stmt)
            event = event_result.scalar_one_or_none()
            if event:
                title = event.title
                meeting_date = (
                    event.start_at.date().isoformat() if event.start_at else None
                )
        except Exception as e:
            logger.warning("get_compressed_transcript: error fetching event: %s", e)

    # Session released — safe to call sub-agent

    if not segments_data:
        logger.info("get_compressed_transcript: no segments for event %s", event_id)
        return {
            "event_id": event_id,
            "title": title,
            "transcript_available": True,
            "compressed_transcript": "(No transcript segments available)",
        }

    # Step 3: Format and compress
    formatted_text = _format_segments_for_compression(segments_data)
    safe_title = title or "Untitled Meeting"
    safe_date = meeting_date or "Unknown Date"

    compression_agent = TranscriptCompressionAgent(org_name=org_name)
    compressed = await compression_agent.compress(safe_title, safe_date, formatted_text)

    return {
        "event_id": event_id,
        "title": title,
        "meeting_date": meeting_date,
        "transcript_available": True,
        "compressed_transcript": compressed,
        "original_segment_count": len(segments_data),
    }


def create_get_compressed_transcript(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create get_compressed_transcript tool bound to a context.

    SECURITY: The context contains the immutable organisation_id.
    """

    async def get_compressed_transcript_tool(
        event_id: str,
    ) -> dict[str, Any] | None:
        """Get a compressed, speaker-attributed summary of a meeting transcript.

        Use this tool when you need to understand what was discussed in a meeting.
        Returns a condensed version with key dialogue points attributed to speakers,
        plus decisions, action items, and unresolved items.

        Much more efficient than reading the raw transcript — use this as your
        primary way to access meeting content.

        Args:
            event_id: The calendar event UUID (from list_deal_meetings)

        Note: organisation_id is automatically set from context and cannot be overridden.
        """
        logger.info(
            "[TOOL CALLED] get_compressed_transcript - event_id='%s'",
            event_id,
        )

        if not context.organisation_id:
            logger.warning(
                "get_compressed_transcript unavailable: missing organisation_id"
            )
            return None

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="GetCompressedTranscript",
            tool_func=get_compressed_transcript,
            parameters={
                "event_id": event_id,
                "context": context,
                "org_name": context.org_name,
            },
            description=f"Compressing transcript for meeting: {event_id}",
            fallback_value=None,
        )

        if result:
            available = result.get("transcript_available", False)
            logger.info(
                "[TOOL RESULT] get_compressed_transcript - available=%s", available
            )
        else:
            logger.info(
                "[TOOL RESULT] get_compressed_transcript returned None"
            )

        return dict(result) if result else None

    return get_compressed_transcript_tool
```

**Step 2: Update `__init__.py` to export the new factory**

Modify `src/agents/tools/intelligence/__init__.py` — add the import and export:

```python
"""Meeting intelligence agent tools.

Tools for listing and retrieving meeting information for deals.
"""

from src.agents.tools.intelligence.get_compressed_transcript import (
    create_get_compressed_transcript,
)
from src.agents.tools.intelligence.get_meeting_deal_context import create_get_meeting_deal_context
from src.agents.tools.intelligence.get_meeting_details import create_get_meeting_details
from src.agents.tools.intelligence.get_meeting_summary import create_get_meeting_summary
from src.agents.tools.intelligence.get_meeting_transcript import create_get_meeting_transcript
from src.agents.tools.intelligence.list_meetings import create_list_deal_meetings
from src.agents.tools.intelligence.list_meetings_by_date import create_list_meetings_by_date

__all__ = [
    "create_list_deal_meetings",
    "create_get_meeting_details",
    "create_get_meeting_summary",
    "create_get_meeting_deal_context",
    "create_get_meeting_transcript",
    "create_get_compressed_transcript",
    "create_list_meetings_by_date",
]
```

**Step 3: Verify import works**

Run: `uv run python -c "from src.agents.tools.intelligence import create_get_compressed_transcript; print('OK')"`

Expected: `OK`

**Step 4: Commit**

```bash
git add src/agents/tools/intelligence/get_compressed_transcript.py src/agents/tools/intelligence/__init__.py
git commit -m "feat(agents): add get_compressed_transcript tool with sub-agent"
```

---

### Task 5: Register the tool and wire into Deal Memory agent

**Files:**

- Modify: `src/agents/tools/registry.py:33-47` (ToolName enum)
- Modify: `src/agents/tools/registry.py:83-91` (TOOL_CATEGORIES)
- Modify: `src/agents/tools/registry.py:377-405` (DealMemoryAgent tools)
- Modify: `src/agents/tools/registry.py:531-537` (intelligence imports)
- Modify: `src/agents/tools/registry.py:652-654` (factory registration)

**Step 1: Add `GET_COMPRESSED_TRANSCRIPT` to `ToolName` enum**

In `src/agents/tools/registry.py`, in the `ToolName` enum under the MEETING TOOLS section (after line 90, `GET_MEETING_TRANSCRIPT`), add:

```python
    GET_COMPRESSED_TRANSCRIPT = "get_compressed_transcript"
```

**Step 2: Add to `TOOL_CATEGORIES`**

After the `GET_MEETING_TRANSCRIPT` entry (around line 441), add:

```python
    ToolName.GET_COMPRESSED_TRANSCRIPT: ToolCategory.INTELLIGENCE,
```

**Step 3: Update `DealMemoryAgent` tool set — replace `GET_MEETING_TRANSCRIPT` with `GET_COMPRESSED_TRANSCRIPT`**

In the `"DealMemoryAgent"` block (lines 381-405), change:

```python
        ToolName.GET_MEETING_TRANSCRIPT,
```

to:

```python
        ToolName.GET_COMPRESSED_TRANSCRIPT,
```

**Step 4: Add import in `_register_tool_factories`**

In the intelligence imports block (around line 531-537), add `create_get_compressed_transcript`:

```python
        from src.agents.tools.intelligence import (
            create_get_compressed_transcript,
            create_get_meeting_deal_context,
            create_get_meeting_details,
            create_get_meeting_summary,
            create_get_meeting_transcript,
            create_list_deal_meetings,
            create_list_meetings_by_date,
        )
```

**Step 5: Register the factory**

After the `GET_MEETING_TRANSCRIPT` factory registration (around line 654), add:

```python
        self._tool_factories[ToolName.GET_COMPRESSED_TRANSCRIPT] = (
            lambda: create_get_compressed_transcript(self.context, self.capabilities_getter)
        )
```

**Step 6: Verify the tool shows up for DealMemoryAgent**

Run: `uv run python -c "from src.agents.tools.registry import ToolRegistry; tools = ToolRegistry.get_available_tools_for_agent('DealMemoryAgent'); print('GET_COMPRESSED_TRANSCRIPT' in {t.value for t in tools}); print('GET_MEETING_TRANSCRIPT' in {t.value for t in tools})"`

Expected:

```
True
False
```

**Step 7: Commit**

```bash
git add src/agents/tools/registry.py
git commit -m "feat(agents): register compressed transcript tool, wire into DealMemoryAgent"
```

---

### Task 6: Add tests

**Files:**

- Create: `tests/agents/sub_agents/__init__.py`
- Create: `tests/agents/sub_agents/test_transcript_compression.py`
- Modify: `tests/workers/deal_worker/test_deal_insight_imports.py`

**Step 1: Create test directory**

Create empty `tests/agents/sub_agents/__init__.py`.

**Step 2: Write unit tests for the compression agent and tool**

Create `tests/agents/sub_agents/test_transcript_compression.py`:

```python
"""Tests for the transcript compression sub-agent.

Run with: uv run pytest tests/agents/sub_agents/test_transcript_compression.py -v
"""

from uuid import uuid4

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
        assert "Alice : Hello" in result
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
        assert config.model == "gpt-5o-mini"
        assert config.timeout_seconds == 60
```

**Step 3: Run all tests**

Run: `uv run pytest tests/agents/sub_agents/test_transcript_compression.py -v`

Expected: All tests PASS.

**Step 4: Run existing import tests to confirm nothing broke**

Run: `uv run pytest tests/workers/deal_worker/test_deal_insight_imports.py -v`

Expected: All existing tests still PASS.

**Step 5: Commit**

```bash
git add tests/agents/sub_agents/
git commit -m "test(agents): add transcript compression sub-agent tests"
```

---

### Task 7: Run linting and type checks

**Step 1: Run ruff**

Run: `uv run ruff check src/agents/sub_agents/ src/agents/tools/intelligence/get_compressed_transcript.py src/agents/tools/registry.py src/infra/config/agent_config.py`

Fix any issues found.

**Step 2: Run ruff on tests**

Run: `uv run ruff check tests/agents/sub_agents/`

Fix any issues found.

**Step 3: Run mypy**

Run: `uv run mypy src/agents/sub_agents/ src/agents/tools/intelligence/get_compressed_transcript.py`

Fix any issues found.

**Step 4: Commit fixes if any**

```bash
git add -A
git commit -m "style: fix lint/type issues in transcript compression"
```

---

### Task 8: Final verification and summary commit

**Step 1: Run all tests together**

Run: `uv run pytest tests/agents/sub_agents/ tests/workers/deal_worker/test_deal_insight_imports.py -v`

Expected: All PASS.

**Step 2: Verify the complete chain works (import check)**

Run:

```bash
uv run python -c "
from src.agents.sub_agents.transcript_compression import TranscriptCompressionAgent
from src.agents.tools.intelligence import create_get_compressed_transcript
from src.agents.tools.registry import AGENT_TOOLS, ToolName
assert ToolName.GET_COMPRESSED_TRANSCRIPT in AGENT_TOOLS['DealMemoryAgent']
assert ToolName.GET_MEETING_TRANSCRIPT not in AGENT_TOOLS['DealMemoryAgent']
print('All checks passed')
"
```

Expected: `All checks passed`
