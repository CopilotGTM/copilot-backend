# Static vs Agentic Classification Examples

## Static reminders (fixed message)

These deliver a pre-written notification. The user wants to be reminded of something, not have analysis done.

| User request | title | message | recurrence |
|-------------|-------|---------|------------|
| "Remind me to follow up with Acme Corp at 3 PM" | Follow up with Acme Corp | Follow up with Acme Corp on the proposal | none |
| "Ping me tomorrow at 9 AM to send the contract" | Send contract | Send the contract to the customer | none |
| "Don't let me forget the team standup every weekday at 10 AM" | Team standup | Join the daily team standup meeting | weekdays |
| "Set a reminder for Friday to update the pipeline" | Update pipeline | Update the deal pipeline before end of week | none |
| "Remind me every Monday to review my deals" | Weekly deal review | Review all active deals and update stages | weekly |

## Agentic alerts (AI-powered analysis)

These run the AI agent at trigger time with a stored prompt. The user wants intelligent output, not a static ping.

| User request | title | agent_prompt | deal_id | recurrence |
|-------------|-------|-------------|---------|------------|
| "At 8 PM, analyze the Acme deal" | Acme deal analysis | Analyze the current health, risks, and next steps for the Acme Corp deal | (resolve from deal search) | none |
| "Every morning at 8 AM, give me a pipeline summary" | Daily pipeline summary | List all active deals, highlight any with stale activity (no engagement in 7+ days), and flag top risks across the pipeline | — | daily |
| "Tomorrow at 5 PM, check if there's been any activity on the Plivo deal" | Plivo deal activity check | Check the Plivo deal for any new activity in the last 24 hours — meetings, emails, notes, or stage changes. Summarize what happened or flag if nothing has moved. | (resolve from deal search) | none |
| "Schedule a weekly MEDPIC assessment for the Databricks deal every Friday at 4 PM" | Databricks MEDPIC review | Run a full MEDPIC assessment for the Databricks deal. Score all six components with evidence. | (resolve from deal search) | weekly |
| "At 9 AM every weekday, summarize any deals closing this month" | Deals closing this month | List all deals with close dates in the current month. For each, show stage, amount, last activity date, and any risks. Highlight deals at risk of slipping. | — | weekdays |

## Ambiguous cases

When the request could go either way, use these tiebreakers:

| Signal | Classification |
|--------|---------------|
| "remind me to…" | Static — user wants a nudge, not analysis |
| "check…", "analyze…", "summarize…", "review…" | Agentic — user wants intelligent output |
| "tell me about…" + time | Agentic — user wants information delivered |
| "don't forget…" | Static — user wants a notification |
| No clear analysis verb | Static — default to safe option |

## Writing effective agent prompts

When creating agentic alerts, write prompts that are:
- **Self-contained**: include deal name, what to analyze, and expected output shape.
- **Specific**: "Analyze deal health for Acme Corp" not "Check Acme".
- **Actionable**: request concrete output (scores, risks, recommendations).

The agent at trigger time has full tool access but no conversation history — the prompt is its only context.
