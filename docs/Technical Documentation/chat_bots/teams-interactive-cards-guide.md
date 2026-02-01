# Teams Interactive Cards & Form Submission Guide

> **Last Updated:** 2026-02-01
> **Applies To:** Any feature that renders an interactive Adaptive Card in Teams and processes the user's submission.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture Layers](#2-architecture-layers)
3. [Action.Execute vs Action.Submit](#3-actionexecute-vs-actionsubmit)
4. [Building a New Interactive Card (Step-by-Step)](#4-building-a-new-interactive-card-step-by-step)
5. [Card Builder Rules](#5-card-builder-rules)
6. [Form Handler Rules](#6-form-handler-rules)
7. [Bot Service: Invoke Response Flow](#7-bot-service-invoke-response-flow)
8. [Webhook Handler: No Changes Needed](#8-webhook-handler-no-changes-needed)
9. [Data Flow Diagram](#9-data-flow-diagram)
10. [Backward Compatibility (Action.Submit Fallback)](#10-backward-compatibility-actionsubmit-fallback)
11. [Common Pitfalls](#11-common-pitfalls)
12. [File Reference](#12-file-reference)

---

## 1. Overview

When we need the user to fill out a form inside Teams (create a deal, add a note, edit settings, etc.) we use **Adaptive Cards**. The card is rendered by Teams, the user fills in the fields, and clicks a button. The submission is routed back to our webhook, processed, and the card is **replaced in-place** with a confirmation or error card so the user cannot re-submit.

**Key principle:** The form definition is **channel-agnostic**. Only the card builder and the bot service layer know about Teams specifics. The form handler (business logic) never touches Teams APIs or card JSON.

---

## 2. Architecture Layers

```
┌─────────────────────────────────────────────────────────────────┐
│  Agent / Service  (decides to show a form)                      │
│  Creates a FormDefinition with fields, actions, metadata        │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  Card Builder Layer       src/adapters/card_builders/           │
│                                                                 │
│  FormDefinition ──► TeamsFormCardBuilder.build() ──► Card JSON  │
│  (channel-agnostic)       (Teams-specific)                      │
└──────────────────────────┬──────────────────────────────────────┘
                           │  Card sent to Teams via Bot Framework
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  User clicks Submit in Teams                                    │
│  Teams sends invoke activity (name="adaptiveCard/action")       │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  Webhook Handler          src/api/routers/bots/                 │
│  teams_webhook_handler.py                                       │
│  → Validates auth, passes to TeamsBotService.process_activity() │
│  → Returns JSONResponse for invoke activities                   │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  Bot Service              src/service/bots/teams_bot_service.py │
│  _handle_invoke()                                               │
│    → Detects "adaptiveCard/action"                              │
│    → Calls _handle_adaptive_card_action()                       │
│      → Extracts verb + form_data from invoke payload            │
│      → Routes to FormHandlerRegistry.handle(verb, ...)          │
│      → Wraps result in replacement card (confirmation / error)  │
│      → Returns invoke response to Teams                         │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  Form Handler             src/service/bots/form_handlers/       │
│  BaseFormHandler.handle(activity, form_data)                    │
│  → Pure business logic (channel-agnostic)                       │
│  → Returns FormHandlerResult(success, message)                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Action.Execute vs Action.Submit

We use **`Action.Execute`** (Universal Actions) as the primary action type, with `Action.Submit` as a fallback for older Teams clients.

| Aspect | Action.Execute | Action.Submit |
|---|---|---|
| Activity type sent | `invoke` (`name="adaptiveCard/action"`) | `message` (with form data in `value`) |
| Card replacement | Yes — return a replacement card in the invoke response | No — card stays active, user can click again |
| Duplicate prevention | Built-in (card is replaced, buttons gone) | Not built-in (must handle server-side) |
| Adaptive Card version | Requires 1.4+ | Works with any version |
| How we use it | Primary action type | Fallback inside `"fallback"` key |

**Why this matters:** Without Action.Execute, clicking Submit leaves the card active. The user can click Submit again and create duplicate records. Action.Execute replaces the card in-place after submission, which is the Microsoft-recommended pattern.

### Invoke payload structure (Action.Execute)

```json
{
  "type": "invoke",
  "name": "adaptiveCard/action",
  "value": {
    "action": {
      "type": "Action.Execute",
      "verb": "create_deal_submit",
      "data": {
        "name": "Acme Corp Deal",
        "amount": "50000",
        "currency": "USD",
        "organisation_id": "..."
      }
    }
  }
}
```

- `verb` — the action identifier (replaces `data.action` from the old Action.Submit pattern)
- `data` — merged object of metadata (from the card definition) + input field values (from user input)

### Invoke response format (card replacement)

```json
{
  "statusCode": 200,
  "type": "application/vnd.microsoft.card.adaptive",
  "value": { "...replacement Adaptive Card JSON..." }
}
```

Teams replaces the original card with whatever card is in `value`. We return a card with **no action buttons**, so the user sees a final confirmation/error and cannot re-submit.

---

## 4. Building a New Interactive Card (Step-by-Step)

When you need to add a new interactive form (e.g. "Add Note", "Edit Deal", "Schedule Meeting"):

### Step 1: Define the form (channel-agnostic)

Create a `FormDefinition` wherever the form is triggered (agent tool, service, etc.):

```python
from src.adapters.card_builders.base import FormDefinition, FormField

form = FormDefinition(
    title="Add a Note",
    subtitle="Add a note to the deal timeline.",
    fields=[
        FormField(id="note_text", label="Note", field_type="text", required=True, multiline=True),
        FormField(
            id="note_type",
            label="Type",
            field_type="choice",
            choices=[
                {"title": "General", "value": "general"},
                {"title": "Follow-up", "value": "follow_up"},
            ],
            value="general",
        ),
    ],
    submit_action="add_note_submit",      # ← This becomes the verb
    submit_label="Add Note",
    cancel_action="add_note_cancel",
    cancel_label="Cancel",
    metadata={"organisation_id": str(org_id), "deal_id": str(deal_id)},
)
```

**Rules for `submit_action` / `cancel_action`:**
- Use a descriptive verb: `<entity>_<operation>_submit` / `<entity>_<operation>_cancel`
- Examples: `create_deal_submit`, `add_note_submit`, `edit_settings_submit`
- This string is used as both the `Action.Execute` verb and the `Action.Submit` fallback `data.action`

### Step 2: Build the card payload

```python
from src.adapters.card_builders import get_card_builder

builder = get_card_builder("teams")
payload = builder.build(form)
# payload is ready to send via send_proactive_payload()
```

### Step 3: Create the form handler

Create a new handler file in `src/service/bots/form_handlers/`:

```python
# src/service/bots/form_handlers/add_note_handler.py

from typing import Any
from src.service.bots.form_handlers.base import BaseFormHandler, FormHandlerResult

class AddNoteFormHandler(BaseFormHandler):
    async def handle(
        self,
        activity: dict[str, Any],
        form_data: dict[str, Any],
    ) -> FormHandlerResult:
        note_text = (form_data.get("note_text") or "").strip()
        if not note_text:
            return FormHandlerResult(success=False, message="Note text is required.")

        # ... business logic (resolve user, create note, etc.) ...

        return FormHandlerResult(success=True, message="Note added to the deal.")
```

**Handler rules:**
- Extend `BaseFormHandler`
- The handler is **channel-agnostic** — never import Teams-specific code
- Always resolve the user from the `activity` (don't trust `form_data` alone for identity)
- Always validate `organisation_id` against the user's actual org (security boundary)
- Return `FormHandlerResult(success=bool, message=str)`

### Step 4: Register the handler

In `src/service/bots/form_handlers/__init__.py`:

```python
from src.service.bots.form_handlers.add_note_handler import AddNoteFormHandler

# At module level (alongside existing registrations)
FormHandlerRegistry.register("add_note_submit", AddNoteFormHandler())
FormHandlerRegistry.register("add_note_cancel", CancelFormHandler("Note cancelled."))
```

### Step 5: Done — no other changes needed

The bot service layer (`_handle_adaptive_card_action`) and webhook handler are generic. They will automatically:
1. Route the `verb` to your registered handler
2. Wrap the `FormHandlerResult` in a confirmation or error replacement card
3. Return it to Teams for in-place card replacement

---

## 5. Card Builder Rules

**File:** `src/adapters/card_builders/teams_card_builder.py`

| Rule | Detail |
|---|---|
| Adaptive Card version | **1.4** — Universal Actions require 1.4+. Some Teams clients have issues with 1.5. |
| Action type | Always `Action.Execute` with `Action.Submit` in the `fallback` key |
| `verb` field | Set to `form.submit_action` — this is the action identifier for the invoke |
| `data` on Action.Execute | Contains only `form.metadata` (org_id, deal_id, etc.). Input field values are auto-merged by the Adaptive Card client at submission time. |
| `data` on Action.Submit fallback | Contains `{"action": "<submit_action>", ...metadata}` — the `action` key is needed because the legacy `_handle_form_submission` path reads `form_data.get("action")` |
| Confirmation/error cards | Use `build_confirmation_card()` / `build_error_card()` — these have **no action buttons**, making the card final |
| No channel logic in FormDefinition | `FormDefinition` and `FormField` are in `src/adapters/card_builders/base.py` and must stay channel-agnostic |

---

## 6. Form Handler Rules

**Directory:** `src/service/bots/form_handlers/`

| Rule | Detail |
|---|---|
| One handler per action | Each `submit_action` string maps to exactly one `BaseFormHandler` subclass |
| Channel-agnostic | Handlers must **never** import Teams-specific code or build card JSON |
| User resolution | Always resolve the user from `activity["from"]` via `ExternalIdentityService`, not from form metadata |
| Org validation | Use the resolved user's `organisation_id`. If `form_data` contains an `organisation_id`, validate it matches — but never trust it as the source of truth |
| DB sessions | Acquire sessions inside the handler (fresh session pattern). Don't hold sessions across external calls. |
| Return type | Always return `FormHandlerResult(success=bool, message=str)` |
| Cancel handlers | Use the built-in `CancelFormHandler(message)` — no need to create a custom class |
| Registration | Register in `src/service/bots/form_handlers/__init__.py` at module level |

---

## 7. Bot Service: Invoke Response Flow

**File:** `src/service/bots/teams_bot_service.py`

The `_handle_invoke()` method routes invoke activities:

```
invoke received
  │
  ├── name == "adaptiveCard/action"?
  │     YES → _handle_adaptive_card_action()
  │             → Extract verb + data from value.action
  │             → FormHandlerRegistry.handle(verb, activity, data)
  │             → success? → build_confirmation_card(result.message)
  │             → failure? → build_error_card(result.message)
  │             → Return invoke response with replacement card
  │
  └── NO → Legacy invoke handling (value.action field)
            → FormHandlerRegistry.handle(action, ...)
            → Send reply message + return {"status": 200, ...}
```

**You should not need to modify `_handle_invoke()` or `_handle_adaptive_card_action()` when adding new forms.** The routing is verb-based and fully generic.

---

## 8. Webhook Handler: No Changes Needed

**File:** `src/api/routers/bots/teams_webhook_handler.py`

The webhook already handles invoke responses generically:

```python
if body.get("type") == "invoke" and result:
    return JSONResponse(content=result, status_code=status.HTTP_200_OK)
```

The `result` dict from `_handle_adaptive_card_action()` (containing `statusCode`, `type`, `value`) is returned as-is. No webhook changes are needed for new forms.

---

## 9. Data Flow Diagram

### Happy path (Action.Execute — modern Teams clients)

```
User clicks Submit on Adaptive Card
  │
  ▼
Teams sends: type="invoke", name="adaptiveCard/action"
  value.action.verb = "create_deal_submit"
  value.action.data = { name: "Acme", amount: "50000", organisation_id: "..." }
  │
  ▼
Webhook → process_activity() → _handle_invoke()
  → Detects "adaptiveCard/action"
  → _handle_adaptive_card_action()
  │
  ▼
FormHandlerRegistry.handle("create_deal_submit", activity, form_data)
  → CreateDealFormHandler.handle()
  → Resolves user, validates org, calls DealWriteService
  → FormHandlerResult(success=True, message="Deal 'Acme' created!")
  │
  ▼
build_confirmation_card("Deal 'Acme' created!")
  → Adaptive Card with no buttons
  │
  ▼
Return invoke response:
  { statusCode: 200, type: "application/vnd.microsoft.card.adaptive", value: {...} }
  │
  ▼
Webhook returns JSONResponse → Teams REPLACES original card
  → User sees confirmation, cannot re-submit
```

### Fallback path (Action.Submit — older Teams clients)

```
Teams sends: type="message", value.action="create_deal_submit"
  │
  ▼
_handle_message() → detects form_data with "action" key
  → _handle_form_submission()
  → FormHandlerRegistry.handle("create_deal_submit", activity, form_data)
  → Same handler, same result
  │
  ▼
_send_reply(result.message) → Sends as new text message
  (Cannot replace the original card — it stays active)
```

---

## 10. Backward Compatibility (Action.Submit Fallback)

Every `Action.Execute` button includes a `fallback` block with `Action.Submit`. This ensures:

- **Old Teams clients** that don't support Action.Execute fall back to Action.Submit
- The fallback sends a `type="message"` activity with `value.action = "<action_name>"`
- This is caught by `_handle_message() → _handle_form_submission()` (the pre-existing path)
- The response is sent as a regular reply message (no card replacement)

**The fallback `data` dict must always include `"action": "<submit_action>"`** because `_handle_form_submission()` routes on `form_data.get("action")`.

---

## 11. Common Pitfalls

| Pitfall | Fix |
|---|---|
| Card version set to 1.5 | Use **1.4**. Some Teams clients don't support 1.5 correctly with Universal Actions. |
| Using Action.Submit without fallback structure | Always use Action.Execute as primary with Action.Submit in `fallback`. Never use bare Action.Submit for new cards. |
| Putting `action` key in Action.Execute `data` | Don't. The `verb` field on Action.Execute replaces `data.action`. The `action` key only goes in the fallback's `data`. |
| Handler importing Teams-specific code | Handlers must be channel-agnostic. Card building happens in the bot service layer. |
| Trusting `organisation_id` from form metadata | Always resolve the user from `activity["from"]` and use their verified `organisation_id`. Form metadata is for convenience, not security. |
| Returning a card with buttons after submission | Confirmation and error cards must have **no actions array**. This prevents re-submission. |
| Forgetting to register both submit and cancel | Register both `<action>_submit` and `<action>_cancel` in `form_handlers/__init__.py`. Use `CancelFormHandler` for cancel. |
| Holding DB sessions across the handler | Use the fresh session pattern. Acquire inside the handler, release before returning. |

---

## 12. File Reference

| File | Role |
|---|---|
| `src/adapters/card_builders/base.py` | Channel-agnostic `FormField`, `FormDefinition`, `FormCardBuilder` ABC |
| `src/adapters/card_builders/__init__.py` | `get_card_builder(channel)` factory |
| `src/adapters/card_builders/teams_card_builder.py` | `TeamsFormCardBuilder` + `build_confirmation_card()` + `build_error_card()` |
| `src/service/bots/form_handlers/base.py` | `FormHandlerResult`, `BaseFormHandler` ABC, `CancelFormHandler` |
| `src/service/bots/form_handlers/__init__.py` | `FormHandlerRegistry` + handler registrations |
| `src/service/bots/form_handlers/create_deal_handler.py` | Reference implementation of a form handler |
| `src/service/bots/teams_bot_service.py` | `_handle_invoke()`, `_handle_adaptive_card_action()`, `_handle_form_submission()` |
| `src/api/routers/bots/teams_webhook_handler.py` | Webhook endpoint — returns invoke responses as JSONResponse |

---

## Quick Checklist for New Interactive Cards

- [ ] Define `FormDefinition` with fields, `submit_action`, `cancel_action`, and `metadata`
- [ ] Use `get_card_builder("teams").build(form)` to render the card
- [ ] Create a `BaseFormHandler` subclass in `src/service/bots/form_handlers/`
- [ ] Resolve user from `activity["from"]`, validate org ownership
- [ ] Return `FormHandlerResult(success, message)` — never build card JSON in the handler
- [ ] Register both submit and cancel actions in `form_handlers/__init__.py`
- [ ] Verify `ruff check` and `mypy` pass on changed files
- [ ] Test: Submit card → card replaced with confirmation → cannot re-submit
