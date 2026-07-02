---
name: deal-health-check
description: Assess overall deal health using a traffic-light framework across five dimensions — activity recency, stakeholder coverage, next-steps clarity, competitive position, and timeline realism. Use when the user asks about deal health, deal status, whether a deal is on track, deal risk assessment, or pipeline review.
---

# Deal Health Check

Run an evidence-first deal health assessment. Do not rate dimensions from assumptions.
Prefer collecting concrete signals before assigning any color. Treat tool names in brackets as optional hints, not mandatory calls.

## Use this workflow

1. Confirm scope.
- If the request targets a specific dimension ("how's our stakeholder coverage?"), focus on that dimension only.
- If the request is broad ("how's this deal looking?", "is this deal on track?"), assess all five dimensions.

2. Gather evidence before rating.
- Deal baseline and stage context: current stage, amount, close date, ownership, and stage progression history.
  [GetDealDetails, GetDealChangeHistory]
- Stakeholder map and engagement: who is involved, roles, influence levels, and recency of engagement.
  [ListDealStakeholders]
- Interaction timeline and momentum: meeting cadence, last activity dates, communication gaps.
  [ListDealMeetings, ListMeetingsByDate, ListDealEmails, ListDealChannelMessages]
- Next steps and commitments: explicit actions with owners and dates from recent meetings and notes.
  [GetMeetingSummary, ListDealNotes]
- Competitive signals: competitor mentions, evaluation dynamics, positioning language.
  [GetMeetingTranscript, ListDealEmails, InternalKnowledgeBase]
- Existing risks and blockers: known issues that affect deal progress.
  [GetExistingRisksBlockers]

Use the best available tools for each data signal. Bracket hints are examples, not hard constraints.

3. Rate each dimension.
- Use only traffic-light ratings: `Green`, `Yellow`, `Red`.
- Every non-trivial rating must include explicit evidence (date, quote, or concrete data point).
- If evidence is missing or contradictory, default to `Yellow` or `Red` conservatively.
- Load `references/scoring-rubric.md` for detailed rating criteria per dimension.

4. Produce a structured output.
- Include overall health rating: `Green`, `Yellow`, or `Red`.
- Include a dimension table with rating and evidence.
- Include top risks with mitigation suggestions.
- Include prioritized recommended actions.

## Output contract

Use this structure:

1. Overall health rating with justification (2-3 lines)
2. Dimension rating table with evidence
3. Top risks and mitigation suggestions (top 3)
4. Recommended actions (3-5, prioritized)

Never omit evidence. Never claim a deal is healthy without concrete signals.
Do not expose internal IDs or UUIDs unless the user explicitly asks for them.

## Reference loading guide

- Use `references/scoring-rubric.md` for rating calibration per dimension.
- Use `references/evidence-quality-rules.md` for admissible evidence and recency rules.
- Use `references/output-guidelines.md` for multilingual output and diagram policy.
