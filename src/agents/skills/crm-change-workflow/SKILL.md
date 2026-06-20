---
name: crm-change-workflow
description: Use when a chat, Teams, Slack, WhatsApp, web, or voice user asks to change CRM data, including creating, updating, completing, assigning, adding notes, or saving CRM records.
---

# CRM Change Workflow

## Purpose

Use this skill when the user wants Kai to change CRM state. A CRM change includes
creating, updating, completing, assigning, linking, adding notes, or saving records.

Do not use this skill for read-only analysis. If the user asks to explain stakeholders,
summarize a deal, assess deal health, or compare CRM data, gather data and answer without
calling CRM write tools.

## Fast Decision

Before calling tools, classify the turn:

| User intent | Action |
| --- | --- |
| "Create/update/add/complete/save..." | Use this skill, then call the matching CRM change tool |
| "Yes/confirm/go ahead" with a pending CRM action | Approve the pending action by `action_state_id` |
| "Change it/make it..." with a pending CRM action | Patch the pending action |
| "What is/who is/analyze/summarize..." | Read-only path, no CRM change tool |
| Ambiguous target or missing required field | Ask one focused clarification question |

Pending CRM state wins over new tool selection. If the current context shows a pending
CRM action and the user approves or edits it, operate on that pending action first.

## Clarification Rule

Ask a question only when all three are true:

- A required field is missing.
- No safe default or active context can fill it.
- Guessing could change the wrong CRM record.

Do not ask for optional fields before using a CRM change tool. Include optional fields
only when the user already provided them.

## Creation Readiness

Before creating or mutating CRM data, identify the required creation logic:

- What object is being changed or created.
- Which required fields are present.
- Which required fields can be safely inferred from active context.
- Which optional fields the user already provided.
- Which record ownership, deal/account/person match, or assignee resolution matters.
- Whether the action could affect the wrong CRM record if inferred incorrectly.

Bias toward confirmation when there is meaningful doubt. If any doubt remains about the
target record, recipient/person, required field, ownership, duplicate match, or user intent,
ask one focused confirmation question before creating it.

If there are no doubts after checking required fields and context, proceed with the narrowest
matching tool. Do not ask for optional information just to make the record more complete.

## Tool Routing

Use the narrowest available tool for the requested change:

| Intent | Tool behavior |
| --- | --- |
| Create deal | Call `CreateDeal` once deal name is known |
| Create account | Call `CreateAccount` once account name, domain, or website is known |
| Create contact | Call `CreateContact` once name and email are known |
| Add stakeholder | Resolve the deal, then call `AddStakeholder` once deal and email are known |
| Add deal note | Resolve the deal, then call `AddDealNote` once deal and note text are known |
| Create task | Call `create_task` once the task title is meaningful |
| Update/complete task | Resolve the task if needed, then call the matching task tool |

Avoid a broad "do anything in CRM" tool when a specific tool exists. Specific tools give
the model clearer schemas and reduce wrong writes.

## Pending Action Rules

Some CRM changes require approval. Treat the pending action as a saved draft.

### New Draft

When a CRM change needs approval:

- Normalize the user's input.
- Apply safe defaults.
- Create pending action state.
- Return the preview/action card with `action_state_id`.

### Draft Edit

When the user edits a pending CRM action:

- Call `patch_pending_action`.
- Pass the listed `action_state_id` and revision.
- Include only changed fields.
- Do not approve the action.
- Do not create a second pending action.

Examples:

- "Make it 75k."
- "Change the due date to Friday."
- "Use Acme India instead."
- "Replace the note with this..."

### Approval

When the user approves a pending CRM action:

- Call `approve_pending_action` with the listed `action_state_id`.
- Execute the saved payload.
- Do not call the original create/update tool again from scratch.

If multiple pending actions could match a short approval, ask which one.

## Safety Rules

Every CRM change must preserve tenant and user boundaries:

- Validate organization scope.
- Validate user scope when applicable.
- Validate target record ownership before mutation.
- Never trust external CRM IDs alone.
- Never mutate cross-organization data.
- Confirm before mutation when identity, ownership, or intent is not clear.
- Do not hold database sessions during long LLM or external-service calls.

## Channel Rules

The workflow is the same on web, voice, Teams, Slack, and WhatsApp. Channel code may render
cards differently, but CRM business logic must use the same backend result or pending-action
payload.

Do not add channel-specific CRM behavior to the general chat prompt when this skill can own
the reusable rule.

## Response Rules

- For direct success, state what changed and any useful next step.
- For pending approval, show what will change and how to approve, edit, or cancel.
- For clarification, ask one focused question.
- For failures, explain the blocker without exposing internal IDs.

## Examples

### Create deal

User:

```text
Create a deal for Acme worth 50k closing next month.
```

Behavior:

- Call `CreateDeal` because the deal name is known.
- Include amount and close date because the user provided them.
- Let backend defaults fill omitted optional fields.
- If approval is required, return a pending preview.

User:

```text
yes
```

Behavior:

- Approve the saved pending action by `action_state_id`.
- Do not call `CreateDeal` again.

### Edit pending deal

User:

```text
Actually make it 75k and close date June 30.
```

Behavior:

- Patch the active pending action.
- Send only amount and close date changes.
- Return the updated preview.

### Create task

User:

```text
Create a task to follow up with Sarah next Friday.
```

Behavior:

- Call `create_task` because the task title is meaningful.
- Include assignee and due date if the tool accepts them.
- Default priority and other optional fields.

## Common Mistakes

- Asking for optional fields before calling a write tool.
- Skipping the required creation logic check before mutating CRM data.
- Creating a record when there is unresolved doubt about the target, person, or intent.
- Treating "yes" as a new create request instead of pending-action approval.
- Creating a new pending action for a draft edit.
- Mixing CRM education or stakeholder analysis with CRM changes.
- Adding more one-off CRM rules to the general prompt.
- Encoding channel-specific business logic in CRM tools.
