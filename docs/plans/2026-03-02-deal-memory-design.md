# Deal Memory — Design Document

**Date:** 2026-03-02
**Status:** Approved

## Problem

DealSummaryAgent, RiskBlockerExtractionAgent, and StakeholderAnalysisAgent perform poorly because they start from scratch every invocation. Each agent independently calls 5-8 tools, reads raw transcripts/emails/notes, and tries to synthesize a deal's full context in a single pass. There is no accumulated understanding — every run is amnesic.

## Solution

Create a persistent **Deal Memory** — a single markdown document per deal that serves as the accumulated, evolving knowledge base. Built incrementally as new meetings, emails, and notes arrive. All downstream agents read this memory as pre-built context instead of discovering the deal from scratch.

## Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Format | Markdown (TEXT) | Token-efficient, LLM-preferred (outperforms JSON on 5/7 benchmarks), human-readable. Industry trend (Letta evolved from structured blocks to markdown). |
| Storage | PostgreSQL TEXT column | Fast reads, transactional, no new infrastructure. Blob archival for closed deals later. |
| Template | Fixed structure, LLM-filled content | Predictable sections for consuming agents. LLM creativity goes into content, not structure. |
| Update mode | Incremental | Agent reads existing memory + new data, updates affected sections only. Full regen not needed. |
| Agent type | Single-call (not tool-calling) | Service pre-fetches data, passes to LLM. One call, deterministic, cheaper. |
| Source tracking | None (deferred) | Trigger events carry what's new. Timestamp comparison handles gap recovery. Add provenance table later if needed. |
| Versioning/history | Deferred | `version` column for optimistic locking. Version history table added later if needed. |
| Graph layer | Deferred | Markdown-first. Graph added when cross-deal queries or temporal fact tracking become requirements. Markdown is the natural input to graph extraction — no migration lock-in. |
| Glossary/classification rules | Agent system prompt | Same for every deal — not deal-specific data. Keeps memory document lean. |

## Table Design

### `deal_memories` (org schema)

| Column | Type | Notes |
|--------|------|-------|
| `memory_id` | UUID | PK, default uuid4 |
| `organisation_id` | UUID | NOT NULL, indexed |
| `deal_id` | UUID | FK -> crm_deals, UNIQUE per org |
| `content` | TEXT | The full markdown document |
| `version` | INT | NOT NULL, default 1, incremented on each update |
| `token_count` | INT | Nullable, for agent context budgeting |
| `created_at` | TIMESTAMP | Via TimestampMixin |
| `updated_at` | TIMESTAMP | Via TimestampMixin |

**Indexes:**
- `ix_deal_memories_org_deal (organisation_id, deal_id)` — UNIQUE
- `ix_deal_memories_org (organisation_id)` — for backfill queries

### `DealJobType` Addition

```python
DEAL_MEMORY_BUILD = "deal_memory_build"
```

## Memory Template

Fixed structure, 12 sections. The LLM fills content within each section but cannot omit or rename sections.

```markdown
# DEAL MEMORY — {{Deal Name}}

**Deal ID:** {{UUID}}
**Prospect:** {{Company Name}}
**Website:** {{URL}}
**LinkedIn:** {{URL}}
**Last Updated:** {{ISO Timestamp}}
**Deal Owner:** {{Rep Name}}
**Stage:** {{CRM Stage}}
**Close Date:** {{Close Date}}
**Commit Category:** {{Category}}
**Competitors:** {{List}}

---

## 1. Executive Snapshot
5-8 sentences: current deal state, momentum direction.

## 2. Commercial State
- Deal Size:
- Stage:
- Close Date:
- Close Date Changes (Last 30 Days):
- Days in Current Stage:

## 3. Deal Business Aspect
5-8 sentences: why this deal exists, business outcome,
how it helps the prospect.

## 4. Technical Aspects
5-8 sentences: technical landscape, requirements,
integration points.

## 5. Pain Points
Detailed list of business problems the client needs solved.

## 6. Buying Dynamics

### 6.1 Stakeholder Map
- Identified Stakeholders (name, role, motivation, pain points)
- Active Stakeholders (Last 30 Days):
- Economic Buyer Identified: (Yes/No)
- Economic Buyer: {{Name, Email}}
- Champion Identified: (Yes/No)
- Champion: {{Name}}
- Champion Strength:
- Ownership Language Trend: (Strengthening / Stable / Weakening)
- Evidence:
- Additional Comments:

### 6.2 Multi-Threading Assessment
- Threading Status: (Single / Partial / Multi)
- Evidence:

## 7. Engagement Trajectory

### 7.1 Meeting Activity
- Meetings Last 30 Days:
- Meetings Prior 30 Days:
- Meeting Trend: (Increasing / Stable / Decreasing)

### 7.2 Response Behavior
- Average Response Time Trend:
- Engagement Direction:

### 7.3 Buyer Participation
- Buyer Talk Ratio Trend:
- Buyer Question Density Trend:

## 8. Contextual Signals

### 8.1 Urgency Trajectory
- Current Urgency Level: (High / Medium / Low)
- Change vs Prior Interaction:
- Evidence:

### 8.2 Decision Clarity
- Decision Process Clearly Articulated: (Yes / Partial / No)
- Evidence:

### 8.3 Champion Strength
- Ownership Language Trend: (Strengthening / Stable / Weakening)
- Evidence:

### 8.4 Budget Certainty
- Budget Confidence Level: (Confirmed / Probable / Unclear / At Risk)
- Evidence:

### 8.5 Competitive Pressure
- Competitors Mentioned:
- Competitive Intensity Trend: (Increasing / Stable / Decreasing)

## 9. Execution Clarity
- Mutual Next Step Defined: (Yes/No)
- Next Meeting Scheduled: (Yes/No)
- Timeline Confirmed: (Yes/No)

## 10. Active Risks
### Risk: {{Risk Name}}
- Severity: (Low / Medium / High)
- First Detected:
- Why This Risk Exists:
- Evidence:
- What Would Reduce This Risk:
- Status: (Active / Resolved)
- User Approved: (Yes / No)

## 11. Confidence Assessment
- Overall Confidence in Close: (High / Medium / Low)
- Explanation:

## 12. Change Log
- Bullet list of changes since last update
- "No material change detected since last update."
```

The update agent MUST validate structural completeness before returning. No section may be omitted. If evidence is insufficient for a section, state "Insufficient evidence" rather than omitting.

## Architecture

### Two Entry Points

**1. Backfill Scheduler (hourly)**
```
Runs every hour:
1. Query: open deals WHERE deal_id NOT IN (SELECT deal_id FROM deal_memories)
2. For each deal without memory:
   a. Check if processed meetings OR emails OR notes exist
   b. If none exist -> skip
   c. If data exists -> create DealJob(DEAL_MEMORY_BUILD, is_initial_build=true)
```

**2. Event Triggers (real-time)**
```
When these events fire for a deal:
- Meeting transcript processed (status = READY)
- Email synced and linked to deal
- Deal note added

Action: Create DealJob(DEAL_MEMORY_BUILD, trigger_type, source_id)
```

### Build/Update Flow

```
DealJob created
    |
    v
DealMemoryService.build_or_update(deal_id, trigger_info)
    |
    v
Phase 1: Load deal basics (fresh session)
    - CRM deal fields (name, amount, stage, close date, owner)
    - Read existing memory if any
    - Release session
    |
    v
Phase 2: Pre-compute metrics (fresh session)
    - meeting_count_30d, meeting_count_prior_30d
    - days_in_current_stage
    - close_date_change_count_30d
    - active_stakeholder_count_30d
    - Release session
    |
    v
Phase 3: Load source data (fresh session)
    Initial build:
      - All meeting summaries (chronological)
      - All emails (chronological)
      - All notes
      - All stakeholders
      - All existing risks/blockers
    Incremental:
      - Just the new source data (meeting/email/note)
    - Release session
    |
    v
Phase 4: Call LLM (NO session held)
    Input:
      - Deal basics + metrics
      - Existing memory (or empty for initial)
      - Source data
      - Template structure + instructions
    Output:
      - Complete markdown document
    |
    v
Phase 5: Save (fresh session)
    - UPSERT deal_memories (version += 1)
    - Compute and store token_count
    - Mark DealJob COMPLETED
```

### Agent Design

**DealMemoryAgent** — Single-call, receives all context upfront.

- System prompt contains:
  - Template structure with section descriptions
  - Classification rules (glossary: Economic Buyer, Champion, Decision Clarity, etc.)
  - Instructions for initial build vs incremental update
  - Rule: never omit sections, use "Insufficient evidence" when data is sparse

- For initial builds: "Build a complete deal memory from the following data."
- For incremental updates: "Update the existing deal memory with the new [meeting/email/note]. Update only affected sections. Add an entry to the Change Log."

### How Downstream Agents Consume Memory

```
BEFORE:                              AFTER:
DealSummaryAgent                     DealSummaryAgent
  -> calls 5-8 tools                  -> reads deal_memories.content
  -> reads transcripts                 -> has full context
  -> reads emails                      -> refines/generates summary
  -> synthesizes from scratch

RiskBlockerAgent                     RiskBlockerAgent
  -> calls 5-8 tools                  -> reads deal_memories.content
  -> reads transcripts                 -> sections 8, 10 have signals
  -> extracts from scratch             -> focused extraction

StakeholderAgent                     StakeholderAgent
  -> receives raw context string       -> reads deal_memories.content
  -> analyzes from scratch             -> section 6 has stakeholder map
                                       -> updates/validates

BriefingAgent                        BriefingAgent
  -> calls tools for context           -> reads deal_memories.content
  -> builds briefing                   -> has everything for prep
```

### Concurrency Handling

The `version` column enables optimistic locking:
- Read current version before update
- On save: `UPDATE ... SET version = version + 1 WHERE version = expected_version`
- If another update raced ahead, retry with the latest version

### Celery Worker

Runs in `deal_worker` alongside existing deal tasks.

## Future Extensions (Not in Scope)

- **Version history table** — store previous memory versions for audit/rollback
- **Source provenance table** — track which sources contributed to which sections
- **Blob archival** — move memories for closed/won/lost deals to Azure Blob Storage
- **Knowledge graph layer** — extract entities and relationships from memories for cross-deal queries
- **Staleness detection** — compare memory.updated_at against latest source timestamps, auto-trigger rebuild
