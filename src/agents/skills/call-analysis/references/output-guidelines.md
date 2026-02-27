# Call Analysis Output Guidelines

## Multilingual behavior

- Reply in the user's language.
- Keep dimension labels stable in English unless the user asks for full localization.
- Keep quoted transcript excerpts in source language; add translated interpretation when helpful.

## Diagram behavior

- Use diagrams only when they improve understanding.
- Prefer Mermaid `sequenceDiagram` for visualizing conversation flow or handoffs.
- Prefer Mermaid `flowchart` for decision trees or next-step paths.
- Do not produce decorative diagrams without evidence-backed content.

## Recommended response shape

1. Call overview (who, when, deal context).
2. Overall momentum verdict: Forward / Sideways / Backward with justification.
3. Dimension analysis with ratings and transcript quotes.
4. Key Takeaways (3-5 actionable items).

## Style constraints

- Be specific and operational, not generic.
- "Rep asked 3 discovery questions but missed the budget objection at [timestamp]" is better than "rep performance was moderate."
- Separate observed behavior from interpretation.
- Mark uncertainty explicitly; do not hide gaps.
- Skip dimensions where there is no relevant data — do not pad with "N/A" assessments.
- Do not include UUIDs or internal record IDs unless explicitly requested by the user.
