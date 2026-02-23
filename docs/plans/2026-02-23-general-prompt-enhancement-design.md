# General Prompt Enhancement Design

**Date**: 2026-02-23
**Status**: Approved (revised)
**Scope**: `src/agents/workflows/general/prompts/general_prompt.py`

## Problem

Kai's GeneralAgent prompt produces inconsistent quality for structured analyses:
- Call insights are shallow — no thorough multi-step analysis
- MEDDIC assessments vary between detailed and superficial
- No enforcement of "gather data first, then synthesize"
- No explicit error handling or anti-hallucination rules

## Approach

**Option chosen**: Enhanced prompt with quality-rubric playbooks (single-agent, no infrastructure changes).

Research from Claude Code, Cursor, Devin, Manus system prompts showed that structured playbooks produce consistent quality without multi-agent overhead. Multi-agent extraction is deferred to Phase 2 if quality gaps persist.

**Key design decision**: Playbooks define **what to analyze** and **what quality looks like**, NOT which tools to call. The LLM decides tool selection based on the question's needs. This preserves the model's ability to adapt while ensuring consistent quality.

## Changes

### 1. Core Behavior Enhancements

| Enhancement | Description |
|-------------|-------------|
| Gather-first mandate | Always call tools before making factual claims |
| Anti-hallucination | Never fabricate data; explicitly state what's missing |
| Error/fallback behavior | What to do when tools fail or return no results |
| Consistency rules | Analysis responses always use structured sections |
| Evidence mandate | Every claim must reference its source |

### 2. Analysis Playbooks (Phase 1 — 3 playbooks, rubric-style)

Each playbook defines:
- **When** it applies (trigger phrases)
- **What to analyze** (dimensions/criteria)
- **Quality bar** (what a good answer looks like)
- **Depth calibration** (scale effort to the question's complexity)

| Playbook | Dimensions Analyzed |
|----------|-------------------|
| Call/Meeting Analysis | Rep performance, customer signals, decisions, action items, risk indicators, momentum |
| MEDDIC Assessment | Metrics, Economic Buyer, Decision Criteria, Decision Process, Identify Pain, Champion (scored Strong/Moderate/Weak/Unknown) |
| Deal Health Check | Activity recency, stakeholder coverage, next steps clarity, competitive position, timeline realism (rated Green/Yellow/Red) |

### 3. Removed: Prescriptive Tool Chains

The original design had explicit tool sequences ("SearchDeals -> GetDealDetails -> ListDealStakeholders"). These were removed to avoid turning the LLM into a blind recipe-follower. Tool selection is left to the model's reasoning.

### 4. Simplified: Sales Intelligence Section

MEDDIC framework details moved into the MEDDIC playbook. Sales Intelligence section reduced to a "mindset" directive: interpret data, classify stakeholders, surface risks proactively.

### 5. Future Playbooks (Phase 2+)

- Competitive analysis
- Stakeholder mapping
- Deal comparison
- Pipeline review
- Forecasting analysis

## Token Impact

Prompt grows from ~230 to ~306 lines (~400 additional input tokens). More efficient than the first draft (which was ~420 lines) due to removing prescriptive tool chains.

## File Changed

| File | Action |
|------|--------|
| `src/agents/workflows/general/prompts/general_prompt.py` | Edit — enhanced prompt |
