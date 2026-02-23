# Scheduled Alerts

## Overview

The Scheduled Alerts system makes the Teams bot **proactive** — instead of only responding when a user asks, it can send messages and run AI tasks at scheduled times.

There are two types of alerts:

| Alert Type | What It Does |
|------------|-------------|
| **Static Alert** | Delivers a fixed text message at the scheduled time. Simple notification — "remind me to review the Acme proposal at 6 PM." |
| **Agentic Alert** | Triggers the GeneralAgent **in the background** at the scheduled time. The agent runs the stored prompt (with full tool access — deal info, meetings, notes, research), generates an answer, and delivers the result as an Adaptive Card. Think: "every morning at 8 AM, give me an analysis of deal Acme Corp." |

The key difference: a **static alert** delivers a message you wrote; an **agentic alert** triggers the bot to **generate a fresh answer** at that moment, exactly as if you had typed the question in chat — but automatically and on schedule.

Both types support one-shot and recurring schedules (daily, weekdays, weekly, biweekly, monthly).

---

## 1. Data Model

### Table: `user_reminders` (per org schema)

```
UserReminder
├── reminder_id          UUID PK
├── organisation_id      UUID (indexed)
├── user_id              UUID (indexed)
├── reminder_type        "static" | "agentic"
├── title                Short label
├── message_content      Text for static reminders (nullable)
├── agent_config         JSONB for agentic: {prompt, deal_id} (nullable)
├── next_trigger_at      Timestamp with timezone (indexed with status)
├── user_timezone        IANA timezone string (e.g. "America/New_York")
├── recurrence           "none" | "daily" | "weekdays" | "weekly" | "biweekly" | "monthly"
├── recurrence_end_at    Stop recurring after this time (nullable)
├── delivery_channel     "teams" (extensible to other channels)
├── delivery_context     JSONB: {service_url, conversation_id}
├── status               "active" | "triggered" | "completed" | "failed" | "cancelled"
├── last_triggered_at    When it last fired (nullable)
├── trigger_count        How many times it has fired
├── consecutive_failures Failure counter (resets on success)
├── last_error           Last error message (nullable)
├── last_result          JSONB: truncated agent output for debugging (nullable)
├── created_at           Auto
└── updated_at           Auto
```

### Status Lifecycle

```
                  ┌──────────────────────────────────────────┐
                  │                                          │
                  ▼                                          │
  (created) → ACTIVE ──(scheduler picks up)──→ TRIGGERED    │
                  ▲                               │         │
                  │                    ┌──────────┼──────┐  │
                  │                    ▼          ▼      │  │
                  │              (success)    (failure)  │  │
                  │                 │             │      │  │
                  │         ┌──────┴──┐    ┌─────┴───┐  │  │
                  │         │ one-shot│    │ retries  │  │  │
                  │         │         │    │ < 3      │  │  │
                  │         ▼         │    ▼          │  │  │
                  │     COMPLETED     │  ACTIVE       │  │  │
                  │                   │  (try again)──┘  │  │
                  │         ┌─────────┘                  │  │
                  │         │ recurring                  │  │
                  │         ▼                            │  │
                  │  compute next_trigger_at             │  │
                  │         │                            │  │
                  │         └── ACTIVE ──────────────────┘  │
                  │                                         │
                  │         ┌── (failures >= 3) ──→ FAILED  │
                  │         │                               │
                  └─────────┘   (user cancels) ──→ CANCELLED
```

### Indexes

| Index | Columns | Purpose |
|-------|---------|---------|
| `ix_user_reminders_org_status` | `(organisation_id, status)` | Per-org active alert queries |
| `ix_user_reminders_next_trigger` | `(next_trigger_at, status)` | Scheduler: find due alerts efficiently |
| `ix_user_reminders_user_status` | `(user_id, status)` | List user's alerts |

---

## 2. How Alerts Get Created

Alerts are created through **agent tools** during a Teams conversation. The user speaks naturally, the GeneralAgent interprets the intent, and calls the appropriate tool.

### Example Conversations

**Static Alert** — user wants a simple notification:
```
User: "Remind me at 6 PM to review the Acme proposal"
Agent: calls CreateReminder(
    title="Review the Acme proposal",
    trigger_time="2026-02-14T23:00:00+00:00",  (6 PM EST → UTC)
    reminder_type="static",
    message="Review the Acme proposal",
    timezone_name="America/New_York"
)
→ "Alert set: Review the Acme proposal at 2026-02-14 23:00 UTC"
```

At 6 PM, the bot delivers: **"Reminder: Review the Acme proposal"** — the exact message the user wrote.

**Agentic Alert** — user wants the bot to do work at a scheduled time:
```
User: "Every morning at 8 AM, give me an analysis of deal Acme Corp"
Agent: calls CreateReminder(
    title="Daily Acme Corp analysis",
    trigger_time="2026-02-14T13:00:00+00:00",  (8 AM EST → UTC)
    reminder_type="agentic",
    agent_prompt="Give me a comprehensive analysis of deal Acme Corp",
    deal_id="<uuid>",
    recurrence="daily",
    timezone_name="America/New_York"
)
→ "Scheduled Alert set: Daily Acme Corp analysis at 2026-02-14 13:00 UTC (repeats daily)"
```

At 8 AM the next morning, the bot **runs the GeneralAgent in the background** with the stored prompt. The agent has full tool access — it can look up deal details, recent meetings, stakeholder info, notes, risk/blockers — just as if the user had typed the question live. The agent's response is rendered as an Adaptive Card and delivered proactively to the user's Teams chat.

### Agent Tools

| Tool | Purpose | Parameters |
|------|---------|------------|
| `CreateReminder` | Create a new alert (static or agentic) | title, trigger_time, reminder_type, message, agent_prompt, deal_id, recurrence, timezone_name |
| `ListReminders` | Show user's active alerts | include_completed, limit |
| `CancelReminder` | Cancel an alert by reminder_id | reminder_id |

All three tools are restricted to the **Teams channel only** (registered in `CHANNEL_ONLY_TOOLS`). They won't appear when the user chats via the web UI.

### Tool Security

Tools receive the `AgentContext` which carries immutable security boundaries:

- `organisation_id` — queries are always scoped to the user's org
- `user_id` — users can only see/cancel their own alerts
- `metadata.service_url` / `metadata.conversation_id` — delivery context captured from the Teams activity, not from user input

The `delivery_context` is extracted from `context.metadata` (set by `TeamsBotService` when enqueuing the message), not from form data. This prevents spoofing.

---

## 3. How Due Alerts Fire (Scheduler)

### Three-Tier Celery Architecture

The scheduler follows the same three-tier pattern used by `stale_job_cleanup` in the knowledge worker:

```
┌─────────────────────────────────────────────────────────────────────┐
│  Celery Beat (every 60 seconds)                                     │
│  "chat.check_due_reminders_all_orgs"                                │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
            Tier 1: Coordinator (no retries)
            Queries all approved orgs from master schema
                               │
            ┌──────────────────┼──────────────────┐
            ▼                  ▼                  ▼
┌───────────────────┐ ┌───────────────────┐ ┌───────────────────┐
│ Tier 2: Org A     │ │ Tier 2: Org B     │ │ Tier 2: Org C     │
│ "chat.check_due_  │ │                   │ │                   │
│  reminders_org"   │ │  (same task)      │ │  (same task)      │
│                   │ │                   │ │                   │
│ Queries:          │ │                   │ │                   │
│ WHERE status=     │ │                   │ │                   │
│   'active' AND    │ │                   │ │                   │
│   next_trigger_at │ │                   │ │                   │
│   <= now()        │ │                   │ │                   │
│ LIMIT 50          │ │                   │ │                   │
└─────┬──────┬──────┘ └───────────────────┘ └───────────────────┘
      │      │
      ▼      ▼
┌──────────┐ ┌──────────┐
│ Tier 3:  │ │ Tier 3:  │
│ Execute  │ │ Execute  │
│ Reminder │ │ Reminder │
│ #1       │ │ #2       │
└──────────┘ └──────────┘
```

### Tier Details

| Tier | Task Name | Queue | Retries | Time Limit | Purpose |
|------|-----------|-------|---------|------------|---------|
| 1 | `chat.check_due_reminders_all_orgs` | `chat_processing` | 0 (never) | 2 min | Find all approved orgs, dispatch Tier 2 |
| 2 | `chat.check_due_reminders_org` | `chat_processing` | 2 (30s, 60s) | 2 min | Find due reminders in one org, dispatch Tier 3 |
| 3 | `chat.execute_reminder` | `chat_messages` | 2 (30s, 60s) | 5 min | Load, execute, and update one reminder |

**Why `chat_messages` for Tier 3**: Execute tasks share the same queue as bot message processing because they use the same infrastructure (GeneralAgent, TeamsAdapter, proactive messaging). This ensures they respect the same concurrency limits.

### Queue Routing (config.py)

```python
"chat.check_due_reminders_all_orgs": {"queue": "chat_processing", ...}
"chat.check_due_reminders_org":      {"queue": "chat_processing", ...}
"chat.execute_reminder":             {"queue": "chat_messages", ...}
```

---

## 4. Execution Flow

### 4.1 Static Alert Execution

A static alert simply delivers the stored message — no AI involved.

```
execute_reminder_task(org_id, reminder_id)
│
├── 1. _load_and_lock(reminder_id)
│       ├── SELECT ... WHERE status='active' FOR UPDATE SKIP LOCKED
│       ├── Dedup check: skip if last_triggered_at within 2 minutes
│       ├── Set status = 'triggered'
│       ├── COMMIT
│       └── Extract all fields into a dict, release session
│
├── 2. _execute_static(data)    (no DB session held)
│       ├── Build message: "**Reminder:** {title}\n\n{message_content}"
│       ├── TeamsBotService().send_proactive_message(
│       │       service_url, conversation_id, message
│       │   )
│       └── Return success/failure
│
└── 3. _mark_success(reminder_id, data)
        ├── Acquire fresh session
        ├── Set last_triggered_at = now(), trigger_count += 1
        ├── If one-shot (recurrence=none): status = 'completed'
        ├── If recurring: compute next_trigger_at, keep status = 'active'
        └── COMMIT, release session
```

### 4.2 Agentic Alert Execution

An agentic alert **triggers the full bot agent pipeline in the background**. It's as if the user typed the stored prompt into the Teams chat at that exact moment — the GeneralAgent runs, calls whichever tools it needs, generates a response, and delivers it.

```
execute_reminder_task(org_id, reminder_id)
│
├── 1. _load_and_lock(reminder_id)
│       (same as static — acquire, lock, extract, release)
│
├── 2. _execute_agentic(data)    (no DB session held during agent run)
│       │
│       │   ┌─────────────────────────────────────────────────────┐
│       │   │  This is the "bot runs in the background" step.     │
│       │   │  The agent has full tool access — same as a live    │
│       │   │  chat. It can query deals, meetings, notes,         │
│       │   │  stakeholders, risks, research — anything the       │
│       │   │  GeneralAgent normally can.                         │
│       │   └─────────────────────────────────────────────────────┘
│       │
│       ├── Build AgentContext(
│       │       organisation_id, deal_id, user_id,
│       │       channel="teams",
│       │       metadata={service_url, conversation_id, source="scheduled_alert"}
│       │   )
│       ├── messages = [{"role": "user", "content": agent_config["prompt"]}]
│       ├── Run GeneralAgent via run_agent_for_chat()
│       │       → Agent reads prompt, decides which tools to call
│       │       → Tools acquire their own DB sessions on demand
│       │       → Agent generates a comprehensive answer
│       ├── Accumulate response frames
│       ├── AgentResponse.from_frames(frames) → structured response
│       ├── TeamsAdapter.render(response) → Adaptive Card payload
│       ├── TeamsBotService().send_proactive_payload(
│       │       service_url, conversation_id, payload
│       │   )
│       └── Store truncated result in last_result (debugging)
│
└── 3. _mark_success / _mark_failure
        (same as static)
```

The agentic execution reuses the **exact same pipeline** as live bot messages (`BotMessageProcessingService._run_agent_and_accumulate()`). The agent can call any tool available to the GeneralAgent, including deal lookups, meeting history, stakeholder details, and research tools.

**In other words**: when an agentic alert fires, the bot "wakes up", processes the stored prompt as a new conversation, and delivers the answer — all without the user having to type anything.

### 4.3 Failure Handling

```
execute_reminder_task
│
├── Agent/delivery exception caught
│       └── _mark_failure(reminder_id, error_msg)
│               ├── consecutive_failures += 1
│               ├── last_error = error_msg
│               ├── If consecutive_failures >= 3:
│               │       status = 'failed' (circuit breaker)
│               └── Else: status = 'active' (retry on next tick)
│
└── Task-level exception (crash)
        └── Celery retry with exponential backoff: 30s, 60s
            └── After max_retries=2: return {status: "failed"}
```

There are two layers of retry:

1. **Application-level**: The alert stays `ACTIVE` after a failure and the next scheduler tick (60s) picks it up again. After 3 consecutive failures, it's marked `FAILED`.
2. **Task-level**: If the Celery task itself crashes (OOM, timeout, broker issue), Celery retries up to 2 times with exponential backoff.

---

## 5. Deduplication & Concurrency Safety

### Problem
Celery Beat fires every 60 seconds. If Tier 2 runs slowly or an alert takes >60s to execute (especially agentic alerts that run the full agent pipeline), the next scheduler tick could dispatch the same alert again.

### Solutions

1. **`SELECT FOR UPDATE SKIP LOCKED`** — Tier 3 locks the alert row with `SKIP LOCKED`. If another worker is already processing this alert, the second worker gets no row and returns `skipped`.

2. **2-minute dedup window** — Even if the lock has been released, `_load_and_lock()` checks `last_triggered_at`. If the alert was triggered within the last 2 minutes, it's skipped.

3. **Status gating** — Tier 2 only queries `status = 'active'`. Once Tier 3 sets `status = 'triggered'`, subsequent Tier 2 queries won't pick it up.

---

## 6. Recurring Alerts

### How Next Trigger Is Computed

After successful execution, the service computes the next trigger time **in the user's local timezone**, then converts back to UTC. This handles DST transitions correctly.

```python
# Example: Daily agentic alert at 8 AM US/Eastern
# Before DST change: next_trigger_at = 2026-03-08 13:00 UTC (8 AM EST = UTC-5)
# After DST change:  next_trigger_at = 2026-03-09 12:00 UTC (8 AM EDT = UTC-4)
```

### Recurrence Rules

| Pattern | Logic |
|---------|-------|
| `daily` | Add 1 day |
| `weekdays` | Add 1 day, skip Sat/Sun |
| `weekly` | Add 7 days |
| `biweekly` | Add 14 days |
| `monthly` | Add 1 month (via `dateutil.relativedelta`) |

### End Date

If `recurrence_end_at` is set and the computed next trigger exceeds it, the alert is marked `COMPLETED` instead of rescheduled.

---

## 7. Cancellation

Users can cancel alerts in two ways:

### Via Agent Tool (Conversational)
```
User: "Cancel my daily pipeline alert"
Agent: calls ListReminders() → shows results
Agent: calls CancelReminder(reminder_id="...")
→ "Alert Daily pipeline check has been cancelled."
```

### Via Adaptive Card Button
When `ListReminders` renders results as Adaptive Cards (rich format), each card includes a "Cancel" button. Clicking it submits a form with `action="cancel_reminder_submit"` and the `reminder_id`. The `CancelReminderFormHandler` processes this submission.

The form handler resolves the user from **Teams identity headers** (not form metadata) for security, then verifies the alert belongs to that user before cancelling.

---

## 8. Database Session Discipline

This feature follows the project's fresh session pattern strictly:

| Operation | Session Strategy |
|-----------|-----------------|
| **Tool execution** (create/list/cancel) | `context.acquire_session()` — released automatically |
| **Tier 1** (coordinator) | `acquire_master_async_session()` — released before dispatching |
| **Tier 2** (per-org query) | `acquire_org_async_session()` — released before dispatching |
| **Tier 3 load** | `acquire_org_async_session()` + `FOR UPDATE` — released after extracting data |
| **Agent execution** | No session held — tools acquire on demand |
| **Tier 3 update** | Fresh `acquire_org_async_session()` after execution completes |

The critical insight: during agentic execution, the GeneralAgent may make multiple LLM calls and tool invocations that take 30+ seconds. No DB session is held during this entire span.

---

## 9. Configuration

### Beat Schedule

```python
# In ChatWorkerConfig (src/workers/chat_worker/config.py)
self.beat_schedule = {
    "check-due-reminders": {
        "task": "chat.check_due_reminders_all_orgs",
        "schedule": 60.0,  # Every 60 seconds
        "options": {"queue": "chat_processing", "routing_key": "chat_processing"},
    },
}
```

### Constants

| Constant | Value | Location |
|----------|-------|----------|
| `MAX_CONSECUTIVE_FAILURES` | 3 | `reminder_execution_service.py` |
| Tier 2 query limit | 50 alerts per org per tick | `reminder_tasks.py` |
| Dedup window | 2 minutes | `reminder_execution_service.py` |
| Tier 3 soft time limit | 5 minutes | `reminder_tasks.py` |
| Tier 3 max retries | 2 | `reminder_tasks.py` |

---

## 10. Code Walkthrough

Read the files in this order to understand the full implementation from data model to execution:

### Step 1: Data Model
**`src/domain/models/organisation_tables/reminder_models.py`**
Start here. Defines the three enums (`ReminderType`, `ReminderRecurrence`, `ReminderStatus`) and the `UserReminder` SQLModel table. All field definitions, indexes, and server defaults live here.

### Step 2: Database Migration
**`migrations/org_migrations/versions/3c7f79637798_030_user_reminders.py`**
The Alembic migration that creates the `user_reminders` table in every `org_*` schema. Shows the exact column types, server defaults, and index definitions.

### Step 3: Agent Tools — Creating Alerts
**`src/agents/tools/reminders/create_reminder.py`**
The `create_reminder` function validates all inputs (type, recurrence, trigger time, content), extracts delivery context from `context.metadata`, builds the `UserReminder` record, and persists it. The factory function `create_create_reminder()` wraps it with the tool docstring that the LLM sees.

### Step 4: Agent Tools — Listing Alerts
**`src/agents/tools/reminders/list_reminders.py`**
Queries the user's active alerts, scoped by `organisation_id` and `user_id`. Returns structured data (id, title, type, status, next trigger, recurrence, timezone).

### Step 5: Agent Tools — Cancelling Alerts
**`src/agents/tools/reminders/cancel_reminder.py`**
Validates ownership (org + user), sets `status = CANCELLED`. The factory docstring instructs the LLM to call `ListReminders` first so the user can pick which one to cancel.

### Step 6: Tool Registration
**`src/agents/tools/registry.py`** (modified sections)
Search for `REMINDERS` and `CREATE_REMINDER` to find the additions:
- `ToolCategory.REMINDERS` enum value
- Three `ToolName` entries: `CREATE_REMINDER`, `LIST_REMINDERS`, `CANCEL_REMINDER`
- All three in `CHANNEL_ONLY_TOOLS` restricted to `{"teams"}`
- All three in `AGENT_TOOLS["GeneralAgent"]`
- All three in `TOOL_CATEGORIES` mapping
- Factory registrations in `_register_tool_factories()`

### Step 7: Agent Prompt
**`src/agents/workflows/general/prompts/general_prompt.py`** (modified sections)
Search for "Reminders & Scheduled Alerts" to find the prompt additions. This section teaches the LLM when and how to use the three alert tools, including examples of static vs agentic, timezone handling, and the tool call workflow.

### Step 8: Three-Tier Celery Tasks
**`src/workers/chat_worker/tasks/reminder_tasks.py`**
The three task functions:
- `check_due_reminders_all_orgs_task()` — Tier 1 coordinator
- `check_due_reminders_org_task()` — Tier 2 per-org query
- `execute_reminder_task()` — Tier 3 per-alert execution

### Step 9: Execution Service
**`src/workers/chat_worker/services/reminder_execution_service.py`**
The core business logic. `ReminderExecutionService` handles:
- `_load_and_lock()` — row locking and dedup
- `_execute_static()` — plain text delivery via `TeamsBotService`
- `_execute_agentic()` — full agent pipeline in background (AgentContext → GeneralAgent → TeamsAdapter → delivery)
- `_mark_success()` / `_mark_failure()` — status updates and recurrence computation
- `_compute_next_trigger()` — DST-aware timezone math

### Step 10: Worker Configuration
**`src/workers/chat_worker/config.py`** (modified sections)
The task routes that map task names to queues, and the `beat_schedule` that fires the Tier 1 coordinator every 60 seconds.

### Step 11: Cancel Form Handler
**`src/service/bots/form_handlers/cancel_reminder_handler.py`**
Handles the `cancel_reminder_submit` action from interactive Adaptive Card buttons. Resolves the user from Teams identity headers (not form data), validates ownership, and sets `status = CANCELLED`.

### Step 12: Form Handler Registration
**`src/service/bots/form_handlers/__init__.py`** (modified lines)
Registers `CancelReminderFormHandler` for the `cancel_reminder_submit` action and a generic `CancelFormHandler` for `cancel_reminder_cancel` (dismiss).

---

## 11. Testing Checklist

1. **Create static alert**: Via Teams bot, say "remind me in 5 minutes to check email". Verify `user_reminders` row created with `reminder_type='static'`, correct `next_trigger_at` and `delivery_context`.
2. **Create agentic alert**: Say "in 5 minutes, analyze deal Acme Corp". Verify `reminder_type='agentic'`, `agent_config` has the prompt and deal_id.
3. **Scheduler fires**: Wait for `next_trigger_at` to pass. Check Celery logs for Tier 1 → Tier 2 → Tier 3 progression.
4. **Static delivery**: Verify the bot sends a plain text proactive message in the Teams conversation.
5. **Agentic delivery**: Verify the bot runs the agent in the background, then sends an Adaptive Card with the agent's analysis.
6. **Recurring**: Create a daily alert. After it fires, verify `next_trigger_at` advanced by 1 day and `status` returned to `active`.
7. **Cancellation (conversational)**: Say "cancel my alerts". Agent should call ListReminders then CancelReminder.
8. **Cancellation (card button)**: Click Cancel on an alert card. Verify `status = cancelled`.
9. **Failure circuit breaker**: Force 3 consecutive failures (e.g. invalid service_url). Verify status becomes `failed`.
10. **Dedup**: Manually dispatch the same alert twice. Second execution should return `skipped`.
11. **Org isolation**: Verify a user cannot see/cancel another org's alerts.
