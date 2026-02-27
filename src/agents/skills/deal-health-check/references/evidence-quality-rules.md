# Evidence Quality Rules

## Evidence admissibility

- Prefer first-party evidence: meeting transcripts, emails, notes, deal metadata, channel messages.
- Prefer recency: default to evidence from the last 30 days unless the user asks for historical context.
- Treat AI summaries as supporting signals, not sole proof for dimension ratings.

## Minimum evidence per dimension

- `Activity Recency`: at least one dated interaction (meeting, email, note, or message).
- `Stakeholder Coverage`: named stakeholders with role and engagement status.
- `Next Steps Clarity`: explicit action item with owner from a recent meeting or note.
- `Competitive Position`: mention of competitor or positioning signal from any source.
- `Timeline Realism`: close date plus current stage and recent stage change history.

If minimum evidence is not met, rate `Yellow` or `Red` conservatively.

## Citation requirements

- Cite concrete source type and context in the evidence column:
  - meeting date and key outcome
  - email sender/recipient and relevant content
  - note content and author
  - deal metadata field and value
- Avoid generic citations like "from meetings" without specifics.

## Recency weighting

- Last 7 days: full weight — strong signal.
- 8-30 days: moderate weight — may indicate slowing momentum.
- 30+ days: low weight — stale signal; flag as risk.

## Contradiction rules

- If sources conflict, prefer the most recent direct source.
- If contradiction remains unresolved, downgrade one level.
- Explicitly call out the contradiction in risks and recommended actions.
