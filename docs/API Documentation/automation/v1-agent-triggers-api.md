# Agent Triggers API V1

This is the frontend implementation contract for configuring and operating registry-backed agent
triggers. Authenticated routes use `/users/v1`; signed webhook delivery uses the public
`/webhooks/v1/automation` path.

The backend owns trigger availability, validation, defaults, event matching, execution authority,
target snapshots, schedules, webhook credentials, and runtime health. The frontend owns how those
backend definitions are presented.

Use the
[Agent Trigger Frontend Implementation Plan](../../superpowers/plans/2026-08-08-agent-trigger-frontend-implementation.md)
for the complete UI flow and verification checklist.

## Endpoint Map

| Method | Path | Success | Frontend use |
| --- | --- | --- | --- |
| `GET` | `/users/v1/automation-trigger-catalog` | `200` | Build the trigger picker and forms |
| `POST` | `/users/v1/automation-trigger-options/search` | `200` | Search active business values in catalog context |
| `POST` | `/users/v1/automation-trigger-options/resolve` | `200` | Batch-resolve saved values to labels and status |
| `GET` | `/users/v1/agents/{agent_id}/triggers` | `200` | List one agent's non-archived triggers |
| `POST` | `/users/v1/agents/{agent_id}/triggers` | `201` | Create and pin a trigger |
| `GET` | `/users/v1/agents/{agent_id}/triggers/{trigger_id}` | `200` | Read the complete saved definition |
| `PUT` | `/users/v1/agents/{agent_id}/triggers/{trigger_id}` | `200` | Fully replace user decisions |
| `POST` | `/users/v1/agents/{agent_id}/triggers/{trigger_id}/refresh-target` | `200` | Repin to the current agent revision |
| `POST` | `/users/v1/agents/{agent_id}/triggers/{trigger_id}/invoke` | `200` | Invoke a manual trigger |
| `POST` | `/users/v1/agents/{agent_id}/triggers/{trigger_id}/rotate-webhook-secret` | `200` | Rotate a webhook signing secret |
| `DELETE` | `/users/v1/agents/{agent_id}/triggers/{trigger_id}` | `204` | Archive a trigger |
| `POST` | `/webhooks/v1/automation/{endpoint_token}` | `202` | Deliver a signed external webhook |

All routes except webhook delivery require the normal authenticated user context. Organisation,
creator, target, principal, and snapshot identities are derived by the backend and must not come
from frontend form state.

## Build The UI From The Catalog

Fetch the catalog when the trigger builder opens:

```http
GET /users/v1/automation-trigger-catalog
```

The response hierarchy is:

```text
data
├── categories[]
│   ├── category_key
│   ├── label
│   └── resources[]
│       ├── resource_key
│       ├── label
│       └── triggers[]
├── delivery_channels[]
├── option_sets
│   ├── pipelines[] -> stages[]
│   └── enums
└── limits
```

`categories[].resources[].triggers[]` contains only definitions whose event producers are
available. Do not maintain a second frontend list of supported trigger keys.

The useful TypeScript projection is:

```ts
type TriggerFamily = "event" | "manual" | "schedule" | "webhook";
type PrincipalMode = "personal" | "organisation";
type ScopeType = "owned_or_created_by_principal" | "organisation";
type AudienceMode =
  | "principal"
  | "none"
  | "explicit_users"
  | "subject_participants";

type TriggerCatalogDefinition = {
  trigger_key: string;
  family: TriggerFamily;
  event_label: string;
  description: string;
  match_event_keys: string[];
  subject_type: string | null;
  subject_required: boolean;
  allow_user_watch: boolean;
  watch_fields: Array<{ path: string; label: string }>;
  default_config: Record<string, unknown>;
  config_fields: Array<{
    key: string;
    label: string;
    value_type: string;
    input_type: string;
    required: boolean;
    default: unknown;
    options: string[];
    option_source: string | null;
    depends_on: string | null;
  }>;
  fields: Array<{
    path: string;
    label: string;
    description: string;
    value_type: string;
    operators: string[];
    input_type:
      | "text"
      | "email"
      | "number"
      | "date"
      | "datetime"
      | "boolean"
      | "single_select"
      | "multi_select"
      | "entity_single_select"
      | "entity_multi_select"
      | "email_tokens"
      | "between";
    option_source: string | null;
    visibility: "standard" | "hidden";
    group: "current" | "previous" | "new" | "context";
    principal_modes: PrincipalMode[];
    event_keys: string[];
    required: boolean;
    exact: boolean;
  }>;
  principal_modes: PrincipalMode[];
  scope_types: ScopeType[];
  audience_modes: AudienceMode[];
  output_schema: Record<string, unknown> | null;
  scope_presentation: {
    personal: string | null;
    organisation: string | null;
  };
  rule_presentation: {
    summary: string;
    details: string[];
  } | null;
  timing_note: string;
  source_selector: {
    label: string;
    all_label_personal: string;
    all_label_organisation: string;
    selected_label: string;
    option_source: string;
    filter_path: string;
    max_selected: number;
  } | null;
  filter_empty_reason: string | null;
};

type TriggerDeliveryChannel = {
  key: "none" | "email_notification";
  label: string;
  description: string;
  principal_modes: PrincipalMode[];
  sender_mode: "platform" | null;
  recipient_mode_by_principal: Partial<
    Record<PrincipalMode, "execution_principal" | "run_audience">
  >;
  requires_audience_modes: Array<"explicit_users" | "subject_participants">;
  body_source: "agent_output_markdown" | null;
};

type FilterNode =
  | { all: FilterNode[] }
  | { any: FilterNode[] }
  | { path: string; operator: string; value?: unknown };
```

Render a definition as follows:

1. Use `trigger_key` as the stable identity and labels only for display.
2. Initialize `config` from `default_config`, then render every `config_fields` entry using its
   `input_type`, `required`, `options`, `option_source`, and `depends_on` metadata.
3. Render optional event predicates from `fields`. Hide `visibility = hidden`, show a field only
   for the selected `principal_mode`, and render the value using `input_type` plus
   `option_source`. Send only the field's advertised `operators`.
4. Display `rule_presentation` as non-removable preset behavior. The compiled fixed matcher is a
   backend implementation detail and must not be copied into the user `watch` or `filter`.
5. Show a watch editor only when `allow_user_watch` is true. Its paths must come from
   `watch_fields`, and its labels must come from the same entries.
6. Offer only the listed principal, scope, and audience values, subject to the authority pairing
   below.
7. Use `scope_presentation` for the Run as me/organisation explanation and `timing_note` for when
   the event becomes eligible. When no standard fields remain, show `filter_empty_reason` instead
   of an empty condition editor.

Filters support at most two group levels and 20 leaf conditions. `is_empty` and `not_empty` omit
`value`; every other operator requires it. Treat paths as opaque backend keys rather than parsing
them in the client.

### Source selectors and option labels

When `source_selector` is present, initialize `config.selection` from the catalog default and
render the supplied personal or organisation **All** label plus **Specific** choice. Selected
values are stable IDs, but the UI must display only labels returned by the option API:

```json
{"selection": {"mode": "all", "ids": []}}
```

or:

```json
{"selection": {"mode": "selected", "ids": ["stable-id"]}}
```

Omitting `selection` also means **All**. Do not construct the hidden matcher in the frontend; the
backend compiles selected values onto `source_selector.filter_path`.

Search with catalog context, never with a client-chosen option source:

```http
POST /users/v1/automation-trigger-options/search
Content-Type: application/json
```

```json
{
  "trigger_key": "email.message.received",
  "control": "source_selector",
  "field_path": null,
  "principal_mode": "personal",
  "query": "sales",
  "limit": 25,
  "cursor": null
}
```

The same endpoint supports four backend-derived control contexts:

| `control` | `field_path` | Use |
| --- | --- | --- |
| `source_selector` | `null` | A definition's optional All/specific source selector |
| `filter` | Catalog field path | A typed optional condition |
| `audience` | `null` | Active workspace members for an explicit organisation audience |
| `direct_subject` | `deal`, `contact`, or `account` | Manual invocation and Schedule record fan-out |

The backend derives both the option source and its tenant/authority policy. It rejects unsupported
control/definition combinations. Search returns active selectable items and an opaque
`next_cursor`; the limit is at most 50.

Resolve selected values in batches before rendering edit forms or saved-trigger summaries:

```http
POST /users/v1/automation-trigger-options/resolve
Content-Type: application/json
```

```json
{
  "requests": [{
    "trigger_key": "email.message.received",
    "trigger_id": "saved-trigger-id",
    "control": "source_selector",
    "field_path": null,
    "principal_mode": "personal",
    "values": ["stable-mailbox-id"]
  }]
}
```

Each result preserves its request index and returns values with `label`, `status` (`active`,
`inactive`, `deleted`, or `unavailable`), and `selectable`. A foreign or inaccessible value is
`unavailable` with `label=null`. An inactive/deleted label is disclosed only through an accessible
saved binding that already contains that value. Render a warning and block save until such a value
is removed or replaced. Never fall back to displaying the stable ID.

Resolution accepts at most 20 groups, 100 values per group, and 200 values total. The backend
batches equivalent source/context reads rather than querying once per chip.

Inline catalog values remain available for these bounded choices:

| Catalog metadata | Frontend option source |
| --- | --- |
| `config_fields[].options` | Use the literal stable values |
| `option_source = pipelines` | `data.option_sets.pipelines` |
| `option_source = pipeline_stages` | Flatten or group `data.option_sets.pipelines[].stages` |
| `option_source = deal_statuses` | `data.option_sets.enums.deal_statuses` |
| `option_source = custom_field:{field_id}` | Matching `data.option_sets.enums` entry |
| Other advertised `option_source` values | Use the automation option search/resolve endpoints above |

`explicit_users` expects active, approved application-user IDs, not CRM-user IDs. Search them with
`control="audience"`; the backend permits this only for an organisation-authority trigger whose
definition supports explicit audiences. Manual and Schedule record chips use
`control="direct_subject"`, with the record type in `field_path`, so edit-time hydration follows the
same saved-binding tombstone rules as source and filter controls.

The catalog and write API expose one development contract. Do not send catalog schema query or
request fields, and do not maintain a second frontend shape. The catalog currently returns 47
producer-backed definitions; absence means unavailable, so the frontend must not add a fallback
entry.

List responses include a backend-built `presentation` object on every trigger summary:

```ts
type AutomationTriggerPresentation = {
  breadcrumb: string;
  authority_label: string;
  scope_summary: string;
  built_in_rule: string | null;
  source_summary: string | null;
  delivery_mode: "none" | "email_notification";
  delivery_summary: string;
  delivery_status: "active" | "needs_attention";
  delivery_reason: string | null;
  conditions: Array<{
    field_label: string;
    group: "current" | "previous" | "new" | "context";
    operator: string;
    value_labels: string[];
    status: "active" | "needs_attention";
  }>;
  warnings: string[];
};
```

The list service resolves labels in batches across the page. Render this object directly; do not
derive a fallback breadcrumb from `trigger_key` and never display an unavailable stable ID.

## Create And Replace Contract

Both create and replace use the same decision fields. `PUT` additionally requires the last-read
`expected_updated_at` and is a complete replacement.

```ts
type TriggerWrite = {
  name: string;
  trigger_key: string;
  status: "active" | "paused";
  config: Record<string, unknown>;
  watch: { mode: "any" | "all"; paths: string[] } | null;
  filter: FilterNode | null;
  principal_mode: PrincipalMode;
  scope: { type: ScopeType; config: Record<string, unknown> };
  audience: { mode: AudienceMode; user_ids: string[] };
  task: { goal?: string };
  delivery: { mode: "none" } | { mode: "email_notification" };
};
```

Current trigger scope configuration is always `{}`. `task.goal` is optional and may contain at
most 4,000 characters. JSON configuration is size-bounded and credential-like keys are rejected.
Archive is not a writable status; use `DELETE`. Hidden or unrecognized filter paths are rejected
on both create and replace.

Example personal event trigger:

```http
POST /users/v1/agents/{agent_id}/triggers
Content-Type: application/json
```

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

The backend derives `family`, `match_event_keys`, the fixed stage watch, execution-principal IDs,
target identity and snapshot, schema version, canonical hash, schedule projection, and health
counters.

### Output delivery

`delivery` contains one user decision only:

- `{ "mode": "none" }` keeps the result in run history;
- `{ "mode": "email_notification" }` sends the successful run's complete Markdown output and
  run context through the CopilotGTM transactional sender.

The frontend must render choices from `data.delivery_channels`. It must not submit a mailbox,
sender integration, From/To/Cc/Bcc address, subject, body, template, provider credential, or
recipient ID inside `delivery`. For personal authority, the backend derives the execution
principal's current verified workspace email. For organisation authority, it derives recipients
from the immutable run audience, and email delivery therefore requires `explicit_users` or
`subject_participants`.

This platform notification is separate from an agent instruction that tells the agent to email a
client. Client email uses the existing connected Gmail/Outlook tool under tool policy; post-run
output notification never uses the user's connected mailbox.

### Authority pairing

| Principal mode | Required scope | Allowed audience | Additional rule |
| --- | --- | --- | --- |
| `personal` | `owned_or_created_by_principal` | `principal` | Executes as the authenticated user |
| `organisation` | `organisation` | `none`, `explicit_users`, or catalog-advertised `subject_participants` | Create/manage requires an organisation admin |

Automation bindings currently reject agents with mutation-capable tools because a retried run
cannot yet give each tool action a deterministic idempotency key. Organisation-service bindings
additionally reject all tools and skills. Disable trigger creation and target refresh for those
agent capability sets, and show the backend validation message if the agent changes concurrently.
An organisation run with audience `none` is intentionally absent from the user run-history API. For
`subject_participants`, the backend resolves visible participant user IDs from the event snapshot.

## Manual Triggers

Create `system.manual.invoked` using the normal write contract. Saving it does not run the agent.
Invoke the active trigger separately:

```http
POST /users/v1/agents/{agent_id}/triggers/{trigger_id}/invoke
Content-Type: application/json
```

```json
{
  "invocation_id": "ui:019f8c35-f31b-7fe9-a76b-06ef7ba6d9a1",
  "subjects": [
    {"type": "deal", "id": "11111111-1111-1111-1111-111111111111"}
  ]
}
```

`subjects` may contain up to 100 unique deal, contact, or account identities. An empty array creates
one global run. The backend validates every subject inside the saved principal scope and commits
the fan-out atomically. Personal bindings can be invoked only by their owner; any current
organisation admin can invoke an organisation-service binding, including one created by another
admin.

Generate `invocation_id` once per deliberate UI action. Reuse the same value and subject set when
retrying an ambiguous network result; this is the idempotency boundary.

```json
{
  "status": "SUCCESS",
  "data": {
    "invocation_id": "ui:019f8c35-f31b-7fe9-a76b-06ef7ba6d9a1",
    "correlation_id": "manual:{trigger_id}:{invocation_id}",
    "items": [
      {
        "event_id": "22222222-2222-2222-2222-222222222222",
        "subject": {"type": "deal", "id": "11111111-1111-1111-1111-111111111111"}
      }
    ]
  }
}
```

The returned event IDs represent accepted durable events, not completed agent runs.

## Scheduled Triggers

`time.schedule.due` is operational. The due dispatcher creates direct events and advances each
schedule atomically.

```json
{
  "name": "Friday pipeline review",
  "trigger_key": "time.schedule.due",
  "status": "active",
  "config": {
    "timezone": "Asia/Kolkata",
    "starts_at": "2026-08-07T09:00:00+05:30",
    "recurrence": "weekly",
    "subject_type": "deal",
    "subject_ids": ["11111111-1111-1111-1111-111111111111"]
  },
  "watch": null,
  "filter": null,
  "principal_mode": "personal",
  "scope": {"type": "owned_or_created_by_principal", "config": {}},
  "audience": {"mode": "principal", "user_ids": []},
  "task": {"goal": "Rank these deals by risk."},
  "delivery": {"mode": "none"}
}
```

Supported recurrence values are `none`, `daily`, `weekdays`, `weekly`, `biweekly`, `monthly`, and
`custom`. `custom` requires `interval_days` from 1 through 365; other values forbid it.
`starts_at` must include an offset and `timezone` must be an IANA name. A one-time start must be in
the future. `subject_type` and `subject_ids` must be provided together, with at most 100 subjects.

Responses expose the backend-owned UTC `next_trigger_at`. After a one-time occurrence is queued,
the trigger remains `active` with `next_trigger_at: null` so its asynchronous direct event fanout
can still activate; render that combination as completed rather than due. An invalid or unavailable
saved subject pauses a due trigger; render `last_error_code` and the sanitized `last_error` in its
health state.

## Webhook Triggers

Create `integration.webhook.received` with a Draft 2020-12 JSON Schema in
`config.payload_schema`. External schema references are rejected; local `#` references are
allowed.

```json
{
  "name": "External risk signal",
  "trigger_key": "integration.webhook.received",
  "status": "active",
  "config": {
    "payload_schema": {
      "type": "object",
      "properties": {"risk": {"type": "string", "maxLength": 20}},
      "required": ["risk"],
      "additionalProperties": false
    }
  },
  "watch": null,
  "filter": null,
  "principal_mode": "personal",
  "scope": {"type": "owned_or_created_by_principal", "config": {}},
  "audience": {"mode": "principal", "user_ids": []},
  "task": {},
  "delivery": {"mode": "none"}
}
```

On first provisioning, `data.webhook_credentials` contains `endpoint_token` and `secret`. These
plaintext values are returned only in that response. Show a copy/download step immediately; never
put them in logs, analytics, URLs other than the endpoint token's path, or normal frontend cache.
Later trigger reads return the public payload schema but not credentials.

The sender posts the exact JSON bytes to:

```http
POST /webhooks/v1/automation/{endpoint_token}
X-CopilotGTM-Timestamp: 1785924000
X-CopilotGTM-Signature: sha256={hex_hmac}
X-CopilotGTM-Delivery-ID: provider-event-123
Content-Type: application/json
```

Compute `hex_hmac` as HMAC-SHA256 using the secret over:

```text
{timestamp}.{exact raw request body bytes}
```

The timestamp is Unix seconds and must be within five minutes. The body limit is 61,440 bytes
(60 KiB), and the payload must be a JSON object with at most 64 top-level fields. Always send a stable delivery
ID of at most 200 characters. Reusing it with a different payload returns `409`; omitting it scopes
the canonical payload hash to the signed timestamp, so exact signed retries dedupe but later
deliveries do not.

A valid request returns `202` and `data.event_id`. Processing remains asynchronous.

Rotate a secret with the trigger's last-read timestamp:

```http
POST /users/v1/agents/{agent_id}/triggers/{trigger_id}/rotate-webhook-secret
Content-Type: application/json

{"expected_updated_at": "2026-08-05T10:00:00Z"}
```

The response returns only the new `secret`; the endpoint token does not change. The previous
secret remains valid for 24 hours. Refetch the trigger after rotation to obtain its new
`updated_at` concurrency token.

Backend operators must configure the dedicated Key Vault secret
`automation-webhook-root-secret`. During a root rotation, retain old root values temporarily in
`AUTOMATION_WEBHOOK_PREVIOUS_ROOT_SECRETS` (comma-separated) until every credential derived from
them has been rotated or retired.

## Saved Trigger State And Concurrency

List responses are offset-paginated with `limit` (default 20, maximum 100), `offset`, and optional
`status=active|paused`. Archived triggers are not listable.

Use these fields in trigger rows:

- `family` and `trigger_key` for identity;
- `status`, `next_trigger_at`, `last_triggered_at`, and `trigger_count` for state;
- `consecutive_failures`, `last_error_code`, and `last_error` for health;
- `target_revision` and `target_update_available` for agent-definition drift;
- `updated_at` as the next write's concurrency token.

Editing an agent does not silently change an existing trigger's pinned `target_snapshot`. Repin only
after an explicit user action:

```http
POST /users/v1/agents/{agent_id}/triggers/{trigger_id}/refresh-target
Content-Type: application/json

{"expected_updated_at": "2026-08-05T10:00:00Z"}
```

`PUT`, refresh, rotate, and archive require the last-read timezone-aware `updated_at`. A stale value
returns `409`; refetch and let the user review the newer state rather than overwriting it.

## Runs And Polling

Accepted events create runs asynchronously. Use:

```http
GET /users/v1/agent-runs?agent_id={agent_id}&trigger_id={trigger_id}&limit=20
GET /users/v1/agent-runs/{run_id}
```

There is no trigger push/stream response. Poll while a visible run is `pending` or `running`, then
also poll while its independent delivery status is `pending` or `sending`. Stop after both are
terminal. A notification failure does not change a succeeded run into a different run status. See
[Agent Runs API](./v1-agent-runs-api.md) for delivery summaries, per-target retry, response fields,
and audience rules.

## Response And Error Handling

Except for `204`, bodies use the common envelope:

```json
{
  "status": "SUCCESS",
  "message": "Request successful",
  "code": null,
  "data": {}
}
```

| Status | Meaning | Frontend action |
| --- | --- | --- |
| `400` | Invalid trigger decision, authority combination, or configuration | Show the backend message near the relevant action |
| `401` | Invalid webhook signature | Check signing configuration; do not retry unchanged credentials forever |
| `403` | Caller cannot manage an organisation binding | Disable organisation management for non-admins |
| `404` | Missing, archived, inaccessible resource, or webhook endpoint | Remove stale local state or re-check the endpoint |
| `409` | Stale write, duplicate trigger, invalid activation state, or dedupe conflict | Refetch; never overwrite silently |
| `413` | Webhook body exceeds 61,440 bytes (60 KiB) | Reduce the sender payload |
| `422` | Malformed HTTP body, field bounds, or webhook payload-schema mismatch | Treat as a contract/validation error |
| `429` | Authenticated-user or public-webhook rate limit | Back off and avoid request loops |
| `503` | Webhook credential or verification service unavailable | Keep current state and offer a deliberate retry |
