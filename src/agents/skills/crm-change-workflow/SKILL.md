---
name: crm-change-workflow
description: Use when a chat, Teams, Slack, WhatsApp, web, or voice user asks to change CRM data, including creating, updating, completing, assigning, adding notes, adding stakeholders, creating tasks, or saving CRM records.
---

# CRM Change Workflow

Use this skill when the user wants Kai to change CRM state. CRM writes must be
intent-confirmed, dependency-aware, and summarized back to the user in clear
markdown. Do not treat tool success as the final answer.

Do not use this skill for read-only analysis. If the user asks to explain stakeholders,
summarize a deal, assess deal health, or compare CRM data, gather data and answer without
calling CRM write tools.

Follow the GeneralAgent Context Resolution Gate before asking which CRM record, person,
task, note, or deal the user means. Read-only resolution may happen automatically; CRM
writes still require a clear target, safe payload, and normal confirmation behavior.

## Fast Decision

Before calling tools, classify the turn:

| User intent | Action |
| --- | --- |
| "Create/update/add/complete/save..." | Use this skill, then call the matching CRM change tool |
| "Yes/confirm/go ahead" with a pending CRM action | Approve the pending action by `action_state_id` |
| "Change it/make it..." with a pending CRM action | Patch the pending action |
| Multiple requested CRM writes | Build a dependency plan before any write tool call |
| "What is/who is/analyze/summarize..." | Read-only path, no CRM change tool |
| Ambiguous target or missing required field after context resolution | Ask one focused clarification question with any useful candidates found |

Pending CRM state wins over new tool selection. If the current context shows a pending
CRM action and the user approves or edits it, operate on that pending action first.

## Mutation Plan

For every CRM write request, make a short internal plan before tool calls:

- Requested sub-actions, in the user's order.
- The narrowest tool for each sub-action.
- Required fields already present.
- Required fields safely inferable from active context.
- Target record or person resolution needed before mutation.
- Dependencies between sub-actions.
- Whether the final response will be single-action, multi-action, pending, or partial.

Do not expose the internal plan unless progress tracking is useful. The plan exists to
prevent wrong writes, duplicate writes, and misleading final answers.

## Clarification Rule

Ask a question only when all three are true:

- A required field is missing.
- No safe default, active context, or read-only lookup can fill it.
- Guessing could change the wrong CRM record or create a useless record.

Do not ask for optional fields before using a CRM change tool. Include optional fields
only when the user already provided them.

## Dependency and Parallelism Rules

The runtime may allow parallel tool calls, but CRM writes may run in parallel only when
they are truly independent.

Run sub-actions sequentially when any of these are true:

- A later action needs an ID or confirmed target produced by an earlier action.
- A later action depends on a pending approval from an earlier action.
- The actions write to the same deal, task, contact, account, stakeholder, or note and
  ordering affects the result.
- One action resolves duplicate or ambiguous identity for another action.
- A failure in the first action would make the next action unsafe or meaningless.

Parallel writes are allowed only when each action has all required fields, an unambiguous
target, no shared mutable dependency, and no output from one tool is needed by another.
When in doubt, run sequentially.

Examples:

| Request | Execution |
| --- | --- |
| "Create two tasks: follow up Friday, send deck Monday" | Two `create_task` calls may run in parallel if titles and dates are clear |
| "Create a task and create an unrelated account for acme.com" | May run in parallel if neither result feeds the other |
| "Create a deal for Acme, then add Sarah as stakeholder" | Sequential: create deal first, then add stakeholder using the created/resolved deal |
| "Create a deal and add a note to it" | Sequential: the note needs the deal ID/name |
| "Create a contact and link it to this deal" | Sequential unless a resolved deal ID is already in context |
| "Create a task for the deal you just created" | Sequential: create deal first, then task with the created deal context |

If a sequential parent action returns a pending approval or preview instead of a completed
write, stop there. Do not run dependent child actions until the parent is approved and the
created record is known.

## Tool Routing

Use the narrowest available tool for the requested change:

| Intent | Required before tool call | Tool behavior |
| --- | --- | --- |
| Create deal | Deal name | Call `CreateDeal`. Include amount, currency, expected close date, account fields, priority, and description only when provided or safely inferred |
| Create account | Account name, domain, website, or company LinkedIn URL | Call `CreateAccount`. Do not ask for optional enrichment fields first |
| Create contact | Contact name, email, or LinkedIn profile URL | Call `CreateContact`. Include deal link only when resolved or explicitly provided |
| Create lead form draft | Form name and at least one field | Call `CreateDraftLeadForm` only after public/private intent is resolved. Current agent path creates private drafts only |
| Edit lead form draft | Resolved draft form ID | Call `UpdateDraftLeadForm` with only requested changes. Do not publish or change lifecycle status |
| Update account | Resolved existing internal CRM account | Search/resolve the account first when needed, then call `UpdateAccount` with only changed fields |
| Update contact | Resolved existing internal CRM contact | Search/resolve the contact first when needed, then call `UpdateContact` with only changed fields |
| Update deal | Resolved existing deal | Use the specific deal update tool for fields, lifecycle, or account changes. Do not create a new deal for an update request |
| Add stakeholder | Resolved existing deal ID/name and stakeholder name, email, or LinkedIn profile URL | Search/resolve the deal first, then call `AddStakeholder`. Add only external contacts |
| Add deal note | Resolved existing deal ID/name and note text | Search/resolve the deal first, then call `AddDealNote` |
| Create task | Meaningful task title | Call `create_task`. Include assignee, due date, priority, reminder, and deal only when provided or already in context |
| Update/complete task | Resolved task or unambiguous active task context | Resolve the task if needed, then call the matching task tool |

Avoid a broad "do anything in CRM" tool when a specific tool exists. Specific tools give
the model clearer schemas and reduce wrong writes.

## Contact and Account Updates

Use update tools only when the user wants to change an existing record. If the user says
"create," "add," "new," or provides a fresh person/company identity with no existing target,
use the create flow instead.

Before calling `UpdateContact` or `UpdateAccount`:

- Resolve exactly one internal CRM record from active context, search results, or a prior
  selected UI record. Do not update from a fuzzy name, email, or domain when multiple records
  could match.
- Pass the concrete `contact_id` or `account_id` returned by CRM lookup/context.
- Include only fields the user asked to change. Do not pad the payload with guessed enrichment.
- If the tool reports that updates are limited to internal CRM records, explain that provider-
  synced contacts/accounts cannot be changed by this agent path.

`UpdateContact` accepts contact identity and profile fields such as email, name fields, job
title, phone, mobile phone, LinkedIn URL, and `company_id`. Use `company_id` to link a contact
to an account; do not pass free-text company names as contact updates.

`UpdateAccount` accepts account profile fields such as name, domain, website, industry,
lifecycle stage, company type, description, phone, LinkedIn URL, employee count, revenue,
address fields, country, and timezone.

For deal updates, use the existing deal update tools: field updates, lifecycle updates, or
account reassignment. Do not route deal updates through contact or account update tools.

## Lead Form Draft Contract

Treat lead-form creation as saving an editable draft, not launching an intake channel. The
agent can help turn a request, screenshot, or pasted field list into a draft form, but the
user must still review mappings before the form is made live.

Before calling `CreateDraftLeadForm`:

- If the user asks "should I create a form?" or the public/private intent is unclear, ask one
  focused question: whether they want a private draft. Do not ask for optional styling,
  submit-button text, or routing rules before creating the draft.
- If the user asks for a public/live form, do not call the tool as a workaround. Say that chat
  can create a private draft for review, and publishing must happen through the lead-form
  management flow.
- Require a useful form name and at least one field. Fields may come from text, active
  context, or image-derived content already visible to the model.
- Map obvious lead fields such as name, email, phone, company, job title, message, interest,
  budget, and UTM fields. Leave uncertain or custom fields unmapped for review instead of
  guessing a custom-field key.

Before calling `UpdateDraftLeadForm`:

- Use a concrete `form_id` from the just-created draft, selected UI context, or a prior form
  result. If only a fuzzy form name is available and more than one form could match, ask which
  draft to edit.
- Include only the requested metadata or field changes. If the user edits only name or
  description, do not rebuild the field list.
- Do not activate, pause, archive, or publish a form. If the user asks for status changes,
  explain that this agent path edits drafts only.

After a form draft tool call, summarize the draft name, status, useful fields, and any
unmapped fields that need review. Do not answer only with the raw tool result.

## Identity URL Routing

When the user pastes a URL as the identity input, classify it before asking a question:

- Company/account URLs go to account creation: company LinkedIn pages, school/showcase LinkedIn
  pages, company domains, and company websites. Pass the URL as `linkedin_url`, `domain`, or
  `website` based on what was provided.
- Person URLs go to person creation: LinkedIn profile pages are enough to call `CreateContact`
  when the user wants a contact, or `AddStakeholder` when the user wants that person attached
  to a resolved deal.
- Deal context decides contact versus stakeholder. If the request says "add this person to this
  deal" or the active context is a deal stakeholder action, resolve the deal and call
  `AddStakeholder`; otherwise call `CreateContact`.
- Do not ask for email first when name or LinkedIn profile URL is already present. The backend
  owns identity resolution and will return a blocker if the provider cannot resolve a usable
  contact.
- Do not create an empty record from an unrelated, malformed, or ambiguous URL. Ask one focused
  question only when the URL cannot be classified as an account or person identity.

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

## Task Creation Contract

For task requests, the useful unit is the work item, not the word "task."

- If the title is meaningful, call `create_task` immediately.
- Never create a placeholder title like "Task," "New task," or "Follow up" when the
  object of the follow-up is missing.
- Preserve explicit assignee text in `assign_to`; the tool resolves it to an org member.
- If no assignee is given, let the tool default to the current user.
- Preserve due-date language in the user's timezone, such as "Friday" or "tomorrow."
- If a reminder is requested, pass it only when a due date is also present.
- For multiple tasks, create one CRM task per distinct work item. Do not merge separate
  tasks into one generic task unless the user asked for a combined task.

When the tool returns an assignee, due date, priority, reminder, or deal link, include those
fields in the final markdown. If assignment or deal resolution fails, say which task is
blocked and what single detail is needed.

## Pending Action Rules

Some CRM changes require approval. Treat the pending action as a saved draft.

### New Draft

When a CRM change needs approval:

- Normalize the user's input.
- Apply safe defaults.
- Create pending action state.
- Return the preview/action card with `action_state_id`.
- Do not run dependent child actions until this draft is approved.

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

## Result Handling

After every CRM write tool call, inspect the returned status before answering:

| Tool result | Response handling |
| --- | --- |
| `created` | State the created record and key fields from the result |
| `exists` or `already_linked` | State that the record/link already existed; do not imply a duplicate was created |
| `preview_sent` or pending approval | State what is pending and how the user can approve, edit, or cancel |
| selection needed, ambiguous match, missing field | Ask one focused question and list only the useful options |
| `error` | Explain the blocker and the single next input/action needed |
| partial success | Separate completed actions from blocked or follow-up actions |
| unknown, timeout, or empty result | Do not retry blindly; check current state or tell the user the result is uncertain |

Do not expose raw internal IDs in user-facing text unless the user asks or the UI/runtime
requires them. Use human-readable names, email addresses, dates, amounts, and statuses.

## Markdown Outcome Contract

Every CRM mutation response must be readable markdown. Do not answer only "done" or paste
raw JSON. The user should know what changed, what did not change, and what is next.

For a single successful write:

```markdown
Done - created task **Send proposal**.

- Assignee: Nikhil
- Due: Friday
- Priority: High
- Related deal: Acme Enterprise
```

For multiple writes:

```markdown
Completed:
- Deal: **Acme Enterprise** - USD 50,000, expected close June 30
- Stakeholder: **Sarah Chen** `<sarah@acme.com>` added to **Acme Enterprise**

Blocked:
- Task: **Send proposal** - I could not find assignee "Sam". Please provide the full name or email.
```

For pending approval:

```markdown
Ready for approval:
- Deal: **Acme Enterprise**
- Amount: USD 50,000
- Expected close date: June 30

You can approve it, edit a field, or cancel.
```

For clarification:

```markdown
I need one detail before I create this: what should the task be about?
```

For failures:

```markdown
I could not add the stakeholder because the deal match is ambiguous.

Which deal should I use?
- Acme Enterprise
- Acme Renewal
```

## Output Fields by Record Type

Include fields that are known from the user request or tool result:

| Record | Useful markdown details |
| --- | --- |
| Deal | Name, amount/currency, account, pipeline/stage, expected close date, priority |
| Task | Title, assignee, due date, priority, reminder, related deal |
| Stakeholder | Name, email, deal, job title, stance, decision power, whether enrichment/research started if returned |
| Account | Name, domain, website, industry |
| Contact | Name, email, LinkedIn, account, job title, linked deal |
| Note | Deal, subject, note preview, whether an identical note already existed |

Keep responses compact for voice and messaging channels. Use a short paragraph plus bullets.

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

## Examples

### Create deal

User:

```text
Create a deal for Acme worth 50k expected to close next month.
```

Behavior:

- Call `CreateDeal` because the deal name is known.
- Include amount and expected close date because the user provided them.
- Let backend defaults fill omitted optional fields.
- Final answer names the created deal and key fields in markdown.
- If approval is required, return a pending preview and stop.

User:

```text
yes
```

Behavior:

- Approve the saved pending action by `action_state_id`.
- Do not call `CreateDeal` again.

### Create task

User:

```text
Create a task to follow up with Sarah next Friday.
```

Behavior:

- Call `create_task` because the task title is meaningful.
- Pass due date if the user provided it.
- Pass assignee only if "Sarah" is clearly the assignee; otherwise keep Sarah in the title.
- Final answer includes title, due date, assignee if returned, priority, and related deal if any.

User:

```text
Create a task and assign it to Mrinmoy.
```

Behavior:

- Do not call `create_task`; there is no meaningful title.
- Ask: "What should the task be about?"

### Multiple independent tasks

User:

```text
Create two tasks: send the proposal by Friday and review the contract by Monday.
```

Behavior:

- Create two separate tasks because there are two work items.
- The two `create_task` calls may run in parallel if there is no shared unresolved target.
- Final answer lists both tasks under `Completed`.

### Dependent deal and stakeholder

User:

```text
Create a deal for Acme Enterprise worth 50k, then add Sarah Chen as stakeholder.
Sarah's email is sarah@acme.com.
```

Behavior:

- Run sequentially because `AddStakeholder` needs a resolved deal ID.
- Call `CreateDeal` first.
- If the deal is created, call `AddStakeholder` with the created deal ID/name and Sarah's email.
- If deal creation returns pending approval, stop and show the pending deal preview. Do not add the stakeholder yet.
- Final answer separates completed, pending, and blocked actions.

### Stakeholder with name or LinkedIn

User:

```text
Add this stakeholder to Acme Enterprise: https://www.linkedin.com/in/sarah-chen
```

Behavior:

- Resolve Acme Enterprise to one existing deal.
- Call `AddStakeholder` with the resolved deal and the LinkedIn profile URL.
- Do not ask for email first; the backend owns profile resolution.
- If the tool cannot resolve the URL or create a useful stakeholder, report that blocker and the
  single next input needed.

### Pasted account URL

User:

```text
Create this account: https://www.linkedin.com/company/acme
```

Behavior:

- Classify the URL as an account identity.
- Call `CreateAccount` with `linkedin_url`.
- Do not ask for account name, domain, or optional enrichment fields first.

### Pasted contact URL

User:

```text
Create this contact: https://www.linkedin.com/in/jane-buyer
```

Behavior:

- Classify the URL as a person identity.
- Call `CreateContact` with `linkedin_url`.
- Do not ask for email first; if the provider cannot resolve a usable contact, report the tool
  blocker and ask for the missing identity.

### Edit pending deal

User:

```text
Actually make it 75k and expected close date June 30.
```

Behavior:

- Patch the active pending action.
- Send only amount and expected close date changes.
- Return the updated preview.

## Common Mistakes

- Answering "done" without a markdown summary of what changed.
- Pasting raw tool JSON instead of a user-facing outcome.
- Asking for optional fields before calling a write tool.
- Creating placeholder tasks when the task title is missing.
- Merging multiple requested tasks into one vague task.
- Running dependent CRM writes in parallel because parallel tool calls are enabled.
- Calling `AddStakeholder` before the deal is resolved.
- Asking for email when the user already gave a name or LinkedIn profile URL for a contact or stakeholder.
- Sending a company LinkedIn URL to `CreateContact` or a person LinkedIn URL to `CreateAccount`.
- Continuing to dependent writes after a parent action returns pending approval.
- Skipping the required creation logic check before mutating CRM data.
- Creating a record when there is unresolved doubt about the target, person, or intent.
- Treating "yes" as a new create request instead of pending-action approval.
- Creating a new pending action for a draft edit.
- Mixing CRM education or stakeholder analysis with CRM changes.
- Adding more one-off CRM rules to the general prompt.
- Encoding channel-specific business logic in CRM tools.
