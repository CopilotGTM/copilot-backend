---
name: pipeline-review
description: Perform forecast-integrity pipeline intelligence with risk prioritization, slippage detection, and systemic pattern analysis. Use when the user asks for a pipeline overview, forecast confidence, pipeline risk assessment, stale deals, quarter-end slip risk, or portfolio-level sales insights such as "How's my pipeline?", "What deals need attention?", "Pipeline risk report", "Show me stale deals", "How confident is this forecast?", or "What might slip this quarter?".
---

# Pipeline Review

Run an evidence-first pipeline review. Do not perform a full deep-dive on every deal.
Default to breadth over depth.
Treat tool names in brackets as optional hints, not mandatory calls.

## Use this workflow

1. Confirm scope.
- If the user asks for a broad portfolio review, assess the full active pipeline at a summary
  level.
- If the user asks about forecast confidence, quarter-end slippage, or stale deals, prioritize
  those dimensions but still review the full active pipeline first.
- If the user explicitly names one or more deals, include those deals in the detailed risk review
  even if they do not rank in the top 3-5.
- Do not run full qualification analysis on every deal. Only drill deeper into the top 3-5 ranked
  risk deals, deals closing in the current forecast window, and deals explicitly requested by the
  user.

2. Gather evidence before analyzing.
- Portfolio baseline: active deals, stage, amount, owner, close date, and last activity.
  [ListDeals, GetPipelineDetails]
- Deal timeline reliability: stage entry timing, historical stage movement, and close-date changes
  when available.
  [GetDealChangeHistory, GetDealDetails]
- Deal health and context: qualification or health signals, notes, and summary-level context if
  already captured.
  [GetDealDetails, ListDealNotes]
- Stakeholder coverage signals for lightweight risk scoring when available.
  [ListDealStakeholders]

Use the best available tools for each data signal. Bracket hints are examples, not hard
constraints.

- Collect, when available: deal stage, stage entry date, close date, historical close-date pushes,
  amount, owner, last activity date, stage duration vs benchmark, qualification or health score,
  and stakeholder coverage signals.
- If any metric is unavailable, mark it as `Unknown`.
- Do not estimate or assume missing values.

3. Analyze the portfolio and rank risks.
- Load `references/staleness-thresholds.md` for stage-specific aging and activity rules.
- Load `references/pipeline-health-signals.md` for distribution, bottleneck, and velocity
  interpretation.
- Load `references/risk-ranking-rules.md` for top-risk deal prioritization.
- Evaluate portfolio-level signals: stage-specific staleness, stage aging vs benchmark, close-date
  compression risk, slippage signals, stage distribution and bottlenecks, and systemic cross-deal
  patterns.
- Flag systemic risks only when thresholds are met, and mark them `Unknown` if threshold inputs are
  incomplete.
- Rank the top 3-5 risk deals using the reference scoring model.
- Keep non-priority deals at lightweight signal scoring only.
- Use these systemic thresholds:
  `>=40%` of deals missing Economic Buyer -> qualification weakness;
  `>=30%` of pipeline stuck in the same stage -> bottleneck risk;
  `>=25%` of deals stale (stage-adjusted) -> execution discipline risk;
  `>=50%` of forecast amount closing in the final 2 weeks -> compression risk;
  `>=40%` of total pipeline value owned by a single rep -> concentration risk;
  `>=35%` of total pipeline value tied to 1-2 deals -> dependency risk.

4. Produce a structured output.
- Keep the analysis executive-ready and portfolio-level.
- Separate direct data from inference.
- Use deeper deal commentary only for the ranked top 3-5 risk deals, forecast-window deals, or
  explicitly requested deals.
- Keep recommendations focused on leadership interventions and priority deal attention, not generic
  deal-by-deal task lists.

## Output contract

Use this structure and keep the section order:

1. Executive Forecast Summary
2. Portfolio Snapshot
3. Stage Distribution & Bottlenecks
4. Slippage & Compression Risk
5. Top 3-5 Risk Deals
6. Rep-Level Risk Patterns
7. Systemic Portfolio Risks
8. Recommended Leadership Actions
9. Confidence Level

- `Executive Forecast Summary`: overall pipeline health and forecast confidence (`High`, `Medium`,
  or `Low`) with reasoning.
- `Portfolio Snapshot`: total pipeline value, forecast-period value, number of active deals, and
  average stage age vs benchmark.
- `Stage Distribution & Bottlenecks`: stage distribution summary and bottleneck stages.
- `Slippage & Compression Risk`: close-date clustering and late-stage fragility signals.
- `Top 3-5 Risk Deals`: ranked deals, evidence-based reasoning, and primary risk driver.
- `Rep-Level Risk Patterns`: owner breakdown when multiple reps exist and concentration risks when
  applicable.
- `Systemic Portfolio Risks`: threshold-based cross-deal pattern detection.
- `Recommended Leadership Actions`: portfolio-level interventions and highest-priority deal-level
  attention areas.
- `Confidence Level`: `High`, `Medium`, or `Low` with an explanation tied to data completeness.

Never expose internal IDs or UUIDs unless explicitly requested.
Never estimate missing values.
Always mark incomplete metrics as `Unknown`.
Avoid speculative language.
Do not deep-dive every deal unless the ranking rules or user scope require it.

## Reference loading guide

- Use `references/staleness-thresholds.md` for stage-specific aging and inactivity rules.
- Use `references/pipeline-health-signals.md` for stage distribution, bottleneck, and velocity
  interpretation.
- Use `references/risk-ranking-rules.md` for prioritizing the top 3-5 risk deals.
- Use `references/output-guidelines.md` for formatting, tone, and visualization rules.
