# Slack & Teams Task Creation via Agent Tool — Design Document

**Date:** 2026-03-14
**Status:** Draft
**Depends on:** CRM Tasks Feature (`2026-03-11-crm-tasks-feature-design.md`)

---

## 1. Overview

Allow users to create tasks from Slack and Teams chat using natural language, handled by the existing GeneralAgent + LLM pipeline. The LLM extracts structured fields from the user's message and calls a new `create_task` agent tool — same pattern as the existing `create_reminder` tool.

### Example Interactions

```
User: "Create a task to follow up with Acme procurement team by Friday, high priority"
→ LLM extracts: title, due_date, priority
→ Calls create_task tool
→ Sends preview card → user confirms → task created

User: "Add a task for the Globex deal — send updated proposal, due March 20"
→ LLM extracts: title, deal reference, due_date
→ Calls create_task tool with deal_id
→ Preview card → confirm → task linked to deal

User: "Todo: update CRM notes for today's call"
→ LLM extracts: title (no due date, no deal)
→ Quick task, no due date
```

### Why Agent Tool (Not Slash Commands / Modals)

- The LLM agent already processes every Slack/Teams chat message
- Natural language is faster than filling a modal form
- Follows the proven `create_reminder` pattern
- No Slack app configuration changes needed (no new slash commands, shortcuts, or OAuth scopes)
- Works identically on both Teams and Slack

---

## 2. Tool Design

### 2.1 Tool Signature

```python
async def create_task(
    *,
    context: AgentContext,
    title: str,                          # Required — task subject
    description: str | None = None,      # Optional — task body
    priority: str = "MEDIUM",            # LOW, MEDIUM, HIGH, URGENT
    due_date: str | None = None,         # Natural language or ISO 8601
    deal_name: str | None = None,        # Deal name for fuzzy lookup
    deal_id: str | None = None,          # Direct deal UUID (if known)
    reminder: str | None = None,         # "at_due", "15m_before", "1h_before", "1d_before"
) -> dict[str, Any]:
```

### 2.2 LLM Tool Description

```
Create a task or to-do item for the user.

Use this tool when the user asks to create a task, add a to-do,
or track an action item. Extract the following from their message:

- title: Short description of what needs to be done (required)
- description: Additional details or context (optional)
- priority: LOW, MEDIUM (default), HIGH, or URGENT
- due_date: When it's due — pass the user's time expression exactly
  as they said it. Examples: "Friday", "March 20", "end of week",
  "2026-03-20T17:00:00". If not mentioned, omit.
- deal_name: If the user mentions a deal by name, pass it here for lookup.
  Do NOT guess — only pass if the user explicitly mentions a deal.
- deal_id: If you already know the deal UUID from context (e.g. deal-scoped
  chat), pass it directly. Takes precedence over deal_name.
- reminder: Set if user asks for a reminder. Options:
  "at_due" (at due time), "15m_before", "1h_before", "1d_before".
  Requires due_date to be set.

The tool sends a preview card for the user to confirm before creating.
```

### 2.3 Tool Flow

```
LLM calls create_task(title="Follow up with Acme", due_date="Friday", priority="HIGH")
    ↓
1. Validate inputs (title required, priority valid)
    ↓
2. Resolve due_date (same hybrid parser as create_reminder: ISO 8601 → dateparser)
    ↓
3. Resolve deal (if deal_name provided → fuzzy search in user's deals)
    ↓
4. Build preview card (Adaptive Card for Teams, Block Kit for Slack)
    ↓
5. Send preview card to chat channel
    ↓
6. Return { status: "preview_sent", message: "Please confirm..." }
    ↓
[User clicks Confirm]
    ↓
7. ConfirmTaskFormHandler receives action
    ↓
8. Creates CRMActivity via TasksWriteService.create_task()
    ↓
9. Optionally creates linked UserReminder
    ↓
10. Sends confirmation message
```

### 2.4 Why Preview Card (Not Direct Creation)

Following the `create_reminder` pattern: the tool creates a **pending preview** and the user must click **Confirm** to finalize. This prevents:
- Accidental task creation from misunderstood intent
- Wrong deal/date/priority from LLM extraction errors
- Gives user a chance to review before committing

Unlike reminders (which use `pending_confirmation` status in DB), tasks don't need a DB row until confirmed — the preview card carries all the data in its action payload.

---

## 3. Preview Card Design

### 3.1 Card Content

| Field | Display |
|-------|---------|
| Title | Task subject |
| Priority | LOW / MEDIUM / HIGH / URGENT (with color indicator) |
| Due Date | Formatted in user's timezone (if provided) |
| Deal | Deal name (if linked) |
| Description | Truncated preview (if provided) |
| Reminder | "1 hour before due" etc. (if set) |

### 3.2 Slack Block Kit Preview

```json
[
  {
    "type": "header",
    "text": { "type": "plain_text", "text": "Task Preview" }
  },
  {
    "type": "section",
    "fields": [
      { "type": "mrkdwn", "text": "*Title*\nFollow up with Acme" },
      { "type": "mrkdwn", "text": "*Priority*\nHigh" },
      { "type": "mrkdwn", "text": "*Due*\nMar 20, 2026 5:00 PM IST" },
      { "type": "mrkdwn", "text": "*Deal*\nAcme Security Rollout" }
    ]
  },
  {
    "type": "context",
    "elements": [
      { "type": "mrkdwn", "text": "Please confirm or cancel this task." }
    ]
  },
  {
    "type": "actions",
    "elements": [
      {
        "type": "button",
        "text": { "type": "plain_text", "text": "Confirm" },
        "style": "primary",
        "action_id": "confirm_task_submit",
        "value": "{\"title\":\"...\",\"priority\":\"HIGH\",...}"
      },
      {
        "type": "button",
        "text": { "type": "plain_text", "text": "Cancel" },
        "style": "danger",
        "action_id": "confirm_task_cancel",
        "value": "{...}"
      }
    ]
  }
]
```

### 3.3 Teams Adaptive Card Preview

Same structure as the Slack version but using Adaptive Card format (FactSet + Action.Execute), following the `create_reminder` pattern.

### 3.4 Action Payload

The Confirm button carries the full task payload as a JSON string in the button `value`:

```json
{
  "title": "Follow up with Acme",
  "description": null,
  "priority": "HIGH",
  "due_date": "2026-03-20T17:00:00Z",
  "deal_id": "uuid-or-null",
  "deal_name": "Acme Security Rollout",
  "reminder_option": "1h_before",
  "user_timezone": "Asia/Kolkata",
  "organisation_id": "uuid"
}
```

No DB row is created until the user confirms — all state lives in the card payload.

---

## 4. Form Handlers

### 4.1 ConfirmTaskFormHandler

**Action:** `confirm_task_submit`

```python
class ConfirmTaskFormHandler(BaseFormHandler):
    async def handle(self, activity: dict, form_data: dict) -> FormHandlerResult:
        # 1. Resolve user from activity
        user, _ = await resolve_user_from_activity(activity)

        # 2. Parse task payload from button value
        title, description, priority, due_date, deal_id, reminder_option = ...

        # 3. Build TasksWriteService payload
        payload = {
            "subject": title,
            "body": description,
            "priority": priority,
            "due_date": due_date,
        }

        # 4. Add reminder config if requested
        if reminder_option and due_date:
            trigger_time = compute_reminder_trigger(due_date, reminder_option)
            payload["reminder"] = {
                "enabled": True,
                "trigger_time": trigger_time,
                "recurrence": "NONE",
                "delivery_channel": channel,  # teams or slack
                "delivery_context": {...},
            }

        # 5. Create task via TasksWriteService
        async for session in acquire_org_async_session(org_id):
            service = TasksWriteService(session, org_id, user.user_id)
            task = await service.create_task(payload, deal_id=deal_id)
            await session.commit()

        # 6. Return confirmation
        return FormHandlerResult(
            success=True,
            message=f"Task **{title}** created successfully!",
        )
```

### 4.2 CancelTaskFormHandler

**Action:** `confirm_task_cancel`

Simple handler — no DB cleanup needed since no row was created:

```python
class CancelTaskFormHandler(BaseFormHandler):
    async def handle(self, activity, form_data) -> FormHandlerResult:
        return FormHandlerResult(success=True, message="Task creation cancelled.")
```

---

## 5. Deal Resolution

When the user mentions a deal by name ("for the Acme deal"), the tool needs to resolve it to a `deal_id`.

### Strategy: Fuzzy Name Search

```python
async def _resolve_deal(
    session: AsyncSession,
    org_id: UUID,
    deal_name: str,
) -> tuple[UUID, str] | None:
    """Find a deal by fuzzy name match. Returns (deal_id, deal_name) or None."""
    # Case-insensitive ILIKE search
    stmt = select(CRMDeal).where(
        col(CRMDeal.organisation_id) == org_id,
        col(CRMDeal.name).ilike(f"%{deal_name}%"),
        col(CRMDeal.is_deleted) == False,
    ).limit(5)

    results = await session.execute(stmt)
    deals = results.scalars().all()

    if len(deals) == 1:
        return (deals[0].deal_id, deals[0].name)
    elif len(deals) > 1:
        # Multiple matches — include deal names in preview for user to verify
        # Pick the best match (exact > starts_with > contains)
        ...
    return None
```

**If no match:** Tool creates the task without a deal link. The preview card shows "Deal: Not found — '{deal_name}'" so the user can cancel if it matters.

**If multiple matches:** Pick the closest match (exact > starts-with > contains). Show the resolved name in the preview card so the user can verify.

---

## 6. Due Date Parsing

Reuse the same `_resolve_trigger_time()` function from `create_reminder.py`:
- ISO 8601 fast path: `"2026-03-20T17:00:00"`
- Natural language via dateparser: `"Friday"`, `"end of week"`, `"March 20"`
- Timezone-aware using user's timezone from context metadata

**Difference from reminders:** Due dates CAN be in the past (tasks can be overdue). Only reminder trigger times must be in the future.

---

## 7. Reminder Offset Calculation

When the user requests a reminder relative to due date:

```python
def _compute_reminder_trigger(
    due_date: datetime,
    reminder_option: str,
) -> datetime:
    """Compute reminder trigger time from due date and offset option."""
    offsets = {
        "at_due": timedelta(0),
        "15m_before": timedelta(minutes=15),
        "1h_before": timedelta(hours=1),
        "1d_before": timedelta(days=1),
    }
    offset = offsets.get(reminder_option, timedelta(0))
    return due_date - offset
```

If the computed reminder time is in the past, skip reminder creation and note it in the confirmation message.

---

## 8. Registry Integration

### 8.1 ToolName Enum

Add to `src/agents/tools/registry.py`:

```python
class ToolName(str, Enum):
    ...
    # TASK TOOLS
    CREATE_TASK = "create_task"
```

### 8.2 Channel Restriction

```python
CHANNEL_ONLY_TOOLS: dict[ToolName, set[str]] = {
    ...
    ToolName.CREATE_TASK: {"teams", "slack"},
}
```

Tasks are only created via chat channels (Teams/Slack), not the web chat — the web UI has its own task creation form.

### 8.3 GeneralAgent Tool List

```python
AGENT_TOOLS = {
    "GeneralAgent": [
        ...
        ToolName.CREATE_TASK,
    ],
}
```

### 8.4 Tool Factory

```python
self._tool_factories[ToolName.CREATE_TASK] = lambda: create_create_task(
    self.context, self.capabilities_getter
)
```

### 8.5 Category Mapping

```python
TOOL_CATEGORIES = {
    ...
    ToolName.CREATE_TASK: ToolCategory.CRM,
}
```

---

## 9. Files to Create / Modify

| File | Action | Description |
|------|--------|-------------|
| `src/agents/tools/tasks/create_task.py` | **Create** | Tool implementation: validate, resolve deal/date, send preview card |
| `src/agents/tools/tasks/__init__.py` | **Create** | Package init |
| `src/agents/tools/registry.py` | **Modify** | Add CREATE_TASK to ToolName, CHANNEL_ONLY_TOOLS, AGENT_TOOLS, TOOL_CATEGORIES, factory registration |
| `src/service/bots/form_handlers/confirm_task_handler.py` | **Create** | ConfirmTaskFormHandler + CancelTaskFormHandler |
| `src/service/bots/form_handlers/__init__.py` | **Modify** | Register confirm_task_submit and confirm_task_cancel handlers |

---

## 10. Security

| Concern | Mitigation |
|---------|------------|
| Cross-org access | `organisation_id` from immutable AgentContext — cannot be overridden |
| User scoping | `user_id` from AgentContext — task assigned to this user |
| Deal ownership | Deal resolved only within the user's org schema |
| Confirmation | Preview card prevents accidental creation from LLM misinterpretation |
| Payload tampering | ConfirmTaskFormHandler re-validates org ownership via `resolve_user_from_activity()` |

---

## 11. What's NOT in Scope

- **List/update/delete tasks from chat** — future tools, same pattern
- **Deal-scoped auto-linking** — when chatting in a deal context, auto-set deal_id (future enhancement)
- **Assignment to other users** — self-assignment only via chat; reassign from web UI
- **Slash commands / modals** — may add later as a complementary UX
- **Web channel** — web chat already has the task creation UI

---

## 12. Implementation Order

1. **Tool implementation** (`create_task.py`) — core logic, preview card builders
2. **Form handlers** (`confirm_task_handler.py`) — confirm/cancel actions
3. **Registry wiring** — ToolName, factory, GeneralAgent tool list
4. **Handler registration** — FormHandlerRegistry entries
5. **Test** — unit tests for tool + form handlers
