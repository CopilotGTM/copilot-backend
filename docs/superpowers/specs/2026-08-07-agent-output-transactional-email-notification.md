# Agent Output Transactional Email Notification Specification

**Status:** Implemented in development; migration, deployment, and live verification pending

**Date:** 2026-08-07

**Applies to:** Agent trigger configuration, successful automated agent runs, platform email
notifications, saved-trigger presentation, and run history

**Supersedes for agent-output email:**
`2026-08-07-agent-output-delivery-email.md`. That earlier proposal used the user's connected
Gmail/Outlook mailbox for output delivery. This document replaces that decision. There is no
legacy or V2 compatibility requirement because Agent Studio is still in development.

**Related contracts:**

- `2026-08-05-ai-native-trigger-redesign.md`
- `2026-08-06-agent-trigger-picker-ui.md`
- `../../Technical Documentation/integrations/transactional-email-architecture.md`

## 1. Final product decision

CopilotGTM has two different outbound-email use cases. They must remain visibly and technically
separate:

| Use case | Example | Sender | Recipient decision | Owner |
| --- | --- | --- | --- | --- |
| Agent action email | “Send the client a follow-up” | User's connected Gmail/Outlook mailbox | The agent tool request, subject to capability and approval policy | Agent/tool runtime |
| Agent output delivery | “Send me the completed agent result” | CopilotGTM transactional sender, normally `noreply@copilotgtm.com` through `FROM_EMAIL` | Derived by the server from the run principal or audience | Shared agent-output delivery service |

The trigger form configures only the second use case. It never chooses a Gmail/Outlook sender,
client address, Cc, Bcc, or custom subject.

The hard rules are:

1. A connected mailbox is used only when the agent is instructed and authorized to send an email
   as the user.
2. A successful triggered run may send a platform-owned output notification from CopilotGTM.
3. The model cannot choose the notification sender, recipient, subject, template, or number of
   sends.
4. The output is persisted once. Notification retries never rerun the model.
5. The default remains **Run history only**. Email starts only after the trigger editor explicitly
   selects it.
6. Do not create a trigger V1/V2 fork, a legacy adapter, or HubSpot/Salesforce integration for this
   feature.

This is not a terse alert that says only “the agent ran” or “10 tasks completed.” The delivered
artifact is the agent's complete final Markdown response. If the run used ten tools or performed ten
business actions, the final Markdown should explain what was attempted, what succeeded, what
failed, and the resulting records or next steps according to the agent prompt. Delivery adds trusted
run context around that output; it does not replace or regenerate it.

### 1.1 Channel model

Run history is the permanent in-product channel and source of truth. External delivery channels
make the same completed result available without requiring the user to open Agent Studio first.

The first external channel is Email. Slack, Teams, and WhatsApp may be added later as adapters over
the same canonical delivery message. Agent execution, tool use, output persistence, recipient
authorization, and per-channel delivery status remain separate concerns.

## 2. Meaning of execution authority

Execution authority answers **whose permissions and data scope the agent uses**. It does not
select the From address of the output notification.

### 2.1 Run as me

**Run as me** is available to ordinary members and admins.

- The agent executes as that user.
- Personal CRM record triggers activate only for records the user owns or created, according to
  the existing trigger scope contract.
- Email-received triggers observe the user's eligible connected inboxes. Any inbox selector under
  **When** decides which incoming emails can activate the trigger; it does not control where the
  agent result is sent.
- **Email me** sends the output notification to the execution principal's current active workspace
  email address.
- The notification is from CopilotGTM. It does not appear in the user's Gmail/Outlook Sent folder.

### 2.2 Run as organisation

**Run as organisation** is available only to organisation admins. Members cannot create, change,
activate, pause, or archive an organisation-service trigger.

- The agent executes once as the organisation service within the explicitly allowed organisation
  scope.
- The creating admin is not the permanent owner or implicit recipient of the shared trigger.
- Output notification recipients come from the run's audience.
- `explicit_users` emails the selected active workspace users.
- `subject_participants` emails the tenant-validated internal participants pinned from the event
  snapshot.
- `none` has no recipient, so **Email audience** is unavailable until a non-empty audience is
  selected.
- Members may receive an organisation notification when they are in the selected audience, even
  though they cannot configure the organisation trigger.

Audience controls discovery and notification fan-out. It never grants execution data access.

### 2.3 Authority and notification matrix

| Authority | Who may configure it | Execution identity | Output option | Recipient source |
| --- | --- | --- | --- | --- |
| Run as me | Member or admin | Current user | Email me | Current active workspace email of the execution principal |
| Run as organisation + explicit users | Admin only | Organisation service | Email audience | Pinned selected workspace users, revalidated at send time |
| Run as organisation + subject participants | Admin only | Organisation service | Email audience | Pinned internal event participants, revalidated at send time |
| Run as organisation + no audience | Admin only | Organisation service | Run history only | None |

## 3. End-to-end examples

### 3.1 Email received, run as me

1. Alice connects `alice@acme.com` to CopilotGTM.
2. Alice saves **When Email received**, optionally narrowing **When** to that connected inbox.
3. Alice selects **Run as me** and **Email me**.
4. A client email arrives in Alice's connected inbox and produces a durable trigger event.
5. The agent runs as Alice, creates a lead with the lead tool, and stores final Markdown such as the
   created lead, fields populated, actions completed, and any warnings.
6. CopilotGTM emails that complete Markdown result plus trusted trigger/run context to Alice's
   current workspace email from the platform transactional sender.
7. If the agent instructions separately say “reply to the client,” that action uses Alice's
   connected mailbox through the email tool and its approval policy. It is a separate side effect.

### 3.2 Deal created, run as me

A personal **Deal created** trigger does not run for every deal in the organisation. It runs only
when the deal satisfies the personal ownership/creation scope. **Email me** then notifies the same
execution principal at their workspace email.

### 3.3 Deal created, run as organisation

An admin may create one shared organisation trigger for all matching organisation deals and choose
an explicit audience such as Revenue Operations. The agent runs once per matching event. The
platform creates one notification target per active audience user; it does not send from the
admin's mailbox and it does not execute the agent once per recipient.

## 4. Frontend contract

### 4.1 Placement and choices

Place this section after **Task goal**, under the eyebrow **Then**:

```text
Then
How to send output

[ Run history only ]  [ Email me ]

Email me
CopilotGTM will email your workspace address after a successful run.

To       You · Alice Morgan · alice@acme.com
From     CopilotGTM
Content  Complete final Markdown output, run context, and a link to the full run
```

For organisation execution, change only the recipient label:

```text
[ Run history only ]  [ Email audience ]

Email audience
CopilotGTM will notify the internal users selected under Audience.

To       Revenue Operations · 4 workspace users
From     CopilotGTM
Content  Complete final Markdown output, run context, and a link to the full run
```

For `subject_participants`, show:

```text
To       Internal subject participants determined when the event occurs
```

These are read-only summaries, not editable form controls.

### 4.2 Controls that must not exist

The output-notification section must not render:

- a Gmail/Outlook From picker;
- a mailbox integration ID;
- editable To, Cc, or Bcc fields;
- a custom subject input;
- a body editor;
- HubSpot or Salesforce choices;
- an “organisation sender” setup prompt.

Connected inbox selectors may still appear under **When** for email source events. Their label must
make the distinction clear, for example **Incoming account**, not **From**.

### 4.3 Business-readable presentation

- Show people as display name plus workspace email.
- Show connected source inboxes as email address plus provider.
- Show audience groups by name and member count.
- Never ask a business user to type or interpret a UUID.
- IDs remain machine values sent only by pickers populated from tenant-scoped server options.

### 4.4 Authority transitions

- Switching from **Run as me** to **Run as organisation** requires admin eligibility.
- If the saved organisation audience is `none`, switch delivery to **Run history only** and explain
  why before save.
- Switching from organisation to personal automatically changes the derived recipient to **You**.
- Never preserve hidden sender, recipient, Cc/Bcc, or subject values because those values are not
  part of this contract.
- A non-admin opening an existing organisation trigger receives a read-only view and cannot submit
  a forged personal/organisation transition.

### 4.5 Saved triggers and run history

A saved trigger card shows one of:

- `Output · Run history only`
- `Output · Email you from CopilotGTM`
- `Output · Email 4 audience members from CopilotGTM`
- `Output · Email internal subject participants from CopilotGTM`

Run history keeps execution and notification status separate:

```text
Agent run            Succeeded
Output notification  Sent to you
```

For organisation fan-out:

```text
Agent run            Succeeded
Output notification  Sent to 3 of 4 · 1 failed
```

Supported presentation states are Pending, Sending, Sent, Partially sent, Failed, and Skipped.
Notification failure never changes a successful agent run to Failed.

## 5. Canonical API contract

Continue using the current `delivery` discriminator on the trigger decision. Replace the
mailbox-specific development shape rather than adding another version.

Run history only:

```json
{
  "delivery": {
    "mode": "none"
  }
}
```

Platform output notification:

```json
{
  "delivery": {
    "mode": "email_notification"
  }
}
```

No other client-supplied delivery fields are valid. In particular, reject
`sender_integration_id`, `from`, `to`, `cc`, `bcc`, `subject`, `body`, `template`, and credentials.

The server derives and pins only the decision needed by the run:

- delivery mode from the trigger snapshot;
- recipient user IDs from the execution principal or immutable run audience;
- template type and version from code;
- sender from platform configuration;
- subject, content, and deep link from trusted run data.

Trigger edits affect future runs only. An already-created run retains its pinned delivery-mode and
audience decisions, while recipient membership and active status are revalidated immediately
before sending.

## 6. Catalog contract

The backend catalog remains authoritative. The frontend must render the choices and denial reasons
instead of guessing from tools, integrations, or role names.

```json
{
  "delivery_channels": [
    {
      "key": "none",
      "label": "Run history only",
      "description": "Keep the completed output in CopilotGTM.",
      "principal_modes": ["personal", "organisation"]
    },
    {
      "key": "email_notification",
      "label": "Email notification",
      "description": "CopilotGTM emails the complete final Markdown output and run context after a successful run.",
      "principal_modes": ["personal", "organisation"],
      "sender_mode": "platform",
      "recipient_mode_by_principal": {
        "personal": "execution_principal",
        "organisation": "run_audience"
      },
      "requires_audience_modes": ["explicit_users", "subject_participants"]
    }
  ]
}
```

The frontend label is contextual: **Email me** for personal execution and **Email audience** for
organisation execution. It does not need a mailbox option-source request for output notification.

## 7. Recipient resolution

### 7.1 Personal run

1. Start from `AgentRun.execution_principal_id`.
2. Require that user to remain active in the run's organisation.
3. Load the user's current verified workspace email from the master user record.
4. Create one delivery target keyed by user ID.
5. If the user is inactive, removed, or has no usable email, mark the target Skipped with a safe
   reason. Do not substitute the trigger creator, an integration owner, or a connected mailbox.

### 7.2 Organisation run

1. Start from the audience user IDs pinned on the run at activation time.
2. Preserve that event-time audience; do not query mutable CRM participants to discover new users.
3. At send time, require each pinned user to remain active in the same organisation.
4. Resolve each current verified workspace email and create one independent target per user.
5. Deduplicate by user ID before creating rows.
6. Skip ineligible targets; do not replace them with the creating admin or all admins.

Only internal workspace users are valid recipients. External meeting attendees, CRM contacts,
client addresses, and arbitrary email strings are not output-notification recipients.

## 8. Canonical delivery message and Email rendering

The transactional template is code-owned and versioned as `agent_output_ready`.

Every successful agent run selected for external delivery produces one canonical, channel-neutral
message from persisted trusted data:

| Part | Source |
| --- | --- |
| Agent output Markdown | The run's persisted final output; never a second model call |
| Agent | Pinned agent name and revision from the run |
| Trigger | Pinned trigger name and event key from the run |
| Trigger reason | Safe summary derived from the immutable event snapshot |
| Subject | Safe business label and record type when available |
| Timing | Triggered, started, and completed timestamps |
| Result | Successful run status and tool/action summary already expressed by the final output |
| Link | Authenticated deep link to the complete run details |

The agent output is the main content. Context explains why and when it ran; context must not reduce
the output to a generic completion count. For example, if an email trigger led the agent to create a
lead and update two CRM fields, the delivered Markdown contains the final agent response describing
those outcomes, not merely “3 tasks completed.”

Subject:

```text
CopilotGTM — {agent_name} completed
```

The email contains:

- recipient greeting;
- agent name and trigger name;
- successful completion time;
- a safe business label for the triggering subject when available;
- the complete persisted final agent output rendered from canonical Markdown;
- a **View full result** button linking to the authenticated run detail page.

Content rules:

- Text output uses persisted `AgentRun.output_text` as Markdown with outer whitespace trimmed.
- Structured JSON output is rendered deterministically with two-space indentation, sorted keys,
  and UTF-8 characters preserved, then wrapped in a fenced `json` block within the canonical
  Markdown message.
- The Email adapter converts the canonical Markdown through one shared safe renderer into styled
  HTML and a plain-text fallback.
- Raw model-authored HTML is never trusted. The renderer escapes or removes unsafe HTML, scripts,
  event handlers, and unsafe links before provider submission.
- Headings, paragraphs, lists, emphasis, links, block quotes, inline code, and fenced code blocks
  must remain readable in the email.
- Up to 100,000 characters of the rendered output may be included inline.
- If the output is larger, include a clearly labelled first 10,000-character preview and the link;
  do not fail the notification and do not imply that the preview is complete.
- Do not copy the output into delivery metadata. The worker reads the already-persisted run result.
- Do not include prompts, credentials, hidden tool traces, internal snapshots, or provider errors.

The sender is the platform-configured `FROM_EMAIL` with the CopilotGTM display name. The frontend
must not hard-code a domain address because environment configuration owns the actual sender.

### 8.1 Future channel adapters

The common agent-output delivery service owns canonical message construction, authorization,
idempotency, audit, and status. Channel adapters own presentation and provider I/O only:

| Channel | Presentation |
| --- | --- |
| Run history | Full persisted input, output, status, timestamps, and run diagnostics |
| Email | Safe rendered HTML plus plain-text fallback from the complete Markdown message |
| Slack | Slack-compatible Markdown blocks plus the run link |
| Teams | Teams-compatible Markdown/card content plus the run link |
| WhatsApp | Readable plain-text Markdown degradation plus the run link |

Adding another channel must not add a new agent execution path, ask the model to rewrite the
result, or duplicate tool side effects.

## 9. Durable runtime design

Reuse the generic `notification_deliveries` ledger. Do not add or retain a separate
`agent_run_deliveries` table solely for this feature.

Create one row per recipient with:

| Field | Value |
| --- | --- |
| `organisation_id` | Run organisation |
| `notification_type` | `agent_output_ready` |
| `source_type` | `agent_run` |
| `source_id` | `run_id` |
| `channel` | `email` |
| `target_key` | Recipient user ID as the stable machine key |
| `target_label` | Optional business-readable user label; not an authorization input |
| `delivery_metadata` | Minimum template/version and correlation metadata; no output or address |

The existing unique key on organisation, notification type, source, channel, and target is the
per-recipient idempotency boundary.

The completion flow is:

```text
Trigger event
  -> one AgentRun is activated
  -> worker executes the model once
  -> one transaction stores Succeeded Markdown output and PENDING delivery targets
  -> post-commit publish of exact delivery IDs
  -> shared delivery worker claims one target
  -> reload run and active recipient under tenant checks
  -> build the canonical agent-output message from persisted run data
  -> Email adapter renders agent_output_ready through the transactional template registry
  -> send through the Resend platform adapter
  -> persist provider receipt and mark SENT
```

A recovery scan republishes stale Pending targets and handles stale Sending claims. The worker task
contains only organisation scope and delivery ID; it never accepts email content or an address from
the trigger payload.

The channel-neutral agent-output delivery service is common to every agent and trigger. Its Email
adapter uses the platform transactional-email facade and worker. Do not build per-agent email
services, call the user-mailbox `EmailSendService`, or perform provider I/O inside the model
execution transaction.

## 10. Failure, retry, and idempotency semantics

- A successful model run remains Succeeded regardless of notification outcome.
- Each recipient target retries independently, so one invalid audience member does not block the
  others.
- Duplicate completion, broker, and recovery messages converge on the existing unique delivery
  row.
- Use `agent-output:{delivery_id}` as the deterministic provider idempotency identity when the
  provider adapter supports it.
- Failures proven to occur before the provider call may retry with bounded exponential backoff and
  jitter.
- A provider rejection is terminal for that target.
- If a worker can no longer prove whether the provider accepted the message, do not automatically
  make a second provider call unless the provider idempotency contract makes that replay safe.
  Present the target as Failed/Needs attention.
- An explicit retry operates only on the notification target. It never reactivates the trigger or
  reruns the agent.
- A run with zero eligible recipients records a visible Skipped notification outcome.

Run-history aggregation is derived from per-target rows:

- all Pending/Sending -> Pending or Sending;
- all Sent -> Sent;
- mixture of Sent and Failed/Skipped -> Partially sent;
- all Failed -> Failed;
- all Skipped -> Skipped.

## 11. Authorization, privacy, and audit invariants

1. Every run and delivery lookup is scoped by `organisation_id`.
2. Personal delivery requires the execution principal to remain an active organisation member.
3. Organisation delivery requires an admin-authorized binding and a tenant-validated pinned
   audience.
4. Output is sent only to users who may discover that run; delivery never broadens visibility.
5. The worker revalidates recipient membership immediately before external I/O.
6. The trigger request cannot provide a sender, recipient address, provider token, template, or
   arbitrary HTML.
7. The LLM cannot add recipients or cause multiple output notifications.
8. Logs contain organisation, run, delivery, status, template, and safe error codes—not output,
   prompts, tokens, message bodies, or full recipient addresses.
9. Provider credentials remain in Key Vault/configuration and are never copied into organisation
   tables.
10. The connected Gmail/Outlook mailbox path remains governed by its separate ownership, scope,
    audit, approval, draft, and threading rules.

## 12. Implementation sequence

This is the smallest vertical sequence that proves the corrected design:

1. Replace the current development-only mailbox delivery schema with `none | email_notification`.
   Remove sender integration, recipient, subject, and body decisions; do not add a version branch.
2. Replace catalog mailbox metadata with platform sender and derived-recipient metadata.
3. Add one canonical agent-output Markdown message builder over persisted run output and immutable
   trigger context.
4. Add the `agent_output_ready` transactional Email template, safe Markdown renderer, and recipient
   resolver over active workspace users.
5. On successful run completion, insert generic `NotificationDelivery` rows in the same
   organisation transaction as the durable output and publish exact IDs after commit.
6. Add the worker/recovery path over the shared transactional email provider, including safe
   idempotency and unknown-outcome handling.
7. Read and aggregate generic delivery rows in run history; keep execution and notification states
   independent.
8. Simplify the frontend to **Run history only**, **Email me**, and **Email audience**, with derived
   read-only To/From summaries.
9. Delete the development-only `agent_run_deliveries` migration/model/service/tests if their only
   purpose is the superseded mailbox-delivery design.
10. Prove the personal walking skeleton first, then organisation fan-out, before enabling the UI.

No step includes HubSpot, Salesforce, an organisation mailbox, arbitrary recipients, or a trigger
V2.

## 13. Walking skeleton

The first end-to-end proof is deliberately narrow:

1. A personal **Deal created** trigger is saved with **Email me**.
2. A qualifying owned deal event creates one run.
3. The run creates a lead and succeeds with persisted Markdown describing that action and result.
4. The same completion transaction creates one Pending generic notification row for the execution
   principal.
5. A fake transactional provider receives one platform email containing the complete safely
   rendered Markdown, trigger reason, run metadata, and run link.
6. The row becomes Sent and run history shows `Succeeded / Sent to you`.
7. Replaying completion, publish, and worker messages produces no second provider request.

The second proof uses one organisation run and two pinned audience users, demonstrating one model
execution, two independent notification rows, and partial-failure presentation.

## 14. Acceptance criteria

1. The default is Run history only and creates no email intent.
2. Run as me is available to members and admins; Run as organisation is admin-only.
3. A member cannot create or mutate an organisation-service trigger with a forged request.
4. The frontend never shows a mailbox picker for output notification.
5. The frontend never asks for To, Cc, Bcc, subject, body, or a UUID.
6. Email source-account filters remain under When and cannot be mistaken for the notification
   sender or recipient.
7. `email_notification` accepts no client-supplied sender or recipient data.
8. A personal notification resolves only the active execution principal's workspace email.
9. An organisation notification resolves only the pinned, tenant-valid internal run audience.
10. Audience `none` cannot save Email audience.
11. Removing a user before send skips that target without substituting another user.
12. One organisation run with multiple recipients executes the model once.
13. Each recipient has an independent, unique generic notification row.
14. Duplicate completion/publication/worker messages do not create duplicate provider calls.
15. Delivery publication failure is recoverable without rerunning the model.
16. The canonical delivery message is derived only from persisted successful output and immutable
   run context; delivery never makes a second model call.
17. Persisted text output is treated as Markdown and safely rendered to Email HTML plus a plain-text
   fallback.
18. Persisted JSON output is deterministic and appears in a fenced `json` block within the
   canonical Markdown.
19. An agent that performs ten actions delivers its complete final Markdown explanation; the
   delivery service does not replace it with “10 tasks completed.”
20. Email includes the trigger reason, safe subject information, completion metadata, and the
   authenticated full-run link.
21. Output larger than 100,000 characters sends a labelled 10,000-character preview and full-result
   link instead of failing or pretending the preview is complete.
22. The From identity is the platform transactional sender, never the user's connected mailbox.
23. An agent-instructed client email still uses the separate connected-mailbox tool path.
24. A notification failure never changes AgentRun from Succeeded to Failed.
25. Organisation fan-out can show partial success without hiding the failed recipient count.
26. Logs and API errors do not expose output, prompts, tokens, message bodies, or full recipient
   lists.
27. Trigger edits do not reinterpret already-created runs.
28. Email is the first adapter; later Slack, Teams, and WhatsApp adapters reuse the same canonical
   output without rerunning the agent or its tools.
29. The implementation contains no trigger V1/V2 fork and no HubSpot/Salesforce work.

## 15. Explicitly deferred

- arbitrary external recipients;
- Cc/Bcc and distribution-list strings;
- custom subjects, templates, or model-authored bodies;
- sending the notification through a user's connected mailbox;
- attachments or an exact-output file export;
- Slack, Teams, WhatsApp, or webhook adapters beyond the canonical channel contract defined here;
- notifications for failed/cancelled runs;
- user-level notification preference controls beyond the saved trigger decision;
- email replies or conversation threading from the transactional sender.

These additions require separate product decisions. They are not reasons to keep the superseded
mailbox-delivery implementation or to create a parallel trigger contract.
