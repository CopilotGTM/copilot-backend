# Agent Trigger Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement and maintain the Agents V1 trigger builder and run-output experience against the catalog-owned trigger contract and the platform-owned transactional output-notification contract.

**Architecture:** The backend is the source of truth for trigger definitions, fields, options, authority eligibility, audience policy, saved-trigger presentation, and run/delivery state. The frontend owns progressive disclosure, local form state, accessible presentation, and cache invalidation; it submits only user decisions. A successful agent run is read from the existing run-detail endpoint, while notification repair targets the existing delivery ledger through one explicit retry mutation. No second trigger contract, output endpoint, connected-mailbox output path, HubSpot/Salesforce integration, or V1/V2 compatibility branch is permitted.

**Tech Stack:** Next.js App Router, React 19, TypeScript, React Query, Zustand auth state, existing shadcn-style primitives, Zod response parsing, Jest, Prettier, ESLint, and the authenticated CopilotGTM `/users/v1` API.

**Implementation status:** Completed in the development worktrees on 2026-08-08. Source, focused
tests, type checking, linting, formatting, and the production frontend build are verified. Migration
application, deployment, worker/provider configuration, and live email delivery remain separate
release gates.

---

## Review result and current source of truth

The current implementation already contains the main vertical slice:

- `src/modules/agents-v1/components/trigger-builder-sheet.tsx` renders the catalog-driven source/event picker, typed configuration, watches, filters, authority, audience, and output delivery choice.
- `src/modules/agents-v1/components/agent-detail-page.tsx` renders saved-trigger presentation, run history, persisted output, delivery state, and the run deep link.
- `src/modules/agents-v1/types/agents-v1.types.ts` and `src/modules/agents-v1/types/agents-v1.schemas.ts` model and validate the catalog, eligibility, decision-only trigger payload, and run-delivery response.
- `src/modules/agents-v1/api/agents-v1.api.ts` and `src/modules/agents-v1/hooks/use-agent-automation.ts` call the catalog, option, trigger, run, and delivery-retry routes.
- Backend contracts are defined by `docs/superpowers/specs/2026-08-06-agent-trigger-picker-ui.md` and `docs/superpowers/specs/2026-08-07-agent-output-transactional-email-notification.md`.

The review found two gaps, both resolved during plan execution:

1. `docs/API Documentation/automation/v1-agent-triggers-api.md` and `v1-agent-runs-api.md` now describe `email_notification`, the independent delivery lifecycle, per-target summaries, and the target retry route.
2. The run row now renders every delivery target with its own status, error, and retry action. The selected target's `delivery_id` is sent independently, and retrying never reruns the agent.

The current verification evidence is source-level and focused-test evidence. Migration application, backend/worker restart, provider credentials, deployment, and live email delivery remain separate release gates.

## 1. Product rules the UI must make obvious

### 1.1 Two email use cases remain separate

| User action | Sender | Recipient | Frontend location |
| --- | --- | --- | --- |
| Agent instruction says to email a client | User's connected Gmail/Outlook account through the existing agent email tool | Client or other tool-specified recipient, subject to tool policy | Agent prompt/tool capability; not the trigger form |
| Triggered run should notify the user of its result | CopilotGTM transactional sender | Execution principal or pinned internal audience | Trigger form **Then → How to send output** |

The trigger form must never ask for a connected mailbox, From address, To/Cc/Bcc, subject, body, credentials, or arbitrary recipient string for output notification. Email source-account selectors remain valid under **When** because they select which incoming source facts can trigger the run; they are not notification senders.

### 1.2 Authority and scope are different decisions

- **Run as me** is available to an eligible member or admin. It executes as the authenticated user, applies the personal owned-or-created record boundary, and delivers a personal output notification only to that execution principal's current verified workspace email.
- **Run as organisation** is available only when the backend marks the organisation mode eligible and the current caller has admin authority. It executes as the organisation service and requires the catalog-supported audience mode: `none`, `explicit_users`, or `subject_participants`.
- Personal delivery does not mean “all records connected to my mailbox” or “all organisation records.” It means the personal event scope and current execution principal defined by the backend.
- The frontend may disable choices for usability, but the backend remains the authorization boundary.

### 1.3 Output delivery is a decision-only discriminator

```ts
type TriggerDeliveryDecision =
  | { mode: "none" }
  | { mode: "email_notification" };
```

The request must contain no other delivery property. The server derives the sender, recipient IDs, template, subject, content, and deep link from the immutable run context. The default is `{ mode: "none" }`.

## 2. Contract and endpoint map

The API client base URL already carries the authenticated `/users` prefix. Feature functions therefore use the route suffixes below.

| Method | Route suffix | Frontend responsibility |
| --- | --- | --- |
| `GET` | `/automation-trigger-catalog` | Load the authoritative picker and delivery catalog when the builder opens |
| `POST` | `/automation-trigger-options/search` | Search tenant-scoped source, filter, audience, or manual-record options |
| `POST` | `/automation-trigger-options/resolve` | Resolve saved IDs into business labels/status before edit or display |
| `GET` | `/agents/{agent_id}` | Load `AgentDetail`, including `automation_eligibility` |
| `GET` | `/agents/{agent_id}/triggers` | Load saved-trigger cards and backend presentation |
| `GET` | `/agents/{agent_id}/triggers/{trigger_id}` | Load complete editable decisions plus concurrency timestamp |
| `POST` | `/agents/{agent_id}/triggers` | Create one decision-only binding |
| `PUT` | `/agents/{agent_id}/triggers/{trigger_id}` | Replace all decisions with `expected_updated_at` |
| `POST` | `/agents/{agent_id}/triggers/{trigger_id}/refresh-target` | Explicitly repin an old trigger to the latest agent revision |
| `POST` | `/agents/{agent_id}/triggers/{trigger_id}/invoke` | Invoke an existing active manual trigger; does not create the binding |
| `POST` | `/agents/{agent_id}/triggers/{trigger_id}/rotate-webhook-secret` | Rotate and display one-time webhook credentials |
| `DELETE` | `/agents/{agent_id}/triggers/{trigger_id}` | Archive a binding with `expected_updated_at` |
| `GET` | `/agent-runs` | Cursor-page visible run summaries and independent delivery summaries |
| `GET` | `/agent-runs/{run_id}` | Load persisted full output and immutable run snapshots |
| `POST` | `/agent-runs/{run_id}/delivery/retry` | Requeue one safe pre-provider delivery target; never rerun the agent |

### 2.1 Trigger write payload

```json
{
  "name": "Review material deal stage changes",
  "trigger_key": "crm.deal.stage_changed",
  "status": "active",
  "config": {},
  "watch": null,
  "filter": {
    "all": [
      {"path": "record.amount", "operator": "gte", "value": "50000"}
    ]
  },
  "principal_mode": "personal",
  "scope": {"type": "owned_or_created_by_principal", "config": {}},
  "audience": {"mode": "principal", "user_ids": []},
  "task": {"goal": "Review the deal and recommend the next action."},
  "delivery": {"mode": "email_notification"}
}
```

Never send `family`, `match_event_keys`, execution-principal IDs, hashes, snapshots, counters, `sender_integration_id`, mailbox IDs, email addresses, custom subject/body, provider credentials, or compiled fixed rules.

### 2.2 Agent detail eligibility

```ts
type AgentAutomationEligibility = {
  personal: {
    available: boolean;
    reason_code:
      | "retry_unsafe_tools"
      | "organisation_requires_admin"
      | "organisation_tools_not_supported"
      | "organisation_skills_not_supported"
      | "no_compatible_principal_mode"
      | null;
    reason: string | null;
  };
  organisation: /* same shape */;
};
```

Intersect `definition.principal_modes` with this object. Do not infer eligibility from tool count, tool names, skill names, or the current user's role alone.

### 2.3 Run and delivery response

The run list remains cursor-based. Delivery is independent from `AgentRun.status`:

```ts
type AgentRunDeliverySummary = {
  channel: "email";
  status:
    | "pending"
    | "sending"
    | "sent"
    | "partially_sent"
    | "failed"
    | "skipped";
  recipient_count: number;
  sent_count: number;
  failed_count: number;
  skipped_count: number;
  can_retry: boolean;
  sent_at: string | null;
  targets: Array<{
    delivery_id: string;
    status: "pending" | "sending" | "sent" | "failed" | "skipped";
    error_code: string | null;
    error_message: string | null;
    can_retry: boolean;
    sent_at: string | null;
  }>;
};
```

The detail endpoint already returns `output_text`, `output_json`, `trigger_snapshot`, and `agent_snapshot`; do not invent an output-only endpoint.

## 3. Trigger-builder implementation

### Task 1: Load and normalize the catalog

**Files:**
- Modify: `src/modules/agents-v1/api/agents-v1.api.ts`
- Modify: `src/modules/agents-v1/hooks/use-agent-automation.ts`
- Modify: `src/modules/agents-v1/types/agents-v1.types.ts`
- Modify: `src/modules/agents-v1/types/agents-v1.schemas.ts`
- Test: `__tests__/unit/modules/agents-v1/agents-v1.api.test.ts`
- Test: `__tests__/unit/modules/agents-v1/agents-v1.schemas.test.ts`

- [ ] Parse the catalog with strict Zod schemas. Reject unknown delivery keys and malformed eligibility metadata rather than silently defaulting to a client-owned catalog.
- [ ] Keep stable `trigger_key`, `resource_key`, and option values as machine data; render labels and descriptions only from the server response.
- [ ] Cache the catalog for five minutes with React Query and refetch when the builder is opened after an error.
- [ ] Verify the API tests assert the exact `/automation-trigger-catalog`, option-search, and option-resolve request bodies.

### Task 2: Implement source → event progressive disclosure

**Files:**
- Modify: `src/modules/agents-v1/components/trigger-builder-sheet.tsx`
- Test: `__tests__/unit/modules/agents-v1/trigger-builder-sheet.test.tsx`

- [ ] Open create mode with no definition selected and a disabled primary action.
- [ ] Build source rows from `categories[].resources[]`; curate visual ordering only, never the valid-trigger list.
- [ ] Group events into Primary, Common updates, and More events using catalog metadata. Show `rule_presentation` as a fixed, non-removable explanation.
- [ ] Search category label, resource label, event label, description, and server-approved aliases. Every result must display a breadcrumb.
- [ ] On mobile, show one drill-in screen at a time with a keyboard-accessible Back control. On desktop, show source and event columns.
- [ ] Preserve only compatible form values after **Change**. Remove invalid values explicitly and announce the change.

### Task 3: Render server-owned configuration, watches, filters, and options

**Files:**
- Modify: `src/modules/agents-v1/components/trigger-builder-sheet.tsx`
- Modify: `src/modules/agents-v1/types/agents-v1.schemas.ts`
- Test: `__tests__/unit/modules/agents-v1/trigger-builder-sheet.test.tsx`

- [ ] Initialize configuration from `default_config` and render fields in `config_fields` order.
- [ ] Respect `required`, `depends_on`, `input_type`, literal `options`, and `option_source`; preserve `false`, `0`, and empty optional lists correctly.
- [ ] Render only standard filter fields whose `principal_modes` include the selected authority. Treat paths as opaque strings.
- [ ] Render only operators advertised by the selected field. Omit values for `is_empty` and `not_empty`; require typed values for every other operator.
- [ ] Enforce catalog limits for two group levels and 20 leaf conditions before save.
- [ ] Use option search/resolve for dynamic source, filter, audience, and direct-subject values. Never show a raw UUID or fall back to a UUID when resolution fails.
- [ ] Render IANA timezone choices for schedules and show the computed first-run preview when the local value is valid.
- [ ] Keep manual binding creation separate from manual invocation. The invocation dialog sends a fresh `invocation_id` and stable subject IDs only after the user chooses the existing trigger.
- [ ] Display webhook credentials only in the one-time post-create/rotate dialog; do not put secrets in query state, analytics, or normal React Query cache.

### Task 4: Implement authority, audience, and delivery choices

**Files:**
- Modify: `src/modules/agents-v1/components/trigger-builder-sheet.tsx`
- Modify: `src/modules/agents-v1/components/agent-detail-page.tsx`
- Test: `__tests__/unit/modules/agents-v1/trigger-builder-sheet.test.tsx`

- [ ] Present **Run as me** and **Run as organisation** as cards with server-provided reason text.
- [ ] Disable unavailable modes and keep the submit action disabled when no mode is compatible.
- [ ] Show audience controls only for organisation mode and only for catalog-advertised audience modes.
- [ ] Search explicit workspace users through `control="audience"`; show name/email labels and send only IDs returned by the option API.
- [ ] When switching personal → organisation, clear personal delivery and require a valid organisation audience before selecting **Email audience**.
- [ ] When switching organisation → personal, clear audience IDs and derive the recipient as **You**.
- [ ] Open a saved organisation trigger read-only when the eligibility object denies the current caller. Disable all controls, the active switch, and save; do not hide the reason.
- [ ] Render output choices as **Run history only**, **Email me**, and **Email audience**. Show read-only To/From/Content summaries. The From summary is `CopilotGTM`; it is not a mailbox field.
- [ ] Validate that organisation email requires `explicit_users` or `subject_participants`; `none` remains run-history-only.

### Task 5: Implement saved-trigger cards and state handling

**Files:**
- Modify: `src/modules/agents-v1/components/agent-detail-page.tsx`
- Modify: `src/modules/agents-v1/hooks/use-agent-automation.ts`
- Test: `__tests__/unit/modules/agents-v1/agent-detail-page.test.tsx`
- Test: `__tests__/unit/modules/agents-v1/use-agent-automation.test.tsx`

- [ ] Render backend `presentation` directly: friendly breadcrumb, authority, scope, fixed rule, source summary, delivery summary, conditions, warnings, and health state.
- [ ] Display Active, Paused, Completed, and Needs attention without deriving status from raw trigger keys.
- [ ] Keep target-revision refresh explicit and preserve optimistic concurrency on every trigger replace/archive/refresh mutation.
- [ ] Invalidate only the relevant trigger list/detail queries after a write; invalidate run lists after manual invocation.
- [ ] Preserve one-time webhook credentials outside normal trigger cache and clear them after the dialog closes.
- [ ] Keep the agent page usable when trigger loading fails; provide a targeted retry state.

### Task 6: Implement run history and persisted output

**Files:**
- Modify: `src/modules/agents-v1/api/agents-v1.api.ts`
- Modify: `src/modules/agents-v1/hooks/use-agent-automation.ts`
- Modify: `src/modules/agents-v1/components/agent-detail-page.tsx`
- Test: `__tests__/unit/modules/agents-v1/agent-detail-page.test.tsx`
- Test: `__tests__/unit/modules/agents-v1/agents-v1.api.test.ts`
- Test: `__tests__/unit/modules/agents-v1/use-agent-automation.test.tsx`

- [ ] List runs with `agent_id` and cursor pagination. Poll while the run is `pending`/`running` or its notification is `pending`/`sending`; stop after terminal state.
- [ ] Open `GET /agent-runs/{run_id}` when the user selects a run or uses `?run_id=`. Render the persisted Markdown through the existing safe renderer.
- [ ] Render `output_json` as a readable fenced `json` block when `output_text` is absent. Do not invoke a model or provider to build the display.
- [ ] Show Agent run status and Output notification status as separate rows. A successful run stays successful when email fails.
- [ ] Show recipient counts, sent/failed/skipped counts, error text, and delivery timestamps without exposing provider IDs or full recipient lists.
- [ ] Replace the current single “first retryable target” action with a target list: each retryable target has its own button and `delivery_id`; a fan-out retry must never retry another target implicitly.
- [ ] Call `POST /agent-runs/{run_id}/delivery/retry` with `{ "delivery_id": "..." }`. Invalidate the run detail and run-list queries after acceptance. Never call trigger activation or agent execution from this action.

### Task 7: Synchronize API documentation

**Files:**
- Modify: `docs/API Documentation/automation/v1-agent-triggers-api.md`
- Modify: `docs/API Documentation/automation/v1-agent-runs-api.md`
- Create/update: `docs/superpowers/plans/2026-08-08-agent-trigger-frontend-implementation.md`

- [ ] Add `delivery_channels` and the `none | email_notification` discriminator to the trigger catalog/write examples.
- [ ] Remove obsolete mailbox-specific output fields from examples and state that connected Gmail/Outlook applies only to agent-instructed client email tools.
- [ ] Add `delivery` summaries and per-target status to run-list/detail examples.
- [ ] Add the delivery retry route and its safe-retry/error semantics.
- [ ] Remove the obsolete `delivery_failed` run status from frontend-facing examples if the backend enum no longer exposes it.
- [ ] Link this plan from the API pages so frontend engineers have one current implementation handoff.

## 4. Error and security behavior

| Response | UI behavior |
| --- | --- |
| `400` / `422` | Preserve form values and show the sanitized backend message beside the closest control |
| `401` | Refresh/authenticate through the existing client boundary; do not retry a mutation blindly |
| `403` | Keep organisation controls unavailable and explain that admin authority is required |
| `404` | Remove stale detail state and return to the owning list/detail surface |
| `409` | Refetch the latest trigger, show a conflict message, and require explicit review before resubmitting |
| `413` | Explain the webhook size limit and keep the credential/form state intact |
| `429` | Respect client retry behavior; never poll in a tight loop |
| `500` | Keep current state and offer deliberate retry |

The frontend must never log or expose provider IDs, secrets, credentials, raw prompts, hidden tool traces, full recipient lists, or arbitrary model-authored HTML. All output rendering must use the existing safe Markdown renderer. Tenant and audience authorization is backend-owned; a hidden or disabled UI control is not a security boundary.

## 5. Accessibility and responsive acceptance checklist

- [ ] The trigger dialog has a title, labelled search, keyboard source/event navigation, visible focus, and focus restoration on close.
- [ ] Source and event choices use semantic buttons/radio states and expose unavailable reasons without relying on color.
- [ ] Every control has a stable accessible label; nested filter groups expose their match mode and removal action.
- [ ] Minimum touch target is 44×44 CSS pixels on mobile.
- [ ] Mobile uses source → event drill-in; desktop uses two columns; the primary action remains reachable without covering the final field.
- [ ] Loading, empty, unavailable, conflict, and retry states are announced with status/alert semantics.
- [ ] Delivery status is conveyed by text, not color alone.

## 6. Verification commands

Run from `D:\CopilotGTM\copilot-app-frontend` after implementation:

```powershell
yarn test:jest __tests__/unit/modules/agents-v1/agent-detail-page.test.tsx __tests__/unit/modules/agents-v1/agents-v1-trigger-contract.test.ts __tests__/unit/modules/agents-v1/agents-v1.api.test.ts __tests__/unit/modules/agents-v1/agents-v1.schemas.test.ts __tests__/unit/modules/agents-v1/trigger-builder-sheet.test.tsx __tests__/unit/modules/agents-v1/use-agent-automation.test.tsx --runInBand
yarn check-types
yarn eslint src/modules/agents-v1/api/agents-v1.api.ts src/modules/agents-v1/components/agent-detail-page.tsx src/modules/agents-v1/components/trigger-builder-sheet.tsx src/modules/agents-v1/hooks/use-agent-automation.ts src/modules/agents-v1/types/agents-v1.schemas.ts src/modules/agents-v1/types/agents-v1.types.ts
yarn prettier src/modules/agents-v1/api/agents-v1.api.ts src/modules/agents-v1/components/agent-detail-page.tsx src/modules/agents-v1/components/trigger-builder-sheet.tsx src/modules/agents-v1/hooks/use-agent-automation.ts src/modules/agents-v1/types/agents-v1.schemas.ts src/modules/agents-v1/types/agents-v1.types.ts --check
yarn build
```

Run from `D:\CopilotGTM\copilot-backend` for contract evidence:

```powershell
.venv\Scripts\python.exe -m pytest tests/service/automation/triggers tests/service/automation/runs tests/api/routers/automation/test_agent_runs_handler.py tests/test_agent_automation_schema.py -q --disable-warnings
.venv\Scripts\python.exe -m ruff check src/service/automation/triggers src/service/automation/runs src/api/routers/automation/runs_handler.py src/api/schemas/automation_trigger_models.py src/api/schemas/agent_run_models.py
```

The source/test gates do not prove migration application, running workers, provider configuration, deployment, or a live email. Those must be reported separately.

## 7. Completion criteria

The frontend plan is complete only when every requirement below has evidence in source and tests:

1. Catalog-driven source/event selection has no duplicated executable trigger-key list.
2. Personal and organisation authority/audience behavior matches the backend eligibility and tenant policy.
3. Output delivery exposes only run-history or platform-email decisions and never a connected-mailbox output sender.
4. Saved-trigger cards render backend presentation, health, status, and concurrency behavior.
5. Run history reads persisted output through the existing detail route and separates run status from delivery status.
6. Every retryable fan-out target has an explicit independent retry action.
7. Stale API documentation no longer contradicts the current `delivery` and run-delivery contracts.
8. The focused frontend tests, type check, lint, formatting, build, backend contract tests, and diff checks pass.

Implementation completed against this plan on 2026-08-08. Keep this document as the frontend handoff and acceptance checklist; do not create a second trigger contract.
