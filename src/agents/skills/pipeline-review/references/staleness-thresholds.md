# Pipeline Review Staleness Thresholds

Use these thresholds to assess pipeline aging and inactivity without improvising stage severity.

## Stage aging benchmarks

- Stage 1 (Discovery): 7 days
- Stage 2 (Qualification): 10 days
- Stage 3 (Evaluation): 14 days
- Stage 4 (Validation): 21 days
- Stage 5 (Negotiation): 14 days

## Aging severity

- Exceeds `1.5x` benchmark -> `At Risk`
- Exceeds `2x` benchmark -> `Critical`

## Activity staleness

- Early stage: `7+` days inactivity -> `Stale`
- Mid stage: `5+` days inactivity -> `Stale`
- Late stage: `3+` days inactivity -> `High Risk`

## Rules

- Always adjust by stage context when stage data is available.
- If stage mapping is unavailable, use `Unknown` instead of forcing a stale label.
