# Skills Architecture

This directory contains Agno skill packages for CopilotGTM agents.

## Package contract

Each skill package should contain:

- `SKILL.md` with YAML frontmatter (`name`, `description`)
- `references/` for detailed guidance loaded on demand
- optional `scripts/` only when deterministic helper code is required

## Skill authoring playbook

### 1) What belongs in `SKILL.md`

Keep `SKILL.md` short and operational:

- Trigger description in frontmatter (`name`, `description`)
- Workflow steps for how to solve the task
- Data-first evidence contract (what information must be collected)
- Output contract (what sections must appear in the answer)
- Reference loading guide (`Use references/x.md for ...`)

Do not put long background theory in `SKILL.md`. Move depth to `references/`.

### 2) How to mention tools

Default pattern:

- Describe required data first (for example: stakeholder coverage, timeline shifts, buyer authority signals)
- Add tool names only as optional hints in brackets

Example:

- Stakeholder coverage and influence signals `[ListDealStakeholders, GetStakeholderDetails]`

Rule:

- Bracket tool hints are examples, not hard constraints.
- Do not over-constrain the model to one fixed tool path unless absolutely required.

### 3) How to structure and mention references

Reference files should be capability-specific, not generic:

- `scoring-rubric.md`
- `evidence-quality-rules.md`
- `discovery-question-bank.md`
- `output-guidelines.md`

Inside `SKILL.md`, mention references with explicit usage intent:

- `Use references/scoring-rubric.md for scoring calibration.`
- `Use references/evidence-quality-rules.md for contradiction handling.`

Keep references one level deep from `SKILL.md`. Avoid nested reference trees.

### 4) Naming guideline

- Skill folder: lowercase + hyphen (example: `medpic-assessment`)
- Reference files: short kebab-case names ending in `.md`
- Prefer intent-based names (`output-guidelines.md`) over vague names (`notes.md`)

---

## Current Skills

| Skill | Description | Replaces |
|-------|-------------|----------|
| `medpic-assessment` | MEDPIC qualification scoring with evidence-based framework | Hardcoded MEDDIC playbook |
| `deal-health-check` | Traffic-light deal health across 5 dimensions | Hardcoded Deal Health Check playbook |
| `call-analysis` | Call/meeting analysis — rep performance, signals, momentum | Hardcoded Call/Meeting Analysis playbook |
| `competitive-analysis` | Competitor comparison using KB, deal signals, and external context | Hardcoded competitive positioning guidance |
| `reminders` | Create, list, cancel reminders and agentic alerts | Hardcoded reminder type/recurrence instructions |

---

## Proposed Skills (Roadmap)

### 1. Stakeholder Mapping

**Trigger phrases**: "Map the stakeholders", "Who are the key players?", "Show me the org chart", "Who's the decision maker?"

**Why**: Currently loose guidance in "Sales Intelligence Mindset" about classifying by Influence/Role/Engagement, but not a structured workflow. A skill standardizes stakeholder analysis with coverage gap detection and power mapping.

**Workflow**:
1. Pull all deal stakeholders with details and insights.
2. Classify each: Role (Economic Buyer, Technical Evaluator, End User, Coach, Blocker), Influence (High/Medium/Low), Engagement (Active/Passive/Disengaged).
3. Identify coverage gaps — missing EB? No champion? No technical evaluator?
4. Pull recent interactions per stakeholder to assess engagement recency.
5. Produce power map table + gap analysis + recommended engagement actions.

**References**:
- `classification-rubric.md` — role/influence/engagement definitions with examples
- `coverage-gaps-guide.md` — what gaps look like and why they matter
- `engagement-scoring.md` — how to rate engagement from activity data

**Value**: High — stakeholder questions are very common in sales, and structured classification improves deal strategy.

---

### 2. Pipeline Review

**Trigger phrases**: "How's my pipeline?", "What deals need attention?", "Pipeline risk report", "Show me stale deals"

**Why**: Cross-deal analysis is fundamentally different from single-deal tools. Without a skill, the model either goes too shallow (just listing deals) or too deep (analyzing each deal fully). A skill balances breadth vs depth.

**Workflow**:
1. List all active deals with stage, amount, close date, owner, last activity.
2. Flag stale deals (no activity in 7+ days).
3. Identify stage distribution and bottlenecks.
4. Surface top 3-5 risks across the portfolio.
5. Produce pipeline summary table + risk highlights + recommended actions.

**References**:
- `staleness-thresholds.md` — what counts as stale by stage
- `pipeline-health-signals.md` — stage distribution, velocity, coverage ratio

**Value**: Medium-High — useful for managers and reps doing weekly reviews.

---

### 3. Close Plan / Deal Acceleration

**Trigger phrases**: "What do we need to close this by March?", "Create a close plan", "Accelerate this deal"

**Why**: Works backward from a target close date to identify remaining steps, gaps, and blockers. Combines deal health, stakeholder coverage, paper process status, and timeline into an action-oriented plan.

**Workflow**:
1. Get deal details, stage, and target close date.
2. Identify remaining stages and typical duration for each.
3. Map stakeholder readiness for each remaining gate.
4. Check paper process status — procurement, legal, security review.
5. Produce timeline with milestones, owners, and risk flags.

**References**:
- `stage-duration-benchmarks.md` — typical time per stage
- `close-checklist.md` — common pre-close requirements

**Value**: Medium — valuable when asked, but may not be frequent enough to prioritize early.

---

### 4. Visualization Guide

**Trigger phrases**: "Show me a chart", "Visualize this data", "Create a graph of..."

**Why**: Currently 6 lines in the prompt for chart type selection. A skill provides richer guidance on data preparation, chart selection, and formatting — loaded only when the model creates a visualization.

**Workflow**:
1. Assess data shape (categories, time series, part-to-whole, ranking).
2. Select appropriate chart type.
3. Prepare data in the required format.
4. Call `CreateVisualizationCard` with correct parameters.

**References**:
- `chart-selection-guide.md` — decision tree for chart type by data shape
- `data-preparation.md` — formatting requirements per chart type

**Value**: Low-Medium — only worth building if visualization quality is inconsistent.

---

## Security contract

Skills must not bypass tool-level org/deal boundaries.
All data access remains through existing tools using `AgentContext`.

## Quality contract

- Evidence-first scoring
- Explicit `Unknown` when evidence is insufficient
- Output language should follow user language
- Do not expose UUID/internal IDs unless user explicitly asks
- Diagrams only when they add decision clarity
