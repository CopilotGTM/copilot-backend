# Skill Authoring Playbook

A guide for developers writing Agno agent skills for the CopilotGTM GeneralAgent.

---

## Why This Structure Works (Research Backing)

The multi-file skill architecture (SKILL.md + references/) is not an arbitrary convention. It is backed by Anthropic's official guidance, independent research, and convergent industry practice.

### The core principle: progressive disclosure

Load only what the model needs, when it needs it. SKILL.md provides the workflow; reference files load on-demand during specific phases (scoring, evidence evaluation, output formatting).

### Evidence supporting multi-file over single-file

| Source | Finding |
|--------|---------|
| Anthropic official guide (32-page PDF) | "Keep SKILL.md under 500 lines. Move detailed documentation to references/." Three-level progressive disclosure: frontmatter (~100 tokens) -> SKILL.md body (<5,000 tokens) -> references (on-demand). |
| Chroma Research — Context Rot Study (18 models tested) | Performance instability at 2,500+ words. Hallucinations increase at 5,000+. Even single distractors reduce accuracy relative to baseline. |
| arXiv — Codified Context Infrastructure (Feb 2026) | Single-file manifests "do not scale beyond modest codebases." Three-tier architecture (hot/specialist/cold memory) associated with 29% runtime reduction and 17% token savings. |
| Softcery — Prompt Engineering Diminishing Returns | Monolithic prompt at 74% accuracy reached 90% after decomposition into specialized modules. |
| Stanford Study (cited across multiple sources) | 15-20% accuracy drop when loading ~4,000 tokens of context upfront vs. selective retrieval. |
| OpenAI Codex | Uses identical structure: SKILL.md + references/ + scripts/. |
| Cursor, Windsurf, Cline | All migrated from single-file rules to modular multi-file directory structures. |

### Why scoring rubrics and evidence rules belong in separate files

| Source | Finding |
|--------|---------|
| AutoSCORE framework (arXiv) | Separating evidence extraction from score assignment improves both accuracy and interpretability. |
| Anthropic — Context Engineering blog | "Good context engineering means finding the smallest possible set of high-signal tokens." Loading rubrics only during scoring reduces noise during other phases. |
| Chroma — Context Rot Study | Irrelevant information in context actively degrades performance. A scoring rubric loaded during evidence-gathering is noise. |

### Recommended size thresholds

| Element | Max recommended | Source |
|---------|----------------|--------|
| SKILL.md body | 200 lines / 3,000 words / 4,000 tokens | Anthropic best practices + Chroma research |
| `description` in frontmatter | 1,024 characters | Anthropic docs — injected every turn as routing metadata |
| Individual reference file | 100 lines | Progressive disclosure principle — split if longer |
| Total skill (SKILL.md + all references) | No hard limit | References load on-demand, so total size matters less than per-file size |

### When a single SKILL.md is sufficient

A single file works when the skill is **under ~100 lines** and purely procedural (no scoring rubrics, no evidence evaluation, no complex output formatting). The `reminders` skill is a good example — it classifies, extracts parameters, and calls a tool. No rubric needed.

### Sources

- [Anthropic — The Complete Guide to Building Skills for Claude (PDF)](https://resources.anthropic.com/hubfs/The-Complete-Guide-to-Building-Skill-for-Claude.pdf)
- [Anthropic — Skill Authoring Best Practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)
- [Anthropic — Effective Context Engineering for AI Agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Anthropic — Extend Claude with Skills](https://code.claude.com/docs/en/skills)
- [Chroma — Context Rot: How Increasing Input Tokens Impacts LLM Performance](https://research.trychroma.com/context-rot)
- [arXiv — Codified Context: Infrastructure for AI Agents in a Complex Codebase](https://arxiv.org/html/2602.20478)
- [arXiv — AutoSCORE: Multi-Agent LLM Structures for Scoring](https://arxiv.org/html/2509.21910v1)
- [Softcery — The AI Agent Prompt Engineering Trap: Diminishing Returns](https://softcery.com/lab/the-ai-agent-prompt-engineering-trap-diminishing-returns-and-real-solutions)
- [Particula.tech — Optimal Prompt Length Before AI Performance Degrades](https://particula.tech/blog/optimal-prompt-length-ai-performance)
- [OpenAI — Codex Agent Skills](https://developers.openai.com/codex/skills/)

---

## 1. When to Create a Skill

Create a skill when **all three** conditions are true:

1. **Repeatable workflow** — the task follows a structured sequence that the model should execute consistently every time (e.g., "assess MEDPIC", "check deal health").
2. **Quality gap without guidance** — without explicit instructions, the model produces inconsistent, shallow, or incorrect output for this task.
3. **Enough depth to justify loading cost** — the workflow has scoring rubrics, evidence rules, or domain-specific logic that would bloat the system prompt if always loaded.

**Do NOT create a skill when:**

- The task is a one-off question answerable from deal data alone.
- The model already handles it well without special instructions.
- The guidance is short enough (< 10 lines) to live in the system prompt directly.

---

## 2. Skill Anatomy

Every skill is a folder under `src/agents/skills/`:

```
my-skill/
├── SKILL.md              # Main instructions (< 200 lines ideally)
├── references/
│   ├── scoring-rubric.md        # Rating scale and calibration
│   ├── evidence-quality-rules.md # What counts as admissible evidence
│   └── output-guidelines.md     # Formatting, language, diagram rules
└── scripts/                     # Optional — deterministic helper code
```

### Naming conventions

| Element | Convention | Example |
|---------|-----------|---------|
| Skill folder | lowercase + hyphen | `deal-health-check` |
| Reference files | short kebab-case `.md` | `scoring-rubric.md` |
| File names | intent-based, not vague | `evidence-quality-rules.md` not `notes.md` |

---

## 3. Writing the SKILL.md

The SKILL.md has two parts: YAML frontmatter and markdown body.

### 3.1 Frontmatter

```yaml
---
name: my-skill-name
description: One to two sentences describing what this skill does and when to trigger it. Include specific trigger phrases the user might say.
---
```

**Rules for `description`:**

- **Be specific about triggers.** List the actual phrases users say: "MEDPIC analysis", "deal health", "qualification scoring". Vague descriptions like "helps with deals" cause undertriggering.
- **Be a bit pushy.** Include related terms so the model activates the skill even for adjacent requests. Example: "Use when the user asks for MEDPIC analysis, qualification scoring, deal readiness, or asks about MEDPIC components like Metrics, Economic Buyer, Decision Criteria..."
- **Use third person.** Write "Perform MEDPIC qualification analysis" not "I can help you with MEDPIC".
- **Keep under 1024 characters.** The description is injected into the system prompt for routing — longer wastes tokens every turn.

### 3.2 Body Structure

Follow this exact section order:

```markdown
# Skill Title

Lead sentence — what this skill does and the core principle.
Example: "Run an evidence-first MEDPIC assessment. Do not score from assumptions."

## Use this workflow

1. Confirm scope.
2. Gather evidence before scoring/analyzing.
3. Score/analyze each dimension.
4. Produce a structured output.

## Output contract

What sections must appear in the answer.

## Reference loading guide

Which reference files to load and when.
```

Each section explained below.

### 3.3 The Lead Sentence

One to two lines that set the core principle. This anchors the model's behavior.

**Pattern:** "Run an evidence-first [task]. Do not [common failure mode]."

**Examples from existing skills:**

- "Run an evidence-first MEDPIC assessment. Do not score from assumptions."
- "Run an evidence-first deal health assessment. Do not rate dimensions from assumptions."
- "Every claim must trace back to a transcript excerpt, meeting detail, or attendee data."

### 3.4 The Workflow

Write numbered steps. Typically 3-4 steps:

**Step 1 — Confirm scope.**
Define what "narrow" vs "broad" requests look like. The model needs to know when to do a partial analysis vs a full one.

```markdown
1. Confirm scope.
- If the request is component-specific, assess only the requested components.
- If the request is broad ("qualify this deal", "give MEDPIC score"), assess all six components.
```

**Step 2 — Gather evidence before scoring.**
List the data signals needed, grouped by category. Add tool hints in brackets as optional guidance.

```markdown
2. Gather evidence before scoring.
- Deal baseline and timeline reliability: current stage, amount, close timeline.
  [GetDealDetails, GetDealChangeHistory]
- Stakeholder coverage and authority signals: who is involved, roles, influence.
  [ListDealStakeholders, GetStakeholderDetails]
```

**Step 3 — Score/Analyze each dimension.**
Define the rating scale and evidence requirements.

```markdown
3. Score each component.
- Use only: `Strong`, `Moderate`, `Weak`, `Unknown`.
- Every non-`Unknown` score must include explicit evidence.
- If evidence is missing or contradictory, score `Unknown` or downgrade conservatively.
```

**Step 4 — Produce structured output.**
High-level output shape. Details go in the output contract.

### 3.5 The Output Contract

Define the exact sections the model must produce. Keep it tight — 4-6 sections max.

```markdown
## Output contract

Use this structure:

1. Overall assessment (1-2 lines)
2. Scoring table with component, score, confidence, and evidence
3. Gaps to close (top 3)
4. Next actions (3-5)
```

Add hard rules at the end:

```markdown
Never omit evidence. Never claim certainty when evidence is thin.
Do not expose internal IDs or UUIDs unless the user explicitly asks.
```

### 3.6 The Reference Loading Guide

Tell the model exactly which reference to load and for what purpose. Use explicit phrasing:

```markdown
## Reference loading guide

- Use `references/scoring-rubric.md` for scoring calibration.
- Use `references/evidence-quality-rules.md` for admissible evidence and tie-break rules.
- Use `references/output-guidelines.md` for multilingual output and diagram policy.
```

**Do not** say "see references/ for more info" — be specific about which file and why.

---

## 4. Writing Reference Files

References hold the depth that would bloat SKILL.md. They load on-demand when the model calls `get_skill_reference()`.

### What belongs in a reference file

| Reference type | Contents | Example |
|---------------|----------|---------|
| Scoring rubric | Rating scale definitions, per-dimension calibration, overall color logic, confidence guidance | `scoring-rubric.md` |
| Evidence rules | Admissibility criteria, minimum evidence per component, citation format, contradiction handling | `evidence-quality-rules.md` |
| Output guidelines | Language matching, diagram policy (Mermaid guidance), response shape, style constraints | `output-guidelines.md` |
| Domain-specific | Question banks, classification examples, pattern libraries, benchmarks | `discovery-question-bank.md` |

### Rules for reference files

1. **One file per capability.** Do not create a single `reference.md` with everything. Split by purpose.
2. **Keep references one level deep.** `SKILL.md` links to `references/x.md`. Never chain references: `references/x.md` should not link to `references/y.md`.
3. **Keep each file under 100 lines.** If it's longer, split it.
4. **Use tables and lists, not paragraphs.** The model processes structured data better than prose.
5. **Include calibration examples.** For scoring rubrics, show what `Strong` vs `Moderate` looks like concretely.

---

## 5. How to Mention Tools

Tools are how the model gathers data. The way you mention them matters.

### The data-first pattern

Describe the **data signal** first, then add tool names as **optional bracket hints**:

```markdown
- Stakeholder coverage and authority signals: who is involved, role completeness, influence level.
  [ListDealStakeholders, GetStakeholderDetails]
```

### Why data-first, not tool-first

| Approach | Problem |
|----------|---------|
| Tool-first: "Call GetDealDetails, then call ListDealStakeholders..." | Over-constrains the model. If a tool name changes or a better tool exists, the skill breaks. |
| Data-first: "Gather stakeholder coverage signals [ListDealStakeholders]" | Model understands the intent. Bracket hints guide without hard-locking. |

### Rules

- **Bracket hints are examples, not hard constraints.** Always include: "Use the best available tools for each data signal. Bracket hints are examples, not hard constraints."
- **Do not list every possible tool.** Include the 1-2 most relevant tools per data signal.
- **Do not force tool call order.** Let the model decide the optimal sequence.
- **For mandatory tool calls**, use explicit language: "You MUST call CreateReminder with all extracted parameters." Reserve this for action tools (create, update, delete), not data-gathering tools.

---

## 6. Rating Scales and Scoring

### Choosing a scale

Use one of these established scales. Do not invent new ones.

| Scale | When to use | Example skills |
|-------|-------------|----------------|
| `Strong` / `Moderate` / `Weak` / `Unknown` | Component-level qualification with evidence depth | medpic-assessment |
| `Green` / `Yellow` / `Red` | Traffic-light health assessment | deal-health-check |
| `Strong` / `Moderate` / `Weak` / `N/A` | Dimension analysis where some dimensions may not apply | call-analysis |
| `Forward` / `Sideways` / `Backward` | Momentum/trajectory verdicts | call-analysis (overall) |

### Scoring principles

1. **Evidence required for every non-default score.** If you cannot cite a date, quote, or data point, use `Unknown` or `N/A`.
2. **Conservative scoring.** When evidence is ambiguous, downgrade. Never round up.
3. **Overall rating has explicit logic.** Define the formula: "Green: at least 4 Strong, no more than 1 Weak/Unknown."
4. **Confidence is separate from score.** A component can be `Strong` with `Low` confidence (single recent data point) or `Weak` with `High` confidence (multiple confirming signals).

---

## 7. Common Pitfalls

### Skill design pitfalls

| Pitfall | Why it fails | Fix |
|---------|-------------|-----|
| Vague description | Model cannot route to the skill. Testing shows ~20% activation with unoptimized descriptions. | Include specific trigger phrases and related terms. |
| SKILL.md over 200 lines | Overwhelms context, competes with conversation history. | Move depth to reference files. |
| Explaining what the model already knows | Wastes tokens. The model knows what a stakeholder is. | Focus on YOUR methodology, YOUR scoring criteria, YOUR constraints. |
| Offering options without defaults | "You could use BFS, DFS, Dijkstra..." confuses the model. | Provide a default with escape hatches for specific cases. |
| No feedback loop | Model generates output and stops, even if data is missing. | Add "If evidence is missing, score Unknown or downgrade" rules. |
| Deeply nested references | Model partially reads files when references chain. | Keep all references one level deep from SKILL.md. |
| Inconsistent terminology | Mixing "score", "rating", "grade", "assessment" confuses. | Pick one term per concept and use it everywhere. |
| Time-sensitive content | "As of 2025, the best approach is..." will rot. | Write timeless methodology. |

### Content writing pitfalls

| Pitfall | Why it fails | Fix |
|---------|-------------|-----|
| Paragraphs instead of lists | Model processes structured data faster and more reliably. | Use tables, lists, and short bullets. |
| Restating what tools already document | Tool descriptions already explain parameters. | Reference the tool, don't re-document it. |
| No output contract | Model invents inconsistent output formats across runs. | Define exact sections every time. |
| Overloading a single reference file | 300-line reference file defeats progressive disclosure. | Split by capability: rubric, evidence rules, output format. |
| Missing scope confirmation step | Model always does full analysis even for narrow questions. | Always start with "Confirm scope" as Step 1. |

---

## 8. Pre-Publish Checklist

Before merging a new skill, verify each item:

### Structure

- [ ] Skill folder uses lowercase + hyphen naming
- [ ] `SKILL.md` exists with valid YAML frontmatter (`name`, `description`)
- [ ] `description` is under 1024 characters
- [ ] `description` includes specific trigger phrases
- [ ] `SKILL.md` body is under 200 lines
- [ ] `references/` directory exists with at least one reference file
- [ ] Reference files use intent-based kebab-case names

### Content

- [ ] Lead sentence states the core principle and main anti-pattern
- [ ] Workflow starts with "Confirm scope"
- [ ] Workflow lists data signals with bracket tool hints
- [ ] Includes "Use the best available tools..." caveat
- [ ] Rating scale is defined with evidence requirements
- [ ] Output contract specifies exact sections
- [ ] Reference loading guide maps each file to its purpose
- [ ] No internal IDs/UUIDs exposed in output by default

### Quality

- [ ] Evidence requirements are explicit (what counts, what doesn't)
- [ ] Conservative scoring rules are stated (downgrade on ambiguity)
- [ ] `Unknown`/`N/A` is defined as the default when evidence is missing
- [ ] Contradiction handling rules exist
- [ ] No time-sensitive content that will rot
- [ ] Terminology is consistent throughout

### Testing

- [ ] Skill loads without errors via `LocalSkills`
- [ ] `test_skills_loading.py` passes with the new skill
- [ ] Manual test: broad request triggers the skill and produces expected output
- [ ] Manual test: narrow request triggers partial analysis correctly
- [ ] Manual test: missing data produces `Unknown` scores, not fabricated ones

---

## 9. Quick-Start Template

Copy this to start a new skill:

```
my-new-skill/
├── SKILL.md
└── references/
    ├── scoring-rubric.md
    └── evidence-quality-rules.md
```

**SKILL.md template:**

```yaml
---
name: my-new-skill
description: [What it does]. Use when the user asks for [trigger phrase 1], [trigger phrase 2], [trigger phrase 3], or mentions [related terms].
---
```

```markdown
# [Skill Title]

Run an evidence-first [task]. Do not [common failure mode].
Treat tool names in brackets as optional hints, not mandatory calls.

## Use this workflow

1. Confirm scope.
- If the request is specific, analyze only the requested dimension.
- If the request is broad, analyze all dimensions.

2. Gather evidence before scoring.
- [Data signal category 1]: [what to look for].
  [ToolHint1, ToolHint2]
- [Data signal category 2]: [what to look for].
  [ToolHint3]

Use the best available tools for each data signal. Bracket hints are examples, not hard constraints.

3. Score each dimension.
- Use only: `[Rating1]`, `[Rating2]`, `[Rating3]`, `[Unknown/N/A]`.
- Every non-default score must include explicit evidence.
- If evidence is missing or contradictory, use [default] or downgrade conservatively.

4. Produce a structured output.
- Include overall rating.
- Include dimension table with ratings and evidence.
- Include top gaps and recommended actions.

## Output contract

Use this structure:

1. Overall assessment (1-2 lines)
2. Dimension table with rating, confidence, and evidence
3. Top gaps (3)
4. Next actions (3-5)

Never omit evidence. Never claim certainty when evidence is thin.
Do not expose internal IDs or UUIDs unless the user explicitly asks.

## Reference loading guide

- Use `references/scoring-rubric.md` for rating calibration.
- Use `references/evidence-quality-rules.md` for admissible evidence rules.
```

**references/scoring-rubric.md template:**

```markdown
# [Skill Name] Scoring Rubric

Use this rubric to calibrate ratings.

## Rating scale

- `[Rating1]`: [definition with evidence standard].
- `[Rating2]`: [definition with evidence standard].
- `[Rating3]`: [definition with evidence standard].
- `[Unknown]`: insufficient evidence to score.

## Dimension calibration

### [Dimension 1]
- `[Rating1]`: [concrete criteria].
- `[Rating2]`: [concrete criteria].
- `[Rating3]`: [concrete criteria].
- `[Unknown]`: [what missing looks like].

## Overall rating

- `[Best]`: [explicit formula, e.g., "at least 4 Strong, no more than 1 Weak"].
- `[Middle]`: [catch-all definition].
- `[Worst]`: [explicit formula].

## Confidence guidance

- `High`: [criteria].
- `Medium`: [criteria].
- `Low`: [criteria].
```

**references/evidence-quality-rules.md template:**

```markdown
# Evidence Quality Rules

## Evidence admissibility

- Prefer first-party evidence: transcript quotes, email content, notes, deal metadata.
- Prefer recency: default to evidence from the last [N] days.
- Treat AI summaries as supporting signals, not sole proof.

## Minimum evidence per dimension

- `[Dimension 1]`: [minimum evidence required].
- `[Dimension 2]`: [minimum evidence required].

If minimum evidence is not met, use `Unknown`.

## Citation requirements

- Cite concrete source type and context.
- Avoid generic citations like "from meetings" without specifics.

## Contradiction rules

- If sources conflict, prefer the most recent direct source.
- If unresolved, downgrade one level or set `Unknown`.
- Call out the contradiction in gaps and next actions.
```
