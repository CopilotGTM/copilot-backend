# General Agent

The **GeneralAgent** is a streaming chat agent designed for presales assistance. It answers user queries by searching internal knowledge bases and the web, then streams responses back as a series of **frames** for real-time UI rendering.

---

## Overview

| Attribute | Value |
|-----------|-------|
| Model | Azure OpenAI `gpt-5` |
| Output Mode | Streaming (SSE frames) |
| Base Class | `BaseAgent` |
| Tools | `InternalKnowledgeBase`, `InternetSearch` |

The agent operates as a conversational assistant for Xeno Organization, helping users with technology solutions, customer assessments, and strategic planning.

---

## Architecture

```
general/
├── agent.py              # Main GeneralAgent class
├── tools.py              # Tool factory functions (search_internal_knowledge, search_internet)
├── prompts/
│   ├── __init__.py
│   └── general_prompt.py # System prompt assembled from common sections
└── README.md             # This file
```

### Shared Components Used

The agent leverages reusable components from `src/agents/common/`:

| Component | Purpose |
|-----------|---------|
| `JsonExtractor` | Extracts JSON from LLM output (sentinel blocks, fenced code, bracket matching) |
| `MetadataParser` | Converts extracted JSON into typed Frame objects |
| `StreamingAnswerBuffer` | Buffers streaming chunks and separates answer content from metadata |
| `use_tool_with_capabilities` | Wraps tool calls with transparent progress tracking |

---

## Frame-Based Streaming

Instead of returning a single response, the agent **yields frames** incrementally. This enables:

1. **Real-time UI updates** — Users see the answer as it's generated
2. **Progress indicators** — UI can show "Searching..." or "Analyzing..." stages
3. **Structured metadata** — Confidence scores, suggestions, and sources arrive as discrete events

### Frame Types Emitted

| Frame | Description |
|-------|-------------|
| `AnswerDelta` | Incremental text chunk of the answer |
| `AnswerMetrics` | Confidence score (`high`, `medium`, `low`) |
| `SuggestionsReplace` | Follow-up question suggestions |
| `SourcesReplace` | Citations and references |
| `QuestionPrompt` | Clarifying question (used sparingly) |
| `AgentSwitch` | Signal to hand off to another agent |
| `Finalize` | End-of-stream marker |

### Streaming Flow

```
┌─────────────────┐
│  run_stream()   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ _run_initial_   │  → Progress frames ("Initializing...", "Analyzing...")
│    setup()      │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ general_agent.  │  → LLM generates response with embedded metadata
│    arun()       │
└────────┬────────┘
         │
         ▼
┌─────────────────────────┐
│ StreamingAnswerBuffer   │  → Separates answer text from ⟦AEP:1⟧ metadata block
│       .stream()         │
└────────┬────────────────┘
         │
         ├── "answer" chunks → yield AnswerDelta(content=...)
         │
         └── "final" with metadata buffer
                    │
                    ▼
         ┌─────────────────┐
         │ MetadataParser  │  → Parses JSON into frames
         │    .parse()     │
         └────────┬────────┘
                  │
                  ├── AnswerMetrics (confidence)
                  ├── SuggestionsReplace (follow-up questions)
                  ├── SourcesReplace (citations)
                  ├── QuestionPrompt (clarifications)
                  └── AgentSwitch (hand-off)
         │
         ▼
┌─────────────────┐
│ _finalize_      │  → yield Finalize(reason="done")
│   stream()      │
└─────────────────┘
```

---

## AEP Metadata Protocol

The LLM appends structured metadata after the answer using the **AEP (Agent Exchange Protocol)** format:

```
<answer content in Markdown>

⟦AEP:1⟧
```json
{
  "version": "aep/1",
  "confidence_score": "high",
  "suggested_questions": ["...", "..."],
  "citations": [{"label": "...", "url": "..."}]
}
```
⟦/AEP⟧
```

The `StreamingAnswerBuffer` detects the `⟦AEP:1⟧` sentinel and splits the stream. The `MetadataParser` then converts the JSON into typed frames.

### Metadata Fields

| Field | Required | Description |
|-------|----------|-------------|
| `version` | Yes | Must be `"aep/1"` |
| `confidence_score` | Yes | `"high"`, `"medium"`, or `"low"` |
| `suggested_questions` | No | Up to 2 follow-up questions |
| `citations` / `sources` | No | References with label, URL, and optional snippet |
| `ask` | No | Clarifying question with fields (used sparingly) |
| `switch_agent` | No | Signal to hand off (e.g., `{"to": "RFP_SHEET", "reason": "..."}`) |

---

## Tools

Tools are created via factory functions that bind the agent's `context` and `capabilities`:

### `search_internal_knowledge`

Searches the organization's internal document store (knowledge base).

```python
async def search_internal_knowledge(query: str, limit: int = 5) -> list[dict]
```

- Uses `InternalKnowledgeBase` tool under the hood
- Returns citations with internal links (`kb://org/{org_id}/doc/{doc_id}#page={page}`)
- **Always preferred first** for company-specific information

### `search_internet`

Searches the public web via DuckDuckGo.

```python
async def search_internet(query: str, max_results: int = 5) -> list[dict]
```

- Uses `InternetSearch` tool
- Returns URLs for external sources
- Used for up-to-date or public information not available internally

### Transparent Tool Usage

Tools are wrapped with `AgentCapabilities` to emit progress frames when tools are invoked:

```python
result = await capabilities.use_tool(
    tool_name="InternalKnowledgeBase",
    tool_func=internal_knowledge_base,
    parameters={...},
    description="Searching internal documents for: ..."
)
```

This allows the UI to show "Searching internal documents for: pricing info" while the tool runs.

---

## Prompt Design

The system prompt (`GENERAL_PROMPT`) is assembled from reusable sections in `src/agents/common/prompt_sections/`:

| Section | Content |
|---------|---------|
| Role definition | Presales expert for Xeno Organization |
| `TOOLING_GUIDANCE` | Instructions for using InternalKnowledgeBase and InternetSearch |
| `AEP_METADATA_GUIDANCE` | Output format rules for the metadata block |

### Key Behavioral Rules

1. **Comprehensive answers** — Cover multiple angles rather than asking clarifying questions
2. **Assume and state** — Make reasonable assumptions and declare them explicitly
3. **Markdown formatting** — Use headers, bold, lists, tables, and code blocks
4. **Minimal clarifications** — Only use `ask` when genuinely blocked
5. **Cite sources** — Always provide references for facts and claims

---

## Usage Example

```python
from src.agents.context import AgentContext
from src.agents.workflows.general.agent import GeneralAgent

context = AgentContext(
    session=db_session,
    organisation_id="org_123",
    user_id="user_456",
)

agent = GeneralAgent(context=context)

async for frame in agent.run_stream(messages):
    if isinstance(frame, AnswerDelta):
        print(frame.content, end="", flush=True)
    elif isinstance(frame, AnswerMetrics):
        print(f"\n[Confidence: {frame.confidence}]")
    elif isinstance(frame, Finalize):
        print("\n--- Done ---")
```

---

## Error Handling

- Exceptions during streaming are logged but do not crash the stream
- The `finally` block ensures `Finalize(reason="done")` is always emitted
- Tool failures return empty results (graceful degradation)

---

## Related Components

- `src/agents/common/` — Shared streaming infrastructure
- `src/agents/tools/transparent_tools.py` — `AgentCapabilities` for tool tracking
- `src/api/schemas/chat_stream_models.py` — Frame type definitions
- `src/agents/base.py` — `BaseAgent` abstract class
