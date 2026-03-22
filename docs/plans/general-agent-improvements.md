# General Agent Improvement Plan

Audit of the GeneralAgent and all Agno-based agents, with concrete improvements ranked by impact.

---

## 1. Token/Cost Tracking (HIGH IMPACT)

**Problem:** No tracking of input/output tokens per conversation turn. Can't measure cost, detect runaway conversations, or set budget limits.

**What to do:**
- Extract token usage from Agno's response object after each `arun()` call (OpenAI API returns `usage.prompt_tokens`, `usage.completion_tokens`, `usage.total_tokens`)
- Log it per-turn with org_id, agent_name, deal_id, message_id
- Emit an `AnswerMetrics` frame (or new `TokenUsage` frame) so the frontend/caller can see cost
- Store cumulative usage per org for future billing/budget controls

**Where to change:**
- `src/agents/workflows/general/agent.py` — extract from Agno stream response
- `src/agents/base.py` — add shared token extraction helper
- `src/infra/messaging/chat_stream_models.py` — add `TokenUsage` frame if needed
- All other agents that use `arun()` — apply same pattern

**Risks:** Agno's streaming response may not expose token counts mid-stream; may only be available after stream completes. Need to verify Agno's API surface.

---

## 2. Reduce Tool Count for GeneralAgent (HIGH IMPACT)

**Problem:** GeneralAgent has 50+ tools registered. CLAUDE.md says "too many available tools lowers call reliability." The model wastes decision capacity choosing from a massive menu.

**What to do:**
- Split tools into **always-loaded core** (~15) and **on-demand** (loaded via skills or context):
  - **Core (always):** search_internal_knowledge, search_internet, search_deals, list_deals, get_deal_details, get_deal_summary, get_deal_memory, list_deal_stakeholders, list_deal_meetings, get_meeting_summary, list_deal_emails, get_email_details, list_deal_notes, search_contacts
  - **On-demand (inject when relevant):** Research tools (Wikipedia, arXiv, stocks, financials, crawl_webpage, extract_news_article), LinkedIn tools, visualization card, CRM write tools, reminder tools, task tools
- Use conversation context or skill loading to inject on-demand tools dynamically

**Where to change:**
- `src/agents/tools/registry.py` — split `AGENT_TOOLS["GeneralAgent"]` into core + extended sets
- `src/agents/workflows/general/agent.py` — support dynamic tool injection
- Prompt may need minor update to tell the model "ask for additional tools if needed"

**Risks:** Some edge cases where the model needed a tool but it wasn't loaded. Mitigate with good skill-to-tool mapping.

---

## 3. Pre-Inject Deal Context (HIGH IMPACT)

**Problem:** When `context.deal_id` is set, the agent still manually calls `get_deal_details` every turn to know what deal it's working with. Wastes a tool call and adds latency.

**What to do:**
- If `deal_id` is present in context, pre-fetch a lightweight deal snapshot (name, stage, amount, company, key stakeholders, last activity date)
- Inject it as a system message or additional context block before the conversation
- The agent starts informed instead of blind

**Where to change:**
- `src/agents/workflows/general/agent.py` — add deal context pre-fetch in `__init__` or `run_stream`
- May need a lightweight `get_deal_snapshot()` service method that returns minimal data without full joins

**Risks:** Adds ~100-200ms to agent startup. Worth it if it saves a tool call (which takes longer). Don't pre-fetch for conversations without a deal_id.

---

## 4. Shorten the System Prompt (MEDIUM IMPACT)

**Problem:** The prompt is ~240 lines covering identity, methodology, skills routing, tool routing, formatting, metadata spec, and task tracking. Competing instructions dilute focus.

**What to do:**
- Move `## Output Format` (AEP metadata spec) to post-processing — parse/inject defaults programmatically instead of relying on the model
- Move `## Agent Task Tracking` to a skill — only inject for multi-step requests
- Move `## Response Formatting` to a shorter "format naturally with markdown" instruction
- Target: core prompt under ~100 lines focused on identity, principles, tool routing, sales mindset

**Where to change:**
- `src/agents/workflows/general/prompts/general_prompt.py`
- `src/agents/common/metadata_parser.py` — enhance to handle missing/malformed metadata gracefully
- `src/agents/common/agent_tasks_extractor.py` — potentially move to skill-triggered only

**Risks:** Model may produce less consistent metadata without explicit instructions. Mitigate with robust post-processing defaults.

---

## 5. Error Frames for Silent Failures (MEDIUM IMPACT)

**Problem:** In `agent.py:113`, exceptions are logged but the user sees nothing — the stream just ends silently.

**What to do:**
- Yield an error frame (e.g., `ErrorFrame(message="Something went wrong, please try again")`) when the agent encounters an unrecoverable error
- Differentiate between retryable errors (timeout, rate limit) and permanent errors (invalid context)
- For retryable errors, attempt retry before surfacing to user

**Where to change:**
- `src/agents/workflows/general/agent.py` — `run_stream()` exception handler
- `src/infra/messaging/chat_stream_models.py` — add `ErrorFrame` if it doesn't exist
- Apply same pattern to all other agents

**Risks:** Minimal. Strictly better than silent failure.

---

## 6. Tool Result Caching Within a Conversation (MEDIUM IMPACT)

**Problem:** If the agent calls `get_deal_details("acme-id")` twice in the same conversation, it hits the DB both times. Common when the model re-gathers context.

**What to do:**
- Add a per-conversation tool result cache keyed by `(tool_name, frozen_args)`
- Cache read-only tools for the duration of the stream
- Skip cache for write tools (create_deal, add_note, etc.)
- Add TTL or invalidation after write operations

**Where to change:**
- `src/agents/tools/transparent_tools.py` or new `src/agents/common/tool_cache.py`
- `src/agents/common/tool_wrappers.py` — wrap `use_tool_with_capabilities` with cache check

**Risks:** Stale data if external state changes mid-conversation. Acceptable for single-turn caching; just don't cache across turns.

---

## 7. Conversation Context Compression (MEDIUM IMPACT)

**Problem:** Full `message_list` is sent to the model every turn. For long conversations, this wastes tokens and can hit context limits.

**What to do:**
- Implement a sliding window: keep last N messages verbatim, summarize older messages
- Or use a "conversation memory" approach where a compressed summary of earlier context is prepended
- Set a token budget for conversation history (e.g., 8K tokens max for history)

**Where to change:**
- `src/agents/utils.py` — `prepare_messages()` is the right place to add windowing
- May need a lightweight summarization step (could use a fast model like gpt-4o-mini)

**Risks:** Information loss from summarization. Mitigate by always keeping the most recent messages verbatim and including key entities (deal names, stakeholder names) in summaries.

---

## 8. Fix `description` vs `instructions` Field (LOW IMPACT, QUICK FIX)

**Problem:** The 240-line system prompt is passed as `description` in the Agno constructor. Agno's `description` is meant for a short agent description; `instructions` or `system_message` is for the system prompt.

**What to do:**
- Move the system prompt from `description=system_prompt` to `instructions=system_prompt` (or `system_message`)
- Set `description` to a short string like "Kai - Sales intelligence assistant"

**Where to change:**
- `src/agents/workflows/general/agent.py` line 80

**Risks:** Minimal. Need to verify Agno places `instructions` in the correct position in the message array.

---

## 9. Output Validation / Guardrails (LOW IMPACT)

**Problem:** No validation that the agent's output contains the required `⟦AEP:1⟧` metadata block or that confidence scores are valid.

**What to do:**
- Add post-processing that injects default metadata if the model omits it
- Validate `confidence_score` is one of `high|medium|low|neutral`
- Ensure `suggested_questions` are actually questions (end with `?`)

**Where to change:**
- `src/agents/common/metadata_parser.py` — add validation + defaults
- `src/agents/workflows/general/agent.py` — `_enqueue_final_metadata` fallback

**Risks:** None. Strictly additive.

---

## 10. Per-Tool Timeouts (LOW IMPACT)

**Problem:** Individual tool calls have no timeout. A hanging pgvector query blocks the entire stream.

**What to do:**
- Wrap each tool call in `asyncio.wait_for(tool_func(...), timeout=N)`
- Default timeout: 30s for DB tools, 15s for web search, 60s for LLM-backed tools
- Return fallback value on timeout and log warning

**Where to change:**
- `src/agents/common/tool_wrappers.py` — `use_tool_with_capabilities()`
- Add configurable timeout per tool category

**Risks:** False timeouts on legitimately slow operations. Set generous defaults and make configurable.

---

## 11. Better Internet Search Provider (LOW IMPACT, OPERATIONAL)

**Problem:** DuckDuckGo has rate limits, inconsistent results, and no API key auth. Fragile for a B2B SaaS product.

**What to do:**
- Evaluate: Brave Search API, Serper, Tavily
- Swap the implementation behind `search_internet` tool
- Keep DuckDuckGo as fallback

**Where to change:**
- `src/agents/tools/search.py` — `internet_search` implementation

**Risks:** API cost. But reliability gain is worth it for a production product.

---

## Implementation Priority

| Priority | Item | Effort | Impact |
|----------|------|--------|--------|
| P0 | 1. Token/Cost Tracking | Medium | High — enables cost visibility |
| P0 | 5. Error Frames | Small | High — fixes silent UX failures |
| P0 | 8. Fix description vs instructions | Tiny | Medium — correct API usage |
| P1 | 3. Pre-Inject Deal Context | Medium | High — saves a tool call per turn |
| P1 | 2. Reduce Tool Count | Large | High — improves tool selection accuracy |
| P1 | 6. Tool Result Caching | Medium | Medium — reduces latency and cost |
| P2 | 4. Shorten System Prompt | Medium | Medium — better instruction following |
| P2 | 10. Per-Tool Timeouts | Small | Medium — prevents hanging |
| P2 | 9. Output Validation | Small | Low — consistency improvement |
| P3 | 7. Context Compression | Large | Medium — only matters for long chats |
| P3 | 11. Better Search Provider | Small | Low — operational improvement |
