# Agent Runs API V1

Base path: `/users/v1/agent-runs`.

An `AgentRun` is the durable execution record and immutable audit snapshot created after a trigger
event is accepted. Reads are organisation- and audience-scoped; knowing a run ID never grants
access to it.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/users/v1/agent-runs` | Cursor-page visible runs |
| `GET` | `/users/v1/agent-runs/{run_id}` | Get snapshots, result, usage, causation, errors, and timing |

## Audience Rules

A run is visible to the authenticated user only when one of these rules holds:

- it executed with a `user` principal matching the caller and audience `principal`; or
- it executed with an `organisation_service` principal, uses `explicit_users` or
  `subject_participants`, and its resolved audience contains the caller.

Organisation-service runs with audience `none` are intentionally absent from this user API.
Foreign and non-visible run IDs both return `404 Agent run not found`.

## List Runs

```http
GET /users/v1/agent-runs?agent_id={agent_id}&trigger_id={trigger_id}&status=succeeded&limit=20
```

All filters are optional. `status` accepts `pending`, `running`, `succeeded`, `failed`,
`delivery_failed`, `validation_failed`, or `cancelled`. `limit` defaults to 20 and is capped at
100.

Pagination is cursor-based. Pass `data.next_cursor` unchanged as `cursor` on the next request. A
null cursor means the page is terminal.

Each `data.items[]` summary contains:

```ts
type AgentRunSummary = {
  run_id: string;
  agent_id: string;
  trigger_id: string;
  agent_revision: number;
  status:
    | "pending"
    | "running"
    | "succeeded"
    | "failed"
    | "delivery_failed"
    | "validation_failed"
    | "cancelled";
  event_id: string;
  event_key: string;
  correlation_id: string;
  execution_principal_type: "user" | "organisation_service";
  audience_mode: "principal" | "none" | "explicit_users" | "subject_participants";
  subject_type: string | null;
  subject_id: string | null;
  attempt_count: number;
  model_name: string | null;
  latency_ms: number | null;
  error_code: string | null;
  error_message: string | null;
  output_preview: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
};
```

`output_preview` contains at most the first 300 characters of text output. Full output and
snapshots are deliberately excluded from the list page.

## Get Run

```http
GET /users/v1/agent-runs/{run_id}
```

The detail includes every summary field plus:

- creator, execution-principal, audience, source, actor, subject, and correlation metadata;
- pinned agent and trigger snapshots plus the canonical input event snapshot;
- text or JSON output;
- attempt/claim state, model, token usage, cost, and latency;
- stable error code and sanitized error message;
- `scheduled_for`, `caused_by_run_id`, and `causation_depth`.

Treat snapshots as audit data, not as current editable definitions.

## Lifecycle And Frontend Polling

```text
pending -> running -> succeeded
                   -> validation_failed
                   -> delivery_failed
                   -> failed
                   -> cancelled

running -> pending  (retryable failure or expired claim)
```

PostgreSQL claim state is authoritative. A frontend that wants live state should poll the list or
detail endpoint while status is `pending` or `running` and stop at any other status. V1 does not
provide a run-history push or stream endpoint.

Manual invocation and webhook acceptance return event IDs, not run IDs. Poll the trigger-filtered
run list and correlate with `event_id` or `correlation_id` after activation creates the run.
