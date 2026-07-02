---
name: close-plan
description: Perform backward-planned deal acceleration by aligning timeline, stakeholder readiness, and gate completion to a target close date. Use when the user asks to close faster, create a close plan, assess whether a deal can close by a target date, accelerate a deal, check whether it can close this quarter, or asks what is blocking the deal.
---

# Close Plan

Run an evidence-first close plan. Do not promise acceleration that the timeline math does not
support.
Start from the target close date and work backward.
Treat tool names in brackets as optional hints, not mandatory calls.
Bracket hints are examples, not hard constraints.

## Use this workflow

1. Confirm scope.
- If the user asks to accelerate or create a close plan, treat the request as a backward-planning
  exercise from the target close date.
- If the user asks whether the deal can close this quarter or by a specific date, make feasibility
  against that date the primary output.
- If no target close date is explicit, use the deal's current target close date if available;
  otherwise mark target-date-dependent conclusions as `Unknown`.
- Focus on one deal only. This is not a portfolio skill.

2. Gather evidence before analyzing.
- Deal baseline and timeline: current stage, target close date, amount, stage entry timing, and
  qualification or health context if available.
  [GetDealDetails, GetDealChangeHistory, GetPipelineDetails]
- Stakeholder readiness: economic buyer access, champion presence, technical evaluator coverage,
  and multi-threading depth.
  [ListDealStakeholders]
- Recent execution evidence: meetings, transcripts, emails, notes, and channel messages that show
  blockers, validation status, or paper process movement.
  [ListDealMeetings, GetMeetingTranscript, ListDealEmails, GetEmailDetails, ListDealNotes,
  ListDealChannelMessages]

Use the best available tools for each data signal. Bracket hints are examples, not hard
constraints.

- Collect, when available: current stage, target close date, stage benchmarks, stakeholder role
  coverage, paper process status, and historical stage velocity.
- If any required input is missing, mark it as `Unknown`.
- Do not estimate missing durations.

3. Assess feasibility and blockers.
- Load `references/stage-duration-benchmarks.md` for remaining-stage timing.
- Load `references/close-checklist.md` for required pre-close gates.
- Load `references/acceleration-playbook.md` for valid acceleration tactics.
- Identify remaining stages and the required exit criteria.
- Calculate minimum realistic timeline by summing remaining benchmark durations, adjusting for
  stakeholder readiness, and adjusting for paper process status.
- Compare minimum realistic timeline versus time left to target close.
- Identify blocking gaps: missing `Economic Buyer`, no `Champion`, no technical validation
  complete, procurement or legal not initiated, and multi-threading gaps.
- Classify feasibility as `On Track`, `At Risk`, or `Unrealistic`.
- Use these timeline compression rules:
  remaining benchmark duration greater than time left to close -> unrealistic compression;
  procurement or legal not started with `<30` days remaining -> high risk;
  no `Economic Buyer` identified in late stage -> high risk;
  security review required but not initiated -> high risk.
- Never assume acceleration without explicit evidence.

4. Produce a structured output.
- Keep the response direct and action-oriented.
- Separate observed data from inference.
- Use milestones with owners and dates where they can be supported by the evidence.
- Keep acceleration recommendations within valid approval sequencing.

## Output contract

Use this structure and keep the section order:

1. Close Feasibility Assessment
2. Timeline Gap Analysis
3. Stakeholder Readiness Gaps
4. Paper Process Status
5. Milestone-Based Close Plan
6. Primary Blockers
7. Acceleration Recommendations
8. Confidence Level

- `Close Feasibility Assessment`: `On Track`, `At Risk`, or `Unrealistic` with reasoning.
- `Timeline Gap Analysis`: time remaining, minimum realistic duration, and compression delta.
- `Stakeholder Readiness Gaps`: missing or weak stakeholder coverage against close requirements.
- `Paper Process Status`: legal, procurement, security, and similar gate status if available.
- `Milestone-Based Close Plan`: milestone, owner, target date, and risk flag.
- `Primary Blockers`: most important constraints preventing the target close.
- `Acceleration Recommendations`: only valid acceleration tactics tied to the observed gaps.
- `Confidence Level`: `High`, `Medium`, or `Low` with an explanation tied to data completeness.

Never fabricate stakeholder roles.
Never estimate missing durations.
Never promise unrealistic acceleration.
Always use `Unknown` when required data is missing.
Never expose internal IDs or UUIDs unless explicitly requested.

## Reference loading guide

- Use `references/stage-duration-benchmarks.md` for typical stage timing and compression
  thresholds.
- Use `references/close-checklist.md` for pre-close requirements and escalation rules.
- Use `references/acceleration-playbook.md` for valid gap-resolution strategies.
- Use `references/output-guidelines.md` for formatting, tone, and timeline-visual rules.
