# Agent Trigger Picker UI Specification

**Status:** Ready for implementation review

**Date:** 2026-08-06

**Applies to:** Agent detail trigger creation, editing, invocation, and saved-trigger management

**Source contracts:** `2026-08-05-ai-native-trigger-redesign.md` and `automation/v1-agent-triggers-api.md`

## 1. Decision

Use an operation-first, progressively disclosed picker over the existing rich trigger catalog.

The user first chooses a source such as **Deal**, **Email**, or **Schedule**, then chooses a short event such as **Created**, **Updated**, or **Deleted**. Common semantic events such as **Stage changed** remain available as shortcuts under that source. The UI must not display the whole catalog in one long dropdown.

This combines the useful part of Twenty CRM's interaction model with CopilotGTM's stronger runtime architecture:

- the backend retains durable, typed events, stable trigger keys, validation, matching, authorization, and execution;
- the frontend presents those definitions as a short, navigable hierarchy;
- semantic shortcuts compile to the existing base event plus fixed watch/filter rules where possible;
- the frontend never owns a second hardcoded list of valid trigger keys.

Do not collapse the backend catalog to only `created`, `updated`, and `deleted`. That would remove useful event semantics and would couple filtering, permissions, and runtime behavior to frontend guesses.

## 2. Goals

- Make the common path understandable without exposing the complete event registry at once.
- Let a user answer, in order: **where**, **what happened**, **under which conditions**, **who runs it**, and **what should the agent do**.
- Keep every selectable and configurable behavior backed by the server catalog.
- Make incompatible choices impossible to save and explain why they are unavailable.
- Give frontend engineers exact states, validation rules, and ownership boundaries.
- Preserve stable trigger keys and existing saved-trigger compatibility.

## 3. Non-goals

- Redesigning the event envelope, outbox, matcher, scheduler, webhook verifier, or agent-run lifecycle.
- Inferring a trigger from the agent prompt.
- Creating new producer event types for every friendly UI shortcut.
- Hardcoding backend trigger keys, field paths, option lists, or authority policy in React.
- Implementing the UI as part of this specification.

## 4. Ownership boundary

### Backend owns

- trigger availability and stable identifiers;
- category, resource, event, family, and source-mode metadata;
- config fields, input types, options, defaults, dependencies, and requiredness;
- watchable fields, filter fields, operators, value types, and fixed rules;
- supported principal modes, scopes, audiences, and target types;
- agent-specific execution eligibility and machine-readable denial reasons;
- validation, persistence, activation, authorization, idempotency, and execution.

### Frontend owns

- source ordering, icons, responsive layout, search, and progressive disclosure;
- human-readable grouping and contextual help based on server metadata;
- local form state and immediate validation feedback;
- translating stable operator codes into friendly labels;
- rendering server denial reasons and validation errors;
- presenting saved-trigger state without exposing raw implementation keys.

The backend remains authoritative. Frontend checks improve usability but never replace server validation.

## 5. Information architecture

### 5.1 Level one: source

Show a short source list, not registry categories as a flat event list. Recommended order:

1. Manual
2. Schedule
3. Deal
4. Contact
5. Account
6. Email
7. Meeting
8. AI insight
9. Agent run
10. Webhook

Only show sources present in the returned catalog. Category metadata may be used for section headings, but the source is the primary choice.

### 5.2 Level two: event

Within a source, group events as follows when the catalog provides them:

- **Primary:** Created, Updated, Deleted.
- **Common updates:** Stage changed, Owner changed, Won, Lost, or other curated semantic presets.
- **More events:** combined or specialized events such as Created or updated.

For non-CRM sources, use domain-appropriate groups without inventing fake CRUD operations. Examples:

- Email: Received, Sent.
- Meeting: Transcript ready, Summary ready.
- AI insight: Risk detected, Risk resolved, Blocker detected, Blocker resolved.
- Agent run: Completed, Failed.
- Schedule: At a specific time, Recurring.
- Manual and Webhook: one primary event each unless the catalog later advertises more.

The catalog's stable `trigger_key` is the saved value. The labels above are presentation labels, not replacements for trigger keys.

### 5.3 Semantic shortcuts

A shortcut such as **Stage changed** must use a catalog definition with a fixed watch on `stage_id`; it must not be reconstructed from a frontend-maintained key map. Where the backend can express a semantic event as a base update plus fixed watch/filter rules, it should do so instead of adding another producer.

The picker renders fixed rules as read-only explanations. Users may add filters around them but cannot remove or contradict the built-in rule.

### 5.4 Search

Search covers source label, event label, description, and approved search aliases supplied by the server. Search results may flatten the hierarchy, but every result must show its breadcrumb, for example:

`CRM / Deal / Stage changed`

Unavailable matches remain visible only when they help explain why a requested option cannot be selected. They must be disabled and include the reason.

## 6. Picker interaction

### 6.1 Initial state

- Open with no trigger selected.
- Do not silently default to Manual or the first catalog entry.
- Disable **Add trigger** until the user explicitly selects an event and the complete form is valid.
- Keep search focused when the dialog is opened by keyboard.
- Do not infer a recommendation from the task goal or prompt.

Desktop layout:

```text
Add trigger
┌─────────────────────────────────────────────────────────┐
│ Search triggers...                                      │
├───────────────────────┬─────────────────────────────────┤
│ Sources               │ Deal                            │
│                       │                                 │
│ Manual                │ Primary                         │
│ Schedule              │ Created                         │
│ > Deal                │ Updated                         │
│ Contact               │ Deleted                         │
│ Account               │                                 │
│ Email                 │ Common updates                  │
│ Meeting               │ Stage changed                   │
│ AI insight            │ Owner changed                   │
│ Agent run             │                                 │
│ Webhook               │ More events                     │
│                       │ Created or updated              │
└───────────────────────┴─────────────────────────────────┘
```

Mobile layout uses two drill-in screens: source list, then event list. A visible Back control returns to sources without dismissing the sheet.

### 6.2 Selected state

After selection, replace the browser with a compact summary and the event-specific form:

```text
When
CRM / Deal / Stage changed                         Change
Runs when a deal's stage changes.

Built-in rule
Stage changes

Watch additional fields (optional)
[ Select fields ]

Only if (optional)
[ Add condition ]
```

**Change** returns to the picker while preserving compatible form values. Changing to an incompatible definition clears only values that are invalid for the new definition and tells the user what was removed.

## 7. Compatibility and agent eligibility

The general trigger catalog cannot by itself determine whether the current agent may execute a principal mode. The frontend must not identify mutating tools or service-safe skills from hardcoded IDs.

Add an agent-specific eligibility object to the agent detail contract:

```json
{
  "automation_eligibility": {
    "personal": {
      "available": true,
      "reason_code": null,
      "reason": null
    },
    "organisation": {
      "available": false,
      "reason_code": "organisation_tools_not_supported",
      "reason": "Organisation execution is unavailable while this agent has tools."
    }
  }
}
```

The UI intersects this eligibility with each catalog definition's `principal_modes`. A definition is selectable only when at least one intersection mode is available.

Supported denial codes must include:

- `retry_unsafe_tools`
- `organisation_requires_admin`
- `organisation_tools_not_supported`
- `organisation_skills_not_supported`
- `no_compatible_principal_mode`

Unknown codes use the server-provided `reason`. The API must still reject invalid combinations even if the frontend is stale or bypassed.

When no principal mode is available:

- show the definition disabled;
- show the precise reason near **Run as**;
- keep **Add trigger** disabled;
- never render an empty authority section that looks valid.

## 8. Configuration forms

Render configuration from the selected definition's `config_fields` in server order.

- Respect `input_type`, `required`, `default`, `options`, `option_source`, and `depends_on`.
- Do not render a dependent field until its dependency is satisfied.
- Preserve falsy valid values such as `false`, `0`, and an explicitly empty optional list.
- Show required errors beside the field after blur and on submit.
- Send only fields accepted by the chosen definition.

### 8.1 Schedule

- Present one-time and recurring choices from catalog definitions.
- Use a searchable IANA timezone chooser. Do not use an unrestricted text field or a four-value suggestion list.
- Show the resolved local next-run time before save when it can be calculated safely.
- Require subject type and subject IDs together when the catalog requires a target.
- Enforce the server maximum of 100 CRM subject IDs for direct/manual-style targeting.
- Render a one-time trigger with `status=active` and `next_trigger_at=null` as **Completed**, not Active or broken.

### 8.2 Webhook

- Render the JSON Schema editor with syntax and schema-shape errors inline.
- Explain the maximum request size before creation.
- Show the generated URL/secret only in the one-time credential dialog returned after create or rotate.
- Require explicit confirmation before closing credentials that cannot be retrieved again.
- Saved-trigger actions include **Rotate secret**; rotation must not imply trigger semantics changed.

### 8.3 Manual

Creating a manual trigger saves a reusable binding; it does not run the agent.

After creation, **Run now** opens a separate invocation flow:

- global invocation when the trigger supports it; or
- target selection for supported CRM records, up to 100 IDs.

Do not place invocation fields in the create form unless they are configuration of the reusable binding.

## 9. Changes and filter editor

### 9.1 Updated events

For a generic **Updated** event, ask **Which fields changed?** before optional value filters.

- Default: Any supported field.
- Optional: one or more fields from `watch_fields`.
- A semantic shortcut shows its fixed watch as a read-only built-in rule.
- User-selected watch fields extend, but never replace, fixed watch behavior when the contract permits extension.

### 9.2 Filters

- Build paths, operators, and input controls only from catalog field metadata.
- Display friendly operator labels such as **Equals**, **Is one of**, and **Changed from**, never raw capitalization such as **Eq**.
- Group fields visually as Current value, Previous value, New value, and Event metadata when the catalog contains those concepts.
- Treat field paths as opaque identifiers in requests.
- Do not advertise previous/new values for an event that cannot supply them.
- Fixed filters are read-only and shown above user conditions.
- Allow no more than 2 nested groups and 20 leaf conditions.
- Show type-appropriate editors for strings, numbers, booleans, enums, dates, and lists.
- Normalize only according to field metadata; do not apply frontend-only matching semantics.

## 10. Authority, scope, and audience

Present authority as cards, not an unexplained dropdown.

### Run as me

Copy:

> Runs as you. Personal record triggers activate only for records you own or created. Runs appear in your history and use your permitted agent capabilities.

This ownership limitation must be visible before save. A label such as “Run when a deal stage changes” must not imply that all organisation deals activate a personal trigger.

### Run as organisation

Copy:

> Runs as the organisation service for the selected audience. Organisation admins can manage this shared trigger.

- Show only to admins when the server marks it available.
- Show audience controls only for organisation execution.
- Render only audience modes returned by the catalog.
- Do not offer explicit-user audience selection until a tenant-scoped, active-member picker exists.
- Explain tool and skill incompatibility separately using eligibility metadata.

## 11. Task goal and active state

- Task goal is required if the existing create contract requires it; otherwise initialize it from the agent's task goal and allow editing.
- Explain that the trigger chooses **when** the agent runs and the task goal chooses **what** it should do.
- The Active switch controls whether the saved binding can activate; it does not invoke the trigger.
- For one-time schedules, Completed is a derived display state and cannot be toggled back to Active without editing the schedule.

## 12. Validation and submission

Disable **Add trigger** or **Save changes** when any of these is true:

- no event is selected;
- no compatible principal mode is selected;
- a required config field is missing;
- JSON or schema input is invalid;
- watch or filter structure is invalid;
- task goal is required and empty;
- the request is already submitting.

On backend validation failure, preserve the form and attach errors to the closest field. Show non-field errors at the top of the form. On `409 Conflict`, explain that the trigger changed elsewhere and offer **Reload latest**; do not overwrite silently.

## 13. Saved-trigger presentation

Each saved trigger card or row shows:

- user-defined name, when available;
- friendly breadcrumb, for example `CRM / Deal / Stage changed`;
- status: Active, Paused, Completed, or Needs attention;
- authority/scope badge: Me or Organisation;
- next run for schedules;
- last triggered time, trigger count, and consecutive failures when populated;
- a concise fixed-rule and filter summary;
- stale-target or revision warning when returned by the API.

Do not use raw `family` or `trigger_key` as the user-facing trigger type.

Available row actions are contextual:

- Edit
- Run now, for manual triggers
- Rotate secret, for webhooks
- Refresh target, when required
- Pause/Activate, when supported
- Archive

Archiving requires confirmation and explains that historical runs remain visible.

## 14. Loading, empty, and error states

- **Catalog loading:** picker skeleton; creation controls disabled.
- **Catalog failed:** retry action and diagnostic message; do not fall back to a hardcoded catalog.
- **No catalog definitions:** “No trigger types are available for this workspace.”
- **No compatible definitions:** explain the agent capability conflict and link the user back to Tools/Skills configuration.
- **Search empty:** “No triggers match ‘…’” with Clear search.
- **No saved triggers:** explain that the agent currently runs only when started manually outside this binding flow, if applicable.
- **Saved-trigger load failed:** keep the rest of the agent page usable and provide Retry.

## 15. Accessibility and responsive behavior

- The picker, source list, event list, cards, filters, and menus must be fully keyboard operable.
- Use a dialog/sheet title and return focus to the invoking control on close.
- Announce selection changes and validation summaries to assistive technology.
- Breadcrumbs require accessible labels and separators that are not read as content.
- Disabled entries remain focusable only when needed to expose an unavailable reason.
- Do not communicate status or eligibility by color alone.
- Maintain a minimum 44-by-44-pixel touch target on mobile.
- On mobile, keep the primary action visible without covering the final form field.

## 16. Backend prerequisites

These must be resolved before considering the UI contract complete in production:

1. **Unique resource identity:** AI deal risk and deal blocker definitions currently reuse the `deal` resource key. Since the catalog groups by category and resource key, their labels collide. Give them unique semantic resource keys such as `deal_risk` and `deal_blocker`, while preserving stable trigger keys.
2. **Agent eligibility metadata:** expose the agent-specific principal eligibility object described in section 7. Passing only `agent.allowed_tools.length > 0` is insufficient because tool safety, organisation tool policy, and skills all affect eligibility.
3. **Truthful filter fields:** each definition must advertise only paths its event snapshot can provide. Immutable or absent before/after paths must not appear as selectable filters.
4. **Stable presentation metadata:** resource keys must be unique within a category, and labels/descriptions must not depend on registry insertion order.

## 17. Expected implementation boundary

The later implementation should adapt existing trigger UI rather than introduce a parallel builder.

Frontend areas expected to change:

- `src/modules/agents-v1/components/trigger-builder-sheet.tsx`
- `src/modules/agents-v1/components/agent-detail-page.tsx`
- the existing agents-v1 API types, schemas, client, hooks, and focused component tests

Backend areas expected to change:

- automation trigger registry/catalog serialization for unique resource metadata;
- agent detail serialization for `automation_eligibility`;
- focused catalog and eligibility contract tests.

The exact file-by-file implementation plan should be written only after this design is approved and the current worktree is re-inventoried.

## 18. Walking-skeleton rollout

1. Implement the source/event picker and eligibility behavior for Deal: Created, Updated, Deleted, and Stage changed.
2. Verify create, edit, activate, and saved-card rendering end to end for that slice.
3. Expand the same catalog-driven components to Contact, Account, Email, Meeting, AI insight, and Agent run.
4. Complete the specialized Manual, Schedule, and Webhook flows.

The skeleton must use the real catalog and save API. A mock-only picker does not prove the architecture.

## 19. Acceptance criteria

1. Opening Add trigger selects nothing and cannot submit.
2. The default browsing view never shows the entire event registry in one dropdown.
3. A user can reach Deal / Created, Updated, or Deleted in two choices.
4. Stage changed appears as a Deal common update and shows its fixed rule.
5. Search results always include a source/event breadcrumb.
6. No valid trigger key list is duplicated in frontend code.
7. Config controls, watch fields, filters, operators, and options come from the server catalog.
8. Generic Updated asks which fields changed; semantic presets preserve fixed watch/filter rules.
9. Personal execution visibly explains its ownership/creator boundary before save.
10. Organisation execution is unavailable when agent eligibility denies it, with a specific reason.
11. A definition with no compatible principal mode cannot be selected or submitted.
12. Tool and skill compatibility is not inferred from frontend-maintained identifiers.
13. Schedule uses an IANA timezone chooser and renders completed one-time schedules correctly.
14. Manual creation and manual invocation are separate flows.
15. Webhook credentials are handled as one-time secrets after create or rotate.
16. Invalid JSON, filters, required fields, or authority state keep submission disabled and show inline errors.
17. Saved triggers show friendly labels and status, not raw family/trigger keys.
18. AI risk and blocker appear as separate resources with stable, non-colliding labels.
19. Desktop and mobile picker paths are keyboard and screen-reader usable.
20. Existing saved trigger keys continue to load and edit without migration.

## 20. Rejected alternatives

### Keep the current flat picker

Rejected because catalog growth directly increases scan cost, unrelated domains compete in one control, and authority/config problems appear only after a difficult selection.

### Reduce the backend to CRUD events

Rejected because Email, Meeting, Schedule, Webhook, AI, and Agent run events are not meaningfully CRUD, and semantic fixed rules belong in the authoritative catalog rather than frontend reconstruction.

### Hardcode a short frontend list

Rejected because it drifts from server availability, validation, and policy. The UI may curate ordering and grouping, but every executable definition must originate from the catalog.

### Generate the trigger from the prompt

Rejected because inference is ambiguous and unsafe for an execution boundary. The user must explicitly confirm the source, event, conditions, and authority.
