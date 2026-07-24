# Agent Runs API V1

Base path: `/users/v1/agent-runs`

An `AgentRun` is both the durable Celery job record and the execution audit. The authenticated user
can only inspect runs whose `organisation_id` and `owner_user_id` match their identity.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/users/v1/agent-runs` | Cursor-page owned runs |
| `GET` | `/users/v1/agent-runs/{run_id}` | Get snapshots, result, usage, errors, and timing |

## List Runs

```http
GET /users/v1/agent-runs?agent_id={agent_id}&status=succeeded&limit=20
```

Optional filters are `agent_id`, `trigger_id`, and `status`. `limit` defaults to 20 and is capped at
100. Pass `next_cursor` from one response as `cursor` on the next request.

List items contain lifecycle fields, attempt count, model, latency, safe error details, and at most
the first 300 characters of text output. Full output and snapshots are deliberately excluded from
the page.

## Get Run

```http
GET /users/v1/agent-runs/{run_id}
```

The detail response includes:

- pinned agent, trigger, and input snapshots;
- output text or JSON;
- status, attempt count, claim identity, and lifecycle timestamps;
- effective model, token usage, cost, and latency;
- stable error code and sanitized error message.

A foreign or unknown run ID returns the same `404 Agent run not found` response.

## Lifecycle

```text
pending -> running -> succeeded
                   -> validation_failed
                   -> failed

running -> pending (retryable failure)
```

Celery messages contain only organization and run IDs. PostgreSQL claim state is authoritative.
Each claim increments `attempt_count`, and terminal writes must match both that attempt and the
claiming worker.
