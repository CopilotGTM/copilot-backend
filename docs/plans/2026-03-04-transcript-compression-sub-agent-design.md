# Transcript Compression Sub-Agent Design

**Date:** 2026-03-04
**Status:** Approved

## Problem

The Deal Memory agent fetches raw meeting transcripts (10-20 pages each) via tool calls. With multiple meetings, the accumulated transcript data drowns out the system prompt and expected output format, causing the LLM to ignore structural requirements — headings change, sections get dropped.

## Solution

A lightweight compression sub-agent that runs inside a new tool (`get_compressed_transcript`). The Deal Memory agent calls this tool instead of `get_meeting_transcript`. The sub-agent compresses each transcript in its own context window, returning speaker-attributed key points (~1-2 pages per meeting). The raw transcript never enters the Deal Memory agent's context.

## Architecture

```
Deal Memory Agent (gpt-5.2)
  ├─ calls list_deal_meetings()
  ├─ calls get_compressed_transcript(event_1)  ──► TranscriptCompressionAgent (gpt-5o-mini)
  ├─ calls get_compressed_transcript(event_2)  ──► TranscriptCompressionAgent (gpt-5o-mini)
  ├─ calls list_deal_emails()
  ├─ calls list_deal_notes()
  └─ generates .md output
```

Each `get_compressed_transcript` call:
1. Fetches transcript segments from DB (speaker + content + timestamps)
2. Releases DB session
3. Spins up a TranscriptCompressionAgent (gpt-5o-mini, no tools)
4. Returns condensed markdown string to parent agent

## File Structure

```
src/agents/
├── sub_agents/                                    # NEW directory
│   ├── __init__.py
│   └── transcript_compression/
│       ├── __init__.py                            # exports TranscriptCompressionAgent
│       ├── agent.py                               # TranscriptCompressionAgent class
│       └── prompts.py                             # compression system prompt
├── tools/
│   └── intelligence/
│       ├── get_meeting_transcript.py              # UNCHANGED
│       └── get_compressed_transcript.py           # NEW tool wrapper
└── tools/registry.py                              # add GET_COMPRESSED_TRANSCRIPT

src/infra/config/agent_config.py                   # add transcript_compression_agent config
```

## Components

### 1. TranscriptCompressionAgent (`sub_agents/transcript_compression/agent.py`)

- Model: gpt-5o-mini (configurable via env var)
- No tools — receives transcript text, returns compressed text
- No AgentContext — doesn't access DB; the tool handles DB access
- Stateless — created per tool call
- Single `arun()` call — no agentic loop

### 2. Compression Prompt (`sub_agents/transcript_compression/prompts.py`)

Output format — speaker-attributed key points:
- **Key Dialogue:** `Speaker Name (Role if known): what they said`
- **Decisions:** what was decided
- **Action Items:** who committed to what
- **Unresolved:** open questions or disagreements

Rules:
- Keep substantive claims, decisions, commitments, concerns, questions
- Remove filler, pleasantries, repetition, off-topic tangents
- Target ~1500 tokens max output per meeting

### 3. Tool Wrapper (`tools/intelligence/get_compressed_transcript.py`)

Factory pattern matching existing tools: `create_get_compressed_transcript(context, capabilities_getter)`.

Steps:
1. Fetch Meeting + transcript_segments via `context.acquire_session()`
2. Format segments: `"Speaker Name [HH:MM:SS]: content"`
3. Release DB session before LLM call
4. Create TranscriptCompressionAgent, call `compress()`
5. Return dict with `event_id`, `title`, `meeting_date`, `compressed_transcript`

### 4. Registry Changes (`tools/registry.py`)

- Add `GET_COMPRESSED_TRANSCRIPT` to ToolName enum
- Add factory in `_register_tool_factories`
- DealMemoryAgent tool set: replace `GET_MEETING_TRANSCRIPT` with `GET_COMPRESSED_TRANSCRIPT`
- Keep `GET_MEETING_SUMMARY` available

### 5. Agent Config (`agent_config.py`)

```
AGENT_TRANSCRIPT_COMPRESSION_MODEL=gpt-5o-mini (default)
AGENT_TRANSCRIPT_COMPRESSION_TIMEOUT=60 (default)
```

## What Does NOT Change

- Deal Memory agent code (`agent.py`, `prompts.py`) — unchanged
- Existing `get_meeting_transcript` tool — unchanged, still available for other agents
- Other agents' tool sets — unchanged
- Worker service code — unchanged

## Expected Impact

- ~90% reduction in transcript tokens entering Deal Memory agent context
- Expected output format stays dominant in context
- Per-meeting compression adds ~5-10s latency (acceptable within 300s timeout)
- Cost: gpt-5o-mini calls are ~10x cheaper than gpt-5.2
