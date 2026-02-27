# MEDPIC Output Guidelines

## Multilingual behavior

- Reply in the user's language.
- Keep MEDPIC component labels stable in English unless the user asks full localization.
- Keep quoted evidence in source language; add translated interpretation when helpful.

## Diagram behavior

- Use diagrams only when they improve a decision.
- Prefer Mermaid `flowchart` for qualification blockers and ownership.
- Prefer Mermaid `quadrantChart` only when you have enough comparable scoring signals.
- Do not produce decorative diagrams without evidence-backed nodes.

## Recommended response shape

1. Overall color rating and confidence.
2. MEDPIC scoring table with evidence.
3. Top qualification gaps.
4. Prioritized next actions.

## Style constraints

- Be specific and operational, not generic.
- Separate observed facts from interpretation.
- Mark uncertainty explicitly; do not hide gaps.
- Do not include UUIDs or internal record IDs unless explicitly requested by the user.
