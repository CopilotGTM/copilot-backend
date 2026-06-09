# General Agent State Persistence Strategy

Date: 2026-06-09

## Confidence Statement

I am not 100% confident in the earlier strategy as written. The direction is
right: use an app-owned state layer shaped like Agno's `session_state` and
requirements instead of blindly enabling Agno's database tables as the source of
truth. But the earlier version had loopholes that would still allow broken
meeting/email continuation flows.

The revised strategy below is the recommended implementation plan. It is
factually grounded in:

- Agno's session storage model: adding `db` to an agent stores sessions in
  `agno_sessions` by default, including `session_data`, `agent_data`, `runs`,
  `summary`, metadata, and timestamps.
- Agno's state model: durable working state is `session_state`, stored under
  `session_data["session_state"]`, and only shown to the model when
  `add_session_state_to_context=True`.
- Agno's human-in-the-loop model: side-effect checkpoints are represented as
  run requirements or approvals, not as vague chat history.
- Current backend reality: GeneralAgent currently creates a fresh Agno agent per
  job without Agno `db`, `session_id`, or `session_state`; the worker passes only
  visible chat history and saves truncated tool summaries for UI/audit.

Primary Agno references:

- https://docs.agno.com/database/session-storage
- https://docs.agno.com/agents/state
- https://docs.agno.com/workflows/hitl/overview
- https://www.agno.com/blog/how-to-add-human-in-the-loop-controls-to-ai-agents-that-actually-run-in-production

## Decision

Use app-owned GeneralAgent state for v1.

Do not make Agno's native `agno_sessions` the source of truth yet. It would
duplicate `chat_sessions`, `chat_messages`, `chat_message_jobs`, Redis frames,
and tenant schema lifecycle. It also introduces a second persistence path inside
the LLM run, while this backend intentionally avoids holding DB sessions during
long-running agent work.

Implement a small app-owned state layer:

- Persist under `ChatSession.properties["general_agent_state"]` for v1.
- Use Agno vocabulary: `session_state`, `active_requirement`, and
  `confirmation_payload`.
- Treat short replies like `Confirm`, `Yes`, `use this sender`, or `title is X`
  as updates to the active requirement when one exists.
- Keep `ChatMessage.agent_data.tool_usage` as UI/audit data, not as working
  memory.

## Loopholes And Fixes

### 1. Tool result summaries are too lossy

Current `ToolUsage.result_summary` stores only a short string representation of
the public tool result. Confirmation payloads can be truncated and cannot be
reliably reconstructed later.

Fix:

- Add a structured internal state-capture path, not based on `result_summary`.
- Capture tool results at the `execute_with_capabilities` / `AgentCapabilities`
  boundary before truncation.
- Emit or return a private internal frame such as `agent.state_delta` that is
  consumed by the worker and not published to the client.
- Redact private transport fields and never store provider tokens, raw bodies
  beyond required email draft content, or hidden channel payload metadata.

### 2. State in `ChatSession.properties` can be overwritten by races

Two messages in the same chat session can run close together. Blindly replacing
`properties["general_agent_state"]` can lose a newer requirement.

Fix:

- Store `state_revision`, `last_user_message_id`, `last_assistant_message_id`,
  and `updated_at` inside the state.
- When saving, reload the chat session and merge only if the base revision still
  matches.
- If the revision changed, keep the newer active requirement unless the current
  run completed the same requirement id.
- Use one active requirement per session for v1. If a new unrelated intent
  starts, mark the old requirement as `superseded`.

### 3. A generic `active_requirement` is not enough

Email, calendar invite, reminder, CRM create/update, and meeting bot flows need
different required fields and confirmation rules.

Fix:

- Define a typed requirement envelope:
  - `id`
  - `kind`: `send_calendar_invite`, `send_email`, `create_reminder`,
    `crm_change`, etc.
  - `status`: `collecting`, `confirmation_required`, `confirmed`,
    `completed`, `cancelled`, `expired`, `superseded`
  - `resolved_fields`
  - `missing_fields`
  - `confirmation_payload`
  - `confirmation_reasons`
  - `source_tool`
  - `created_from_message_id`
  - `expires_at`
- Add per-tool state adapters that know which fields are safe to persist and
  how to merge follow-up user input.

### 4. Follow-up text must be routed before normal prompting

If the model sees `Confirm` as a new user request without structured state, it
can ask again or create the wrong object.

Fix:

- Before invoking GeneralAgent, detect whether there is an active requirement.
- Add a compact system/context block:
  - current active requirement
  - what fields are resolved
  - what fields are missing
  - exact confirmation action expected
  - instruction to call the same tool with `confirmed=True` only after user
    confirmation
- Add deterministic parsing for common continuation intents:
  - confirmation: `confirm`, `yes`, `go ahead`, `send it`
  - rejection: `cancel`, `no`, `don't send`
  - field update: sender email, attendee email, title, time range
- The model can still compose natural language, but the state layer should
  reduce ambiguity before prompt construction.

### 5. Confirmation must be tied to exact payload, not just user intent

The user saying `Confirm` must confirm the exact payload shown previously, not a
newly inferred payload built from partial history.

Fix:

- Save the full sanitized `confirmation_payload`.
- On confirmation, call the tool with fields from the saved payload plus
  `confirmed=True`.
- If the user changes any field after the confirmation card, return to
  `confirmation_required` and show the updated payload again.
- Use an idempotency key derived from requirement id and payload hash for
  side-effect tools.

### 6. Timezone state can still drift

The state plan does not by itself fix timezone mistakes. If a saved payload
stores only natural language like `Friday 5 PM`, later turns can reparse it
against a different anchor.

Fix:

- Store both user-facing text and canonical resolved values:
  - `start_time_text`
  - `end_time_text`
  - `timezone`
  - `start_at_utc`
  - `end_at_utc`
  - `anchor_current_datetime`
- When confirming, use canonical UTC/local fields from the saved payload, not
  reparse free text unless the user edits the time.
- Require `user_timezone` and `current_datetime` in worker metadata for web,
  WhatsApp, Slack, Teams, Google Chat, and voice. If absent, mark time fields
  as needing clarification instead of defaulting silently to UTC for meeting
  invites.

### 7. State can leak across users or channels if keyed too broadly

`ChatSession.properties` is session-scoped, but bot flows and group chats may
use generated history and metadata differently from web chat.

Fix:

- Key state by `chat_session_id` only when that id is present and verified to
  belong to the current organisation and user/session.
- Store `organisation_id`, `user_id`, `channel`, and optional `deal_id` inside
  the state envelope for validation.
- Do not reuse active requirements when the current user id differs from the
  state owner, except for explicitly supported group-chat semantics.
- For bot flows without durable `chat_session_id`, do not pretend state is
  durable; either create/resolve a real chat session or keep the flow one-turn.

### 8. Tool-specific result codes are inconsistent

Some tools return `needs_clarification`; reminders currently return
`status="error"` with `missing_fields`. A generic state reducer can miss
requirements if it only looks for one status.

Fix:

- Normalize tool outputs through state adapters.
- Treat any result with `missing_fields` as a collection requirement, regardless
  of whether the status is `error` or `needs_clarification`.
- Treat any result with `confirmation_payload` as a confirmation requirement.
- Over time, standardize tool result statuses to:
  - `needs_clarification`
  - `confirmation_required`
  - `created` / `sent` / `updated`
  - `error`

### 9. The prompt alone cannot enforce tool reuse

A system prompt saying "continue the active requirement" is not enough. The
model can still ask the wrong question or omit `confirmed=True`.

Fix:

- Add deterministic continuation helpers in code:
  - If active requirement is `confirmation_required` and user confirms, execute
    the saved tool call directly or constrain the agent context to that action.
  - If active requirement is collecting missing fields, merge extracted fields
    before agent execution.
- Keep prompts as guidance, not the only enforcement mechanism.

### 10. Sensitive content retention needs boundaries

Email bodies, meeting bodies, and CRM note content may contain sensitive data.
Storing every draft forever inside `ChatSession.properties` is risky and can
also bloat the row.

Fix:

- Store only the active requirement payload and expire it.
- Clear payload after completion, cancellation, supersession, or expiry.
- Use size limits per field and for the whole state object.
- For long bodies, store a short preview and a hash unless the body is required
  to execute the continuation. If required, clear immediately after execution.

## Revised Implementation Strategy

### Phase 1: State Infrastructure

- Add a `general_agent_state` helper module with typed dicts or Pydantic models.
- Implement:
  - load from `ChatSession.properties`
  - validate owner and revision
  - expire stale requirements
  - merge state deltas
  - clear completed/superseded requirements
  - sanitize and size-limit stored payloads
- Keep this as app state, not Agno DB state.

### Phase 2: Structured Tool State Capture

- Extend tool execution tracking to capture full structured tool results
  internally before summarizing for UI.
- Convert tool results into state deltas:
  - `missing_fields` -> collecting requirement
  - `confirmation_payload` -> confirmation requirement
  - successful side-effect statuses -> clear matching requirement
- Do not depend on truncated `ToolUsage.result_summary`.

### Phase 3: Continuation Injection And Resolution

- Load state in `MessageProcessingService._load_job_data`.
- Pass state through `AgentContext.metadata`.
- Add a compact GeneralAgent context block for active requirements.
- Add deterministic continuation handling before the agent run for simple
  confirmations/cancellations and obvious field updates.
- Ensure final confirmation uses saved payload plus `confirmed=True`.

### Phase 4: Tool Adapters

Start with the flows causing current pain:

- `send_calendar_invite`
- `send_email`
- `create_reminder`

Then extend to CRM create/update tools.

For calendar invites specifically:

- Do not accept email-address-derived titles as final without confirmation.
- Bundle missing title, sender, attendee, and time questions into one ask.
- Search deal stakeholders, CRM contacts, CRM users, and internal users before
  asking for email.
- Store canonical resolved time and timezone in the requirement.

### Phase 5: Tests

Required tests before claiming the flow is fixed:

- First turn missing multiple fields saves one active requirement and asks one
  bundled question.
- User supplies title and sender together; state merges both fields.
- User says `Confirm`; saved payload is reused and tool is called with
  `confirmed=True`.
- User edits a field after preview; confirmation is invalidated and regenerated.
- Successful send/create clears state.
- Expired requirement is ignored.
- Concurrent stale save does not overwrite newer state.
- Calendar invite keeps `Asia/Kolkata` / `Asia/Calcutta` semantics and does not
  silently default to UTC.
- Internal-user recipient resolution survives across turns.

## Final Confidence

I am confident in the revised strategy as the right next implementation path.
I am not claiming 100% production certainty until the above tests are written
and pass, because the biggest remaining risks are integration behavior and race
conditions, not the high-level architecture.

The key correction from the earlier plan is this: state persistence is not just
"save some JSON in `ChatSession.properties`." It must include structured tool
result capture, typed requirement envelopes, deterministic continuation
handling, idempotency, expiry, and ownership checks.
