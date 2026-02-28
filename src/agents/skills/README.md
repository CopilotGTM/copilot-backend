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
| `stakeholder-mapping` | Stakeholder power mapping and coverage gap detection | Hardcoded stakeholder analysis guidance |
| `pipeline-review` | Forecast-integrity pipeline intelligence and risk prioritization | Hardcoded pipeline review guidance |
| `reminders` | Create, list, cancel reminders and agentic alerts | Hardcoded reminder type/recurrence instructions |

---

## Proposed Skills (Roadmap)

### 1. Close Plan / Deal Acceleration

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

### 2. Visualization Guide

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
