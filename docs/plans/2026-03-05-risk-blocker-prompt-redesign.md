# Risk/Blocker Extraction Prompt Redesign

**Date:** 2026-03-05
**Status:** Approved
**Scope:** `src/agents/workflows/risk_blocker_extraction/prompts.py`

## Problem

The risk/blocker extraction agent was producing low-quality output:
- Client pain points and business failures counted as deal risks (agent couldn't distinguish client vs vendor)
- Signals from deal memory mechanically converted into risks without analysis
- Future speculation — predicting risks for stages that haven't happened
- Generic, vague items ("budget concerns", "stakeholder alignment needed") with no evidence
- Technical concerns flagged as risks without checking if we actually support the capability
- Old outdated risks piling up because existing items were never managed (updated/dismissed/resolved)
- Wrong classifications — inflating RISKs into BLOCKERs

## Design Decisions

### Approach: Guided Analyst (Structured sections, human paragraphs)

Clear section breaks for major conceptual blocks, but each section written as flowing prose — not bullet lists, tables, or numbered rules. Matches the deal memory prompt's natural human tone.

### What the prompt covers (8 sections):

1. **Role & Audience** — Sales engineer at {{ORG_NAME}}, output for sales managers reviewing pipeline
2. **Who Is Who** — {{ORG_NAME}} is vendor, prospect is buyer. Client pain = reason for deal, not a risk. "Simple test: more or less likely to buy?"
3. **Risk vs Blocker** — Probability vs certainty. Most items should be risks. Blockers are rare.
4. **Present State Only** — No future speculation. Evidence must exist today.
5. **Signals != Risks** — Analyze signals from deal memory, don't mechanically convert. Check temporal resolution.
6. **Internal Knowledge Verification** — Search knowledge base before flagging technical concerns as risks.
7. **Managing Existing Items** — Review existing items first, use update/resolve/dismiss tools. Prevent pile-up.
8. **Quality Over Quantity** — Evidence test, materiality test, specificity test. Inline good/bad examples. Actionable next steps.

### What the prompt does NOT cover (handled elsewhere):

- **Categories list** — defined in `RiskBlockerCategory` enum, LLM sees via schema
- **Severity definitions** — defined in `RiskBlockerSeverity` enum
- **Output field descriptions** — defined in `ExtractedRiskBlocker` model Field descriptions
- **Execution steps** — defined in user prompt in `agent.py`
- **Tool names** — injected by Agno automatically

### Supporting changes:

- `registry.py`: Added `SEARCH_INTERNAL_KNOWLEDGE` to RiskBlockerExtractionAgent tool set
- `agent.py`: User prompt updated with step-by-step execution flow (read memory -> read existing -> verify technical -> drill down if needed -> identify new)

## Key Principles

- Human-written tone matching deal memory prompt style
- No redundancy with schema/tool docstrings
- Critical rules (client vs vendor, signals != risks) get their own sections so they don't get buried
- Examples woven inline, not in separate tables
- Quality over quantity philosophy throughout
