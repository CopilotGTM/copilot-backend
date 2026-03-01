# Pipeline Review Risk Ranking Rules

Use these rules to rank the top risk deals consistently and limit deep analysis to the right set.

## Risk scoring dimensions

- Score each dimension from `0-2`:
- Stage aging vs benchmark
- Activity recency
- Close-date compression
- Stakeholder completeness
- Historical slippage
- Health score when available

## Total score interpretation

- `0-3` -> `Low risk`
- `4-7` -> `Medium risk`
- `8-12` -> `High risk`

## Ranking rules

- Prioritize forecast-period deals.
- Higher deal value breaks ties.
- Multiple risk dimensions stack.
- If a critical signal exists (`2x` stage aging or `>=2` close-date pushes), automatically elevate
  the deal into the top 5.

## Deep-dive boundary

- Only the top 3-5 ranked deals should receive deeper reasoning by default.
