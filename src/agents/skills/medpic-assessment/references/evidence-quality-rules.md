# Evidence Quality Rules

## Evidence admissibility

- Prefer first-party evidence: meeting transcript quotes, email content, notes, deal metadata.
- Prefer recency: default to evidence from the last 90 days unless the user asks historical.
- Treat AI summaries as supporting signals, not sole proof for critical scoring.

## Minimum evidence per component

- `Metrics`: one quantified outcome or explicit absence statement.
- `Economic Buyer`: named person plus authority or budget signal.
- `Decision Criteria`: explicit requirements, scorecard, or stated evaluation priorities.
- `Paper Process`: named approval/procurement/legal steps or owners.
- `Identify Pain`: explicit problem statement plus impact or urgency signal.
- `Champion`: named advocate plus observable internal action.

If minimum evidence is not met, use `Unknown`.

## Citation requirements

- Cite concrete source type and context in table evidence cells:
  - meeting summary/transcript (speaker + date when possible)
  - note/email/channel evidence
  - deal/stakeholder metadata signal
- Avoid generic citations like "from meetings" without specifics.

## Contradiction rules

- If sources conflict, prefer the most recent direct source.
- If contradiction remains unresolved, downgrade one level or set `Unknown`.
- Explicitly call out the contradiction in gaps and next actions.
