# CRM Agent Tool Routing

This document defines how CRM account/contact write tools should be exposed to agents when the
overall tool catalog is large. The goal is to keep agent tool selection reliable without removing
the ability to update or archive records when the user asks for it.

## Problem

The backend can support many CRM write capabilities:

- create account
- update account
- archive account
- create contact
- update contact
- archive contact
- bulk import/upsert later

If every tool is exposed to every agent turn, the model has more choices than it needs. That
increases wrong-tool risk, especially when tools have similar names such as `create_account`,
`update_account`, and `manage_account`.

The opposite extreme is also unsafe. If only `create_account` is available and the user asks to
update an account, the model cannot call `update_account`. Hidden tools are not callable unless the
runtime loads them, routes to another agent, or runs a second model call with a different allowed
tool subset.

## External Guidance

OpenAI's function-calling guidance recommends keeping the initially available function set small,
using clear descriptions, using enums/object structure to make invalid states hard to express, and
using tool search/deferred tools for larger catalogs.

OpenAI's `tool_choice` guidance also supports restricting calls to an `allowed_tools` subset. This
lets the application pass or register a broader catalog while allowing only a small set for a
specific turn.

OpenAI's reasoning-model guidance says tool-heavy workflows should put side effects, retry safety,
required inputs, and common error modes into tool descriptions, and should consider tool search for
large tool catalogs.

LangChain documents the same operational pattern as dynamic tool selection: too many tools can
overwhelm the model, and the runtime can filter tools based on state, permissions, feature flags, or
conversation stage.

Anthropic's tool-use guidance emphasizes detailed tool descriptions that explain what each tool
does, when to use it, when not to use it, and important caveats.

References:

- OpenAI function calling best practices: https://developers.openai.com/api/docs/guides/function-calling#best-practices-for-defining-functions
- OpenAI tool choice / allowed tools: https://developers.openai.com/api/docs/guides/function-calling#tool-choice
- OpenAI reasoning model tool guidance: https://developers.openai.com/api/docs/guides/latest-model#using-reasoning-models
- LangChain dynamic tool selection: https://docs.langchain.com/oss/python/langchain/context-engineering
- Anthropic tool-use implementation guidance: https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/implement-tool-use

## Decision

Use a small active toolset per turn or per agent. Do not expose all lifecycle tools directly to every
agent.

The durable pattern is:

1. Keep capability-specific tools in the registry.
2. Route the user intent before execution.
3. Build an `allowed_tools` subset for the current turn or hand off to a specialist agent.
4. Let the write tool validate IDs, organization scope, permissions, required fields, and
   confirmation.

This gives us both reliability and capability:

- The general agent is not overloaded with every CRM write tool.
- A create-only workflow cannot accidentally update or archive records.
- Update/archive is still reachable when the user intent requires it.

## Tool Families

Use capability-tiered tools in the registry:

```text
search_accounts
create_account
update_account
archive_account
manage_account

search_contacts
create_contact
update_contact
archive_contact
manage_contact
```

Do not expose all of these together by default. `manage_account` and the individual lifecycle tools
are alternatives for different agent profiles, not tools that should normally appear side by side.

## Agent Profiles

### Broad CRM Chat Agent

Use when the agent is expected to handle natural CRM write requests from the user.

Preferred active toolset after routing:

```text
search_accounts
create_account
```

or:

```text
search_accounts
update_account
```

or:

```text
search_accounts
archive_account
```

The broad agent should not start every turn with all account/contact write tools available. It
should route first, then run with the smallest set required for that intent.

### Create-Only Workflow Agent

Use when the workflow's business rule is "create missing records, but never mutate existing
records." Meeting-summary stakeholder gap filling is an example of this class.

Active toolset:

```text
search_accounts
create_account
search_contacts
create_contact
```

This agent should not receive `update_account`, `archive_account`, `update_contact`, or
`archive_contact`.

### CRM Maintenance Agent

Use when the agent's job is cleanup, correction, enrichment, or admin-managed data maintenance.

Active toolset:

```text
search_accounts
update_account
archive_account
search_contacts
update_contact
archive_contact
```

This agent should not receive create tools unless creation is part of the maintenance workflow.

### Bulk Import/Sync Worker

Use for thousands of rows, external CRM sync, CSV import, or deterministic reconciliation.

Do not call chat tools row by row. Use a dedicated async job surface:

```text
bulk_upsert_accounts
bulk_upsert_contacts
```

Bulk jobs should include preview, validation, duplicate handling, approval, async processing, and
per-row results.

## How Hidden Update Tools Become Reachable

An LLM cannot call a tool that is not in the active tool list. Therefore update/archive must become
reachable through runtime orchestration.

Use one of these patterns.

### Pattern 1: Application-Side Intent Router

The application classifies intent before the main agent call and chooses the allowed tools.

Example user request:

```text
Update Acme's website to https://acme.com.
```

Router output:

```json
{
  "entity": "account",
  "action": "update",
  "risk": "medium",
  "allowed_tools": ["search_accounts", "update_account"],
  "requires_confirmation": false
}
```

Second-stage agent call:

```text
System/runtime allowed tools:
- search_accounts
- update_account
```

Expected execution:

```text
1. search_accounts(query="Acme")
2. If exactly one account matches, update_account(account_id=..., fields={"website": "https://acme.com"})
3. If multiple accounts match, ask the user to choose.
4. Return a receipt that lists changed fields.
```

This is the safest default because routing is deterministic application behavior, not a hidden
model assumption.

### Pattern 2: Capability Request Tool

Expose a small always-available tool that asks the runtime to reload the next turn with a different
tool profile.

Always-available tools:

```text
search_accounts
create_account
request_crm_write_capability
```

Example request:

```text
Archive the old Acme account.
```

Tool call:

```json
{
  "capability": "archive_account",
  "reason": "User requested account archival",
  "entity_hint": "Acme"
}
```

Runtime response:

```json
{
  "status": "approved",
  "next_allowed_tools": ["search_accounts", "archive_account"],
  "confirmation_required": true
}
```

Second-stage agent call:

```text
Use search_accounts to resolve the account. Ask for explicit confirmation before archive_account.
```

This pattern is useful when the first model call must notice that its current tools are insufficient.
It still requires the runtime to enforce the actual tool switch.

### Pattern 3: Specialist Agent Handoff

The broad agent hands off to an agent with a narrower write surface.

General agent tools:

```text
search_accounts
create_account
handoff_to_crm_update_agent
```

CRM update agent tools:

```text
search_accounts
update_account
archive_account
```

Example:

```text
User: Change Acme's industry to Healthcare.
GeneralAgent: handoff_to_crm_update_agent(reason="User asked to update an existing account")
CRMUpdateAgent: search_accounts(query="Acme")
CRMUpdateAgent: update_account(account_id=..., fields={"industry": "Healthcare"})
```

This is useful when update/archive behavior has different prompts, permissions, audit policy, or
confirmation rules.

### Pattern 4: Tool Search / Deferred Tool Loading

Use a tool-search layer or registry lookup to retrieve the most relevant tool definitions for the
turn.

Example:

```text
User asks: Update Jane's title.
Tool search query: contact update job title
Loaded tools: search_contacts, update_contact
```

This pattern is useful when the tool catalog becomes too large for static routing rules. It should
still be paired with policy checks so a user cannot surface destructive tools without permission.

## Recommended Architecture For This Backend

Use application-side routing as the primary mechanism. Use handoff for workflows that deserve a
separate agent identity.

```text
Incoming message
    |
    v
CRM intent router
    |
    +-- no CRM write intent -> normal GeneralAgent tools
    |
    +-- create account/contact -> allowed_tools: search + create
    |
    +-- update account/contact -> allowed_tools: search + update
    |
    +-- archive account/contact -> allowed_tools: search + archive + confirmation
    |
    +-- bulk import/sync -> route to async bulk job flow, not chat tools
```

The router does not write to the database. It only chooses the execution profile.

## Example Routing Rules

```python
def route_crm_write_intent(message: str) -> dict[str, object]:
    intent = classify(message)

    if intent.entity == "account" and intent.action == "create":
        return {
            "profile": "crm_account_create",
            "allowed_tools": ["search_accounts", "create_account"],
            "requires_confirmation": False,
        }

    if intent.entity == "account" and intent.action == "update":
        return {
            "profile": "crm_account_update",
            "allowed_tools": ["search_accounts", "update_account"],
            "requires_confirmation": False,
        }

    if intent.entity == "account" and intent.action == "archive":
        return {
            "profile": "crm_account_archive",
            "allowed_tools": ["search_accounts", "archive_account"],
            "requires_confirmation": True,
        }

    return {
        "profile": "general",
        "allowed_tools": ["search_accounts", "create_account"],
        "requires_confirmation": False,
    }
```

The real implementation should use structured classification, not string matching. The important
point is that routing happens before the write tool is exposed.

## Tool Contract Examples

### `create_account`

Use only for new account creation. It may search/reuse an existing account if a unique domain or
exact name match already exists, but it must not update an existing account's fields.

Input:

```json
{
  "name": "Acme",
  "domain": "acme.com",
  "website": "https://acme.com",
  "industry": "Manufacturing"
}
```

Allowed outcomes:

```json
{
  "status": "created",
  "account_id": "..."
}
```

```json
{
  "status": "exists",
  "account_id": "...",
  "message": "Account already exists; no fields were updated."
}
```

### `update_account`

Use only when an existing account is resolved. Require `account_id`. Update only provided fields.
Never create a new account.

Input:

```json
{
  "account_id": "9f0c0e7d-56c3-41b3-9af7-1e3ab0fb8e08",
  "fields": {
    "website": "https://acme.com",
    "industry": "Healthcare"
  }
}
```

Allowed outcome:

```json
{
  "status": "updated",
  "account_id": "9f0c0e7d-56c3-41b3-9af7-1e3ab0fb8e08",
  "changed_fields": ["website", "industry"]
}
```

### `archive_account`

Use only after explicit confirmation unless the caller is a pre-approved bulk/admin job. Require
`account_id`. Prefer archive/soft-delete over hard delete.

Input:

```json
{
  "account_id": "9f0c0e7d-56c3-41b3-9af7-1e3ab0fb8e08",
  "confirmation_token": "confirmed_by_user"
}
```

Allowed outcome:

```json
{
  "status": "archived",
  "account_id": "9f0c0e7d-56c3-41b3-9af7-1e3ab0fb8e08"
}
```

## User Experience Examples

### Create

User:

```text
Add Acme as an account.
```

Runtime:

```text
allowed_tools = ["search_accounts", "create_account"]
```

Agent:

```text
Created Acme.
```

### Update

User:

```text
Update Acme's website to https://acme.com.
```

Runtime:

```text
allowed_tools = ["search_accounts", "update_account"]
```

Agent:

```text
Updated Acme's website to https://acme.com.
```

### Ambiguous Update

User:

```text
Update Acme's industry to Healthcare.
```

Search result:

```json
[
  {"account_id": "1", "name": "Acme Inc", "domain": "acme.com"},
  {"account_id": "2", "name": "Acme Healthcare", "domain": "acmehealth.com"}
]
```

Agent:

```text
I found two Acme accounts. Which one should I update: Acme Inc or Acme Healthcare?
```

No update tool call should happen until the account is resolved.

### Archive

User:

```text
Archive Acme.
```

Runtime:

```text
allowed_tools = ["search_accounts", "archive_account"]
```

Agent:

```text
I found Acme Inc. Please confirm you want to archive this account.
```

After confirmation:

```text
Archived Acme Inc.
```

## Safety Rules

1. A hidden tool is not reachable unless the runtime loads it, routes to another agent, or uses a
   second call with a different allowed tool subset.
2. Do not expose `create_*`, `update_*`, `archive_*`, and `manage_*` for the same entity in the same
   active toolset unless testing proves the model selects correctly.
3. Create-only agents must not receive update/archive tools.
4. Update tools require a resolved internal ID and must not create fallback records.
5. Archive tools require explicit confirmation unless called from a pre-approved bulk/admin job.
6. Bulk import/sync must not loop through chat tools row by row.
7. Every write tool must enforce organization isolation and validate resource ownership.
8. Tool descriptions must state side effects, retry safety, and what the tool refuses to do.

## Testing Requirements

Add regression tests before shipping the routing implementation:

- Create request exposes only `search_accounts` and `create_account`.
- Update request exposes only `search_accounts` and `update_account`.
- Archive request exposes only `search_accounts` and `archive_account`.
- Create-only workflow cannot call update/archive tools.
- Ambiguous search result blocks update/archive until user selection.
- Archive without confirmation returns a confirmation-required response.
- Cross-organization IDs are rejected by every write tool.
- Bulk requests route to the bulk job flow, not per-row chat tools.

## Final Position

The product should not provide all CRM write tools directly to every agent. The product should keep
the complete capability set in the registry, then expose the smallest safe subset based on agent
profile, user intent, permissions, and conversation stage.

For account/contact work, the recommended production model is:

```text
Capability registry:
  search_accounts, create_account, update_account, archive_account
  search_contacts, create_contact, update_contact, archive_contact

Runtime selection:
  route intent -> choose allowed_tools -> execute with small active toolset

Special cases:
  create-only workflow agents get create tools only
  maintenance agents get update/archive tools only
  bulk sync/import gets separate async job tools
```
