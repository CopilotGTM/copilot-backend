---
name: reminders
description: Create, list, and cancel reminders and scheduled AI-powered alerts. Use when the user asks to set a reminder, schedule an alert, manage existing reminders, or asks about reminder capabilities. Covers static reminders (fixed messages) and agentic alerts (AI analysis at trigger time).
---

# Reminders & Scheduled Alerts

Help the user create, view, and manage reminders. Classify each request correctly and call the right tool with the right parameters.

## Reminder types

There are two types. Classify based on user intent:

| Type | When to use | Key parameter |
|------|-------------|---------------|
| **Static** (`reminder_type="static"`) | User wants a fixed notification delivered at a specific time. | `message` — the text to deliver |
| **Agentic** (`reminder_type="agentic"`) | User wants an AI analysis or task executed at a specific time. | `agent_prompt` — the instruction for the agent to run |

**Classification rules:**
- "Remind me to…" / "Don't let me forget…" / "Ping me about…" → **Static**
- "Analyze … at …" / "Give me a report on … tomorrow" / "Check the deal at …" / "Run a health check at …" → **Agentic**
- If unclear, default to **Static** with the user's words as the message.

## Use this workflow

### Creating a reminder

1. **Extract parameters** from the user's message:
   - `title`: short label (e.g., "Follow up with Acme Corp"). Infer from context if not stated.
   - `trigger_time`: pass the user's time expression **exactly as stated** (e.g., "at 3 PM", "tomorrow at 9 AM", "in 30 minutes"). Do NOT convert to UTC or calculate offsets.
   - `reminder_type`: "static" or "agentic" based on classification rules above.
   - `message`: required for static — use the user's intended notification text.
   - `agent_prompt`: required for agentic — write a clear instruction for the agent to execute at trigger time.
   - `deal_id`: for agentic alerts, include if the analysis targets a specific deal. Search for the deal first if the user mentions a deal name.
   - `recurrence`: default "none". Set if user says "every day", "daily", "weekly", etc. See `references/recurrence-guide.md`.

2. If content, time, or reminder intent is missing, ask only for the missing piece:
   - Missing content: "What should I remind you about?"
   - Missing time: "When should I remind you?"
   - Generic intent/title such as "set a reminder": ask what the reminder is about.

3. **Call `CreateReminder`** once the mandatory fields are known. Do not ask the user for their timezone — the tool resolves relative dates from the stored user timezone and current datetime.

4. **Confirm to the user** that the reminder was created. No preview confirmation card is required in the normal path.

### Listing reminders

1. Call `ListReminders` when the user asks to see, check, or review their reminders.
2. Present results in a readable format: title, trigger time, type, recurrence, and status.
3. If the user wants to cancel one, proceed to the cancel workflow.

### Cancelling a reminder

1. If the user doesn't specify which reminder, call `ListReminders` first to show options.
2. Once identified, call `CancelReminder` with the `reminder_id`.
3. Confirm cancellation to the user.

## Parameter reference

| Parameter | Required | Default | Notes |
|-----------|----------|---------|-------|
| `title` | Yes | — | Short label, max 255 chars |
| `trigger_time` | Yes | — | Natural language or ISO 8601. Pass exactly as user states it. |
| `reminder_type` | No | "static" | "static" or "agentic" |
| `message` | Conditional | — | Required when `reminder_type="static"` |
| `agent_prompt` | Conditional | — | Required when `reminder_type="agentic"`. Write a clear, self-contained instruction. |
| `deal_id` | No | — | UUID of deal for agentic alerts. Resolve from deal name if needed. |
| `recurrence` | No | "none" | "none", "daily", "weekdays", "weekly", "biweekly", "monthly" |

## Critical rules

- **Never convert time to UTC** — pass the user's expression verbatim. The server resolves it.
- **Never ask for timezone when context has one** — it is auto-detected from the channel context.
- **Preserve local weekday intent** — phrases like "Saturday 10 PM" mean the user's local Saturday, not a UTC day boundary.
- **Always include content** — static needs `message`, agentic needs `agent_prompt`. Tool will error without them.
- **Create directly** — once mandatory fields are known, call `CreateReminder`; the reminder is active immediately.
- **Agentic prompts must be self-contained** — at trigger time, the agent runs independently. Include enough context (deal name, what to analyze) in the `agent_prompt`.

## Output contract

After creating: tell the user the reminder was created and summarize when it will run.
After listing: present a clean table or list of reminders.
After cancelling: confirm which reminder was cancelled.

Do not expose reminder UUIDs unless the user explicitly asks.

## Reference loading guide

- Use `references/recurrence-guide.md` for recurrence patterns and edge cases.
- Use `references/classification-examples.md` for static vs agentic classification examples.
- Use `references/agentic-prompt-patterns.md` for writing effective agentic alert prompts.
