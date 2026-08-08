# Agent Runs API V1

Base path: `/users/v1/agent-runs`.

An `AgentRun` is the durable execution record and immutable audit snapshot created after a trigger
event is accepted. Reads are organisation- and audience-scoped; knowing a run ID never grants
access to it. The frontend implementation handoff is
[Agent Trigger Frontend Implementation Plan](../../superpowers/plans/2026-08-08-agent-trigger-frontend-implementation.md).

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/users/v1/agent-runs` | Cursor-page visible runs and notification summaries |
| `GET` | `/users/v1/agent-runs/{run_id}` | Get snapshots, persisted output, delivery state, usage, causation, errors, and timing |
| `POST` | `/users/v1/agent-runs/{run_id}/delivery/retry` | Requeue one safe notification target without rerunning the agent |

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
`validation_failed`, or `cancelled`. `limit` defaults to 20 and is capped at 100.

Pagination is cursor-based. Pass `data.next_cursor` unchanged as `cursor` on the next request. A
null cursor means the page is terminal.

Each `data.items[]` summary contains:

```ts
type AgentRunStatus =
  | "pending"
  | "running"
  | "succeeded"
  | "failed"
  | "validation_failed"
  | "cancelled";

type AgentRunDeliveryPresentationStatus =
  | "pending"
  | "sending"
  | "sent"
  | "partially_sent"
  | "failed"
  | "skipped";

type AgentRunDeliveryTargetSummary = {
  delivery_id: string;
  status: AgentRunDeliveryPresentationStatus;
  error_code: string | null;
  error_message: string | null;
  can_retry: boolean;
  sent_at: string | null;
};

type AgentRunDeliverySummary = {
  channel: "email";
  status: AgentRunDeliveryPresentationStatus;
  recipient_count: number;
  sent_count: number;
  failed_count: number;
  skipped_count: number;
  can_retry: boolean;
  sent_at: string | null;
  targets: AgentRunDeliveryTargetSummary[];
};

type AgentRunSummary = {
  run_id: string;
  agent_id: string;
  trigger_id: string;
  agent_revision: number;
  status: AgentRunStatus;
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
  delivery: AgentRunDeliverySummary | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
};
```

`output_preview` contains at most the first 300 characters of text output. Full output and
snapshots are deliberately excluded from the list page. `delivery_id` identifies the target for
API actions; it is not a business label and must not be displayed as a recipient.

## Get Run

```http
GET /users/v1/agent-runs/{run_id}
```

The detail includes every summary field, including `delivery`, plus:

- creator, execution-principal, audience, source, actor, subject, and correlation metadata;
- pinned agent and trigger snapshots plus the canonical input event snapshot;
- persisted `output_text` or `output_json`;
- attempt/claim state, model, token usage, cost, and latency;
- stable error code and sanitized error message;
- `scheduled_for`, `caused_by_run_id`, and `causation_depth`.

Treat snapshots as audit data, not as current editable definitions. Render `output_text` as safe
Markdown. When it is absent, render `output_json` as formatted JSON. There is no separate
output-only endpoint.

## Independent Run And Delivery Lifecycles

```text
Agent run: pending -> running -> succeeded | validation_failed | failed | cancelled
Delivery:  pending -> sending -> sent | failed | skipped
                              -> partially_sent  (aggregate fan-out state)
```

Delivery is deliberately separate from `AgentRun.status`. A succeeded run remains succeeded when
one or more notification targets fail. Poll while the run is `pending` or `running`, or while its
delivery is `pending` or `sending`; stop after both lifecycles are terminal. V1 does not provide a
run-history push or stream endpoint.

Display aggregate recipient/sent/failed/skipped counts, then render each returned target with its
own status, sanitized error, and retry action when `can_retry` is true. Do not expose provider IDs
or a full recipient list.

## Retry One Delivery Target

```http
POST /users/v1/agent-runs/{run_id}/delivery/retry
Content-Type: application/json
```

```json
{"delivery_id": "11111111-1111-1111-1111-111111111111"}
```

A successful repair request returns `202`:

```json
{
  "status": "SUCCESS",
  "message": "Agent run delivery retry accepted",
  "data": {
    "run_id": "22222222-2222-2222-2222-222222222222",
    "status": "pending",
    "delivery_count": 1
  }
}
```

Call this route only for the selected target whose `can_retry` is true. The server permits only a
known pre-provider failure with a confirmed non-send outcome. It returns `404` when the visible run
or selected target is unavailable, and `409` when the target is unsafe to resend or changed after
the last read. Refresh the run after either response. A retry requeues only the notification; it
never activates the trigger or reruns the agent.

The pending repair is committed before task publication. If publication fails, worker recovery can
still discover the durable target.

Manual invocation and webhook acceptance return event IDs, not run IDs. Poll the trigger-filtered
run list and correlate with `event_id` or `correlation_id` after activation creates the run.
