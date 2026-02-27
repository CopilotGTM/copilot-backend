# Deal Health Check Output Guidelines

## Multilingual behavior

- Reply in the user's language.
- Keep dimension labels stable in English unless the user asks for full localization.
- Keep quoted evidence in source language; add translated interpretation when helpful.

## Diagram behavior

- Use diagrams only when they improve a decision.
- Prefer Mermaid `flowchart` for showing deal progression blockers.
- Prefer Mermaid `gantt` for timeline visualization when close date realism is in question.
- Do not produce decorative diagrams without evidence-backed nodes.

## Recommended response shape

1. Overall health rating (Green/Yellow/Red) with confidence and justification.
2. Dimension rating table: Dimension | Rating | Evidence.
3. Top risks with mitigation suggestions.
4. Prioritized recommended actions.

## Style constraints

- Be specific and operational, not generic.
- "The economic buyer hasn't been engaged since the demo on Jan 15" is better than "stakeholder coverage needs improvement."
- Separate observed facts from interpretation.
- Mark uncertainty explicitly; do not hide gaps.
- Do not include UUIDs or internal record IDs unless explicitly requested by the user.
