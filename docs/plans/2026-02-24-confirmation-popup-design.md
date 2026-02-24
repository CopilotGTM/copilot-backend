# Confirmation Popup for Deal & Note Creation

**Date:** 2026-02-24
**Branch:** `enhance/teams`
**Status:** Approved

## Problem

The general agent currently sends interactive forms (with editable input fields) when users request deal creation or note creation. This adds friction — users must review and potentially re-enter data the agent already extracted from conversation. The reminder tool already uses a better pattern: a read-only confirmation popup where the user simply clicks Confirm or Cancel.

## Decision

Replace the form-based flow with a confirmation-card flow for both `create_deal` and `add_deal_note` tools. Use a Redis nonce to prevent duplicate submissions from stale cards.

## Approach: Confirmation Card + Redis Nonce

### How It Works

1. Agent extracts deal/note data from conversation
2. Tool generates a UUID nonce, stores it in Redis with 15-min TTL
3. Tool builds a **read-only Adaptive Card** (FactSet preview, no input fields)
4. All data is embedded in the Confirm button's `data` payload
5. User clicks Confirm → handler validates nonce (one-time use), creates deal/note
6. User clicks Cancel → handler consumes nonce, returns "Cancelled" card

### Update Flow

When user says "change the amount to 50k":
- Agent calls the tool again with updated values → new card, new nonce
- Old card's nonce expires naturally (15-min TTL) or is rejected as consumed

## Changes

### Tool Changes

#### `src/agents/tools/crm/create_deal.py`
- Replace `FormDefinition`/`FormField` building with a read-only preview card
- Generate nonce via shared helper, store in Redis
- Build `FactSet` card showing: Name, Amount, Currency, Pipeline, Stage, Close Date, Priority, Account, Description
- Empty fields shown as "—"
- Confirm button `data`: all field values + `organisation_id` + `nonce`
- Cancel button `data`: `nonce` + `organisation_id`
- Return `{"status": "preview_sent", ...}` instead of `{"status": "form_sent", ...}`

#### `src/agents/tools/crm/add_deal_note.py`
- Same pattern: read-only preview card with FactSet
- Shows: Deal Name, Note Content
- `note_content` is required (agent must extract from conversation)

### New File: Nonce Utility

#### `src/service/bots/form_handlers/nonce.py`
```python
async def create_nonce(prefix: str, ttl: int = 900) -> str
    """Generate UUID4 nonce, store in Redis as {prefix}:{nonce} with TTL."""

async def validate_and_consume_nonce(prefix: str, nonce: str) -> bool
    """Check if nonce exists, delete it (one-time use), return validity."""
```

Prefixes: `deal_confirm_nonce`, `note_confirm_nonce`

### Handler Changes

#### `src/service/bots/form_handlers/create_deal_handler.py`
- Add nonce validation at the start of `handle()`
- If nonce invalid/expired → return error "This confirmation has expired"
- Data extraction unchanged (same keys in `form_data`)

#### `src/service/bots/form_handlers/add_deal_note_handler.py`
- Same nonce validation pattern

#### `src/service/bots/form_handlers/__init__.py`
- Register cancel handlers for `create_deal_cancel` and `add_deal_note_cancel`

### No Changes Required

- No DB migrations
- No new models
- `FormDefinition`/`FormField`/card builder classes remain (used elsewhere)
- `TeamsFormCardBuilder` still exists for other forms
- Form handler registry pattern unchanged

## Card Structure (Example: Create Deal)

```json
{
  "type": "AdaptiveCard",
  "version": "1.4",
  "body": [
    {"type": "TextBlock", "text": "Create New Deal", "size": "Medium", "weight": "Bolder"},
    {"type": "FactSet", "facts": [
      {"title": "Name", "value": "Acme Corp - Enterprise License"},
      {"title": "Amount", "value": "USD 50,000"},
      {"title": "Pipeline", "value": "Sales Pipeline"},
      {"title": "Stage", "value": "Qualification"},
      {"title": "Close Date", "value": "2026-03-15"},
      {"title": "Priority", "value": "High"},
      {"title": "Account", "value": "Acme Corp"},
      {"title": "Description", "value": "Enterprise license deal"}
    ]},
    {"type": "TextBlock", "text": "Please confirm or cancel.", "isSubtle": true}
  ],
  "actions": [
    {"type": "Action.Execute", "title": "Confirm", "style": "positive", "verb": "create_deal_submit", "data": {"name": "...", "amount": 50000, "nonce": "uuid", "organisation_id": "..."}},
    {"type": "Action.Execute", "title": "Cancel", "style": "destructive", "verb": "create_deal_cancel", "data": {"nonce": "uuid", "organisation_id": "..."}}
  ]
}
```

## Scope

- Teams channel only (web chat unchanged)
- Both `create_deal` and `add_deal_note` tools
