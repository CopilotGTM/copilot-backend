---
name: stakeholder-mapping
description: Perform evidence-based stakeholder power mapping and coverage gap detection to assess structural deal strength. Use when the user asks to map the stakeholders, identify key players, show the org chart, find the decision maker, assess whether coverage is sufficient, or asks whether there is a champion.
---

# Stakeholder Mapping

Run an evidence-first stakeholder mapping assessment. Do not infer authority, role, or engagement
from title alone.
Treat tool names in brackets as optional hints, not mandatory calls.
Bracket hints are examples, not hard constraints.

Follow the GeneralAgent Context Resolution Gate before asking which deal or stakeholder
the user means. Use active deal/account context and read-only CRM lookup first; ask with
candidate deals or people only when ambiguity remains.

## Use this workflow

1. Confirm scope.
- If no deal context exists after active context and read-only lookup, ask which deal to
  analyze and list any useful candidate deals found.
- If the request is narrow ("who is the decision maker?", "is there a champion?"), answer that
  focus area but still enumerate stakeholders first.
- If the request is broad ("map the stakeholders", "do we have the right coverage?"), run the full
  framework.
- If stakeholder records are incomplete, continue with available evidence and call out the gap
  explicitly.

2. Gather evidence before analyzing.
- Stakeholder enumeration and known attributes: name, title, role fields, department, relationship
  history. Use the same stakeholder list tool with a person query when the user asks about one
  stakeholder.
  [ListDealStakeholders]
- Deal context and existing notes that may mention ownership, authority, or internal relationship
  history.
  [GetDealDetails, ListDealNotes]
- Stakeholder insights and structured signals already captured by the system.
  [ListDealStakeholderInsights]
- Interaction evidence: meeting participation, transcript language, and participation recency.
  [ListDealMeetings, GetMeetingTranscript]
- Communication evidence: email responsiveness and channel participation when available.
  [ListDealEmails, GetEmailDetails, ListDealChannelMessages]

Use the best available tools for each data signal. Bracket hints are examples, not hard
constraints.

3. Classify and assess structure.
- Load `references/classification-rubric.md` for role and influence calibration.
- Load `references/engagement-scoring.md` for engagement status and decay rules.
- Classify role as `Economic Buyer`, `Technical Evaluator`, `End User`, `Coach`, `Blocker`, or
  `Unknown`.
- Classify influence as `High`, `Medium`, `Low`, or `Unknown`.
- Classify engagement as `Active`, `Passive`, `Disengaged`, or `Unknown`.
- Every non-`Unknown` classification must cite concrete evidence source types such as `Stakeholder
  record`, `Transcript`, `Email`, `Notes`, or `Channel`.
- If evidence is missing, stale, or contradictory, use `Unknown` or downgrade conservatively.
- Explicitly evaluate missing EB, no validated champion, single-threading, disengaged power holder,
  authority ambiguity, silent veto risk, and influence concentration.

4. Produce a structured output.
- Keep the response structural and diagnostic, not generic.
- Focus interventions on leverage points tied to observed gaps.
- Do not recommend "add more stakeholders" unless the gap is explicitly evidenced.

## Output contract

Use this structure and keep the section order:

1. Stakeholder Power Map Table
2. Structural Coverage Analysis
3. Engagement Risk Signals
4. Leverage Points for Intervention
5. Unknowns / Missing Evidence

Never hallucinate roles or authority.
Never assume title equals budget ownership.
Never expose internal IDs or UUIDs unless the user explicitly asks.
Mark `Unknown` when evidence is insufficient.

## Reference loading guide

- Use `references/classification-rubric.md` for role and influence calibration.
- Use `references/coverage-gaps-guide.md` for structural gap definitions and why they matter.
- Use `references/engagement-scoring.md` for engagement status and decay interpretation.
