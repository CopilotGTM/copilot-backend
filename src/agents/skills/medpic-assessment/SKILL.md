---
name: medpic-assessment
description: Perform MEDPIC qualification analysis for a deal using evidence-based scoring. Use when the user asks for MEDPIC analysis, qualification scoring, deal readiness, or asks about MEDPIC components like Metrics, Economic Buyer, Decision Criteria, Paper Process, Identify Pain, or Champion.
---

# MEDPIC Assessment

Run an evidence-first MEDPIC assessment. Do not score from assumptions.
Prefer describing and collecting data signals. Treat tool names in brackets as optional hints, not mandatory calls.

## Use this workflow

1. Confirm scope.
- If the request is component-specific, assess only the requested components.
- If the request is broad ("qualify this deal", "give MEDPIC score"), assess all six components.

2. Gather evidence before scoring.
- Deal baseline and timeline reliability: current stage, amount, close timeline, ownership, and recent stage/timeline changes.
  [GetDealDetails, GetDealChangeHistory]
- Stakeholder coverage and authority signals: who is involved, role completeness, influence level, and likely budget authority.
  [ListDealStakeholders]
- Interaction momentum and decision trail: meeting cadence, recency, decisions, objections, and explicit next steps.
  [ListDealMeetings, GetMeetingSummary]
- Verbatim proof for critical claims: speaker-attributed quotes for pain, criteria, champion behavior, and buyer authority language.
  [GetMeetingTranscript]
- Written commitment and paper trail: procurement/legal mentions, approval path signals, and internal/external follow-through.
  [ListDealEmails, ListDealNotes, ListDealChannelMessages]
- Existing risk context: known blockers and unresolved risks that should affect score confidence.
  [GetExistingRisksBlockers]

Use the best available tools for each data signal. Bracket hints are examples, not hard constraints.

3. Score each MEDPIC component.
- Use only: `Strong`, `Moderate`, `Weak`, `Unknown`.
- MEDPIC here means:
  - `M` Metrics
  - `E` Economic Buyer
  - `D` Decision Criteria
  - `P` Paper Process
  - `I` Identify Pain
  - `C` Champion
- Every non-`Unknown` score must include explicit evidence (quote, date, or concrete data point).
- If evidence is missing or contradictory, score `Unknown` or downgrade conservatively.

4. Produce a structured output.
- Include overall color rating: `Green`, `Yellow`, or `Red`.
- Include a scoring table with component, score, confidence, and evidence.
- Include top gaps and prioritized next actions.

## Output contract

Use this structure:

1. Overall assessment (1-2 lines)
2. MEDPIC score table
3. Gaps to close (top 3)
4. Next actions (3-5)

Never omit evidence. Never claim certainty when evidence is thin.
Do not expose internal IDs or UUIDs unless the user explicitly asks for them.

## Reference loading guide

- Use `references/scoring-rubric.md` for scoring calibration.
- Use `references/evidence-quality-rules.md` for admissible evidence and tie-break rules.
- Use `references/discovery-question-bank.md` when the user asks what to ask next.
- Use `references/output-guidelines.md` for multilingual output and diagram policy.
