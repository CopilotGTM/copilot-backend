# Agentic Prompt Patterns

When creating agentic reminders, the `agent_prompt` runs independently at trigger time with no conversation history. Write prompts that are self-contained and produce actionable output.

## Pattern: Deal health check

```
Analyze the current health of the {deal_name} deal.
Assess activity recency, stakeholder coverage, next steps, competitive position, and timeline realism.
Highlight top risks and recommend immediate actions.
```

## Pattern: MEDPIC assessment

```
Run a full MEDPIC assessment for the {deal_name} deal.
Score all six components (Metrics, Economic Buyer, Decision Criteria, Paper Process, Identify Pain, Champion) with evidence.
Include gaps and prioritized next actions.
```

## Pattern: Pipeline summary

```
List all active deals in the pipeline.
For each deal show: name, stage, amount, close date, and days since last activity.
Flag deals with no activity in the last 7 days.
Highlight the top 3 risks across the pipeline.
```

## Pattern: Deal activity check

```
Check the {deal_name} deal for any new activity in the last {timeframe}.
Look for: meetings, emails, notes, channel messages, and stage changes.
Summarize what happened or flag if nothing has moved.
```

## Pattern: Meeting prep

```
Prepare a briefing for my upcoming meeting on the {deal_name} deal.
Include: deal overview, key stakeholders and their roles, recent activity, open risks, and suggested talking points.
```

## Pattern: Closing month deals

```
List all deals with close dates in the current month.
For each: show stage, amount, owner, last activity, and confidence assessment.
Highlight deals at risk of slipping past month-end.
```

## Guidelines

- Always include the deal name explicitly — the agent cannot infer from conversation context.
- Include `deal_id` parameter when available so the agent has direct deal context.
- Specify what output shape you expect (scores, tables, bullet points).
- For recurring prompts, use relative timeframes ("last 24 hours", "last 7 days") not absolute dates.
- Keep prompts concise but complete — the agent follows the prompt as its sole instruction.
