---
name: call-analysis
description: Analyze a sales call or meeting — rep performance, customer signals, key decisions, action items, risk indicators, and deal momentum. Use when the user asks about a call, meeting performance, what happened in a meeting, call coaching, or meeting review.
---

# Call / Meeting Analysis

Run an evidence-first call analysis. Do not assess dimensions from assumptions.
Every claim must trace back to a transcript excerpt, meeting detail, or attendee data. Treat tool names in brackets as optional hints, not mandatory calls.

## Use this workflow

1. Confirm scope.
- If the user names a specific meeting, analyze that one.
- If the user asks about "the last call" or "recent meetings", identify the most relevant meeting first.
- If the request focuses on one dimension ("how did the rep handle objections?"), focus there.
- If the request is broad ("analyze this call", "how did the meeting go?"), assess all six dimensions.

2. Gather evidence before analyzing.
- Meeting context and logistics: date, attendees, duration, deal association.
  [GetMeetingDetails, ListDealMeetings, ListMeetingsByDate]
- Structured summary: AI-generated overview, key topics, and outcomes.
  [GetMeetingSummary]
- Full transcript: speaker-attributed dialogue for quoting and pattern analysis.
  [GetMeetingTranscript]
- Attendee context: stakeholder roles, influence levels, and engagement history.
  [ListDealStakeholders, GetStakeholderDetails]
- Deal baseline: current stage, amount, and recent changes for momentum assessment.
  [GetDealDetails, GetDealChangeHistory]

Use the best available tools for each data signal. Bracket hints are examples, not hard constraints.

3. Analyze each dimension.
- Load `references/scoring-rubric.md` for detailed rating criteria.
- Rate each dimension: `Strong`, `Moderate`, `Weak`, or `N/A` (if no relevant data).
- Every non-`N/A` rating must include a transcript quote or concrete data point.
- Skip dimensions where there is genuinely no relevant data — do not pad with empty assessments.

4. Produce a structured output.
- Include overall call assessment: moved the deal Forward, Sideways, or Backward.
- Include a dimension analysis with ratings, evidence, and quotes.
- Include key takeaways (3-5 actionable items).

## Output contract

Use this structure:

1. Call overview (who, when, deal context — 2-3 lines)
2. Overall momentum verdict: Forward / Sideways / Backward with justification
3. Dimension analysis with transcript quotes as evidence
4. Key Takeaways (3-5 actionable items)

Never fabricate quotes. Never claim certainty about rep intent without transcript evidence.
Do not expose internal IDs or UUIDs unless the user explicitly asks for them.

## Reference loading guide

- Use `references/scoring-rubric.md` for dimension rating calibration.
- Use `references/evidence-quality-rules.md` for admissible evidence and citation rules.
- Use `references/output-guidelines.md` for multilingual output and diagram policy.
