# Agent Triggers API V1

This is the frontend implementation contract for configuring and operating registry-backed agent
triggers. Authenticated routes use `/users/v1`; signed webhook delivery uses the public
`/webhooks/v1/automation` path.

The backend owns trigger availability, validation, defaults, event matching, execution authority,
target snapshots, schedules, webhook credentials, and runtime health. The frontend owns how those
backend definitions are presented.

## Endpoint Map

| Method | Path | Success | Frontend use |
| --- | --- | --- | --- |
| `GET` | `/users/v1/automation-trigger-catalog` | `200` | Build the trigger picker and forms |
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
├── schema_version
├── categories[]
│   ├── category_key
│   ├── label
│   └── resources[]
│       ├── resource_key
│       ├── label
│       └── triggers[]
└── option_sets
    ├── pipelines[] -> stages[]
    └── enums
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
  watch_paths: string[];
  fixed_watch: { mode: "any" | "all"; paths: string[] } | null;
  fixed_filter: FilterNode | null;
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
    option_source: string | null;
    required: boolean;
    exact: boolean;
  }>;
  principal_modes: PrincipalMode[];
  scope_types: ScopeType[];
  audience_modes: AudienceMode[];
  output_schema: Record<string, unknown> | null;
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
3. Render optional event predicates from `fields`. Send only a field's advertised `operators` and
   use its `value_type` to choose the value control.
4. Display `fixed_watch` and `fixed_filter` as non-removable preset behavior. Do not copy either
   value into the user `watch` or `filter`; the backend compiles and combines them.
5. Show a watch editor only when `allow_user_watch` is true. Its paths must come from
   `watch_paths`.
6. Offer only the listed principal, scope, and audience values, subject to the authority pairing
   below.

Filters support at most two group levels and 20 leaf conditions. `is_empty` and `not_empty` omit
`value`; every other operator requires it. Treat paths as opaque backend keys rather than parsing
them in the client.

### Option-source adapters

| Catalog metadata | Frontend option source |
| --- | --- |
| `config_fields[].options` | Use the literal stable values |
| `option_source = pipelines` | `data.option_sets.pipelines` |
| `option_source = pipeline_stages` | Flatten or group `data.option_sets.pipelines[].stages` |
| `option_source = deal_statuses` | `data.option_sets.enums.deal_statuses` |
| `option_source = custom_field:{field_id}` | Matching `data.option_sets.enums` entry |
| `option_source = crm_users` | Search `GET /users/crm/users?q={query}&limit=20` |
| `input_type = record_multi_select` | Use the deal, contact, or account search route selected by `subject_type` |

Record search routes are `/users/crm/deals/search`, `/users/crm/contacts/search`, and
`/users/crm/accounts/search`. Send their stable record IDs, never labels.

`explicit_users` expects active, approved application-user IDs, not CRM-user IDs. The automation
API does not currently expose an organisation-member discovery route. Use an existing trusted
organisation-member data source in the application; if the frontend has none, do not expose that
audience option yet.

Catalog `schema_version` versions the catalog response. A saved trigger's
`config_schema_version` versions its compiled configuration. Neither is the agent
`target_revision`.

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
  delivery: { mode: "none" };
};
```

Current trigger scope configuration is always `{}`. `task.goal` is optional and may contain at
most 4,000 characters. JSON configuration is size-bounded and credential-like keys are rejected.
Archive is not a writable status; use `DELETE`.

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
  "delivery": {"mode": "none"}
}
```

The backend derives `family`, `match_event_keys`, the fixed stage watch, execution-principal IDs,
target identity and snapshot, schema version, canonical hash, schedule projection, and health
counters.

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
stop at `succeeded`, `failed`, `delivery_failed`, `validation_failed`, or `cancelled`. See
[Agent Runs API](./v1-agent-runs-api.md) for the response fields and audience rules.

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
