# Agent Definitions API

Base path: `/users/v1/agents`

This API manages reusable private agent definitions owned by the authenticated user. It is
separate from `/users/agents`, the legacy reminder-backed scheduled-agent surface. Creating an
agent definition does not create a trigger, schedule, or run.

Platform base agents are exposed separately under `/users/v1/agent-templates`. Using one copies its
complete metadata into this user-owned resource; it does not execute the template directly.
Templates are optional: clients may create a private agent directly with `POST /users/v1/agents`
without first creating, selecting, or publishing a template.

## Security And Ownership

The backend derives `organisation_id` and `owner_user_id` from authentication. Clients cannot
supply or change tenant, owner, provenance, visibility, runtime type, revision, or snapshot fields.

Every read and write filters by both authenticated organisation and owner. A foreign, archived, or
unknown `agent_id` returns the same `404 Agent not found` response.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/users/v1/agents` | Create a private agent definition |
| `GET` | `/users/v1/agents` | List the current user's active/paused agents |
| `GET` | `/users/v1/agents/{agent_id}` | Get one complete owned definition |
| `PUT` | `/users/v1/agents/{agent_id}` | Fully replace mutable fields |
| `PUT` | `/users/v1/agents/{agent_id}/capabilities` | Atomically replace selected tools and skills |
| `PUT` | `/users/v1/agents/{agent_id}/output-delivery` | Replace external output destinations without changing executable revision |
| `DELETE` | `/users/v1/agents/{agent_id}?expected_revision=1` | Archive without hard deletion |
| `GET` | `/users/v1/agent-capability-catalog` | List code-owned tool, skill, and output-delivery choices |
| `GET` | `/users/v1/agent-templates` | List code-owned base agents |
| `POST` | `/users/v1/agent-templates/{template_id}/use` | Copy an exact template version into the user's agents |

## Create Agent

```http
POST /users/v1/agents
Content-Type: application/json
```

```json
{
  "name": "Pipeline Coach",
  "description": "Reviews my open deals and highlights risk.",
  "prompt": "Rank my open deals by risk and recommend the next action.",
  "model_name": "gpt-5-mini",
  "agent_model_config": {
    "temperature": 0.2
  },
  "allowed_tools": ["search_deals", "get_deal_details"],
  "allowed_skills": ["pipeline-review"],
  "max_turns": 6,
  "input_schema": {
    "type": "object"
  },
  "output_mode": "json",
  "output_schema": {
    "type": "object",
    "required": ["summary"],
    "properties": {
      "summary": {"type": "string"}
    }
  }
}
```

Success returns `201 Created`, a `Location: /users/v1/agents/{agent_id}` header, and the complete
definition in the normal `{status, message, data}` envelope. New rows use `revision = 1`,
`source_type = user`, `visibility = private`, `status = active`, `agent_type = general`, and
`output_delivery.channels = []`.

Agents copied through `Use Template` instead use `source_type = system`, store the stable template
UUID in `source_agent_id`, and store the selected metadata version in `source_version`. After the
copy, they follow the same edit, trigger, execution, and archive behavior as user-authored agents.

## List Agents

```http
GET /users/v1/agents?status=active&limit=20&offset=0
```

`status` is optional and accepts `active` or `paused`. Archived agents are not exposed from the user
list. `limit` defaults to 20 and is capped at 100; `offset` defaults to 0.

```json
{
  "status": "SUCCESS",
  "message": "Agents retrieved successfully",
  "data": {
    "items": [
      {
        "agent_id": "5ee919d2-a2b0-4eb8-bf43-3d38f56df418",
        "revision": 1,
        "source_type": "user",
        "status": "active",
        "name": "Pipeline Coach",
        "description": "Reviews my open deals and highlights risk.",
        "agent_type": "general",
        "model_name": "gpt-5-mini",
        "allowed_tools": ["search_deals", "get_deal_details"],
        "allowed_skills": ["pipeline-review"],
        "output_mode": "json",
        "created_at": "2026-07-13T10:00:00Z",
        "updated_at": "2026-07-13T10:00:00Z"
      }
    ],
    "total_count": 1,
    "limit": 20,
    "offset": 0
  }
}
```

## Get Agent

```http
GET /users/v1/agents/{agent_id}
```

The detail response adds owner/provenance fields, visibility, prompt, model configuration, turn
limit, input/output schemas, and the separately versioned delivery decision to the list
representation:

```json
{
  "output_delivery": {
    "channels": ["email_notification"],
    "updated_at": "2026-08-09T10:30:00Z"
  }
}
```

## Replace Agent

```http
PUT /users/v1/agents/{agent_id}
Content-Type: application/json
```

PUT is a complete replacement of the mutable definition. Send all create fields plus:

```json
{
  "expected_revision": 1,
  "status": "paused"
}
```

The complete payload is validated before one conditional update. A successful replacement
increments `revision`. If another edit already changed the row, the API returns `409` with
`Agent changed since it was loaded; refresh and retry`.

`status` accepts `active` or `paused`. Archive is intentionally unavailable through PUT.
The complete definition PUT does not accept or overwrite `output_delivery`.

## Replace Capabilities

```http
PUT /users/v1/agents/{agent_id}/capabilities
Content-Type: application/json
```

```json
{
  "expected_revision": 2,
  "allowed_tools": ["search_deals", "get_deal_details"],
  "allowed_skills": ["pipeline-review"]
}
```

This endpoint replaces the complete desired capability set and increments the agent revision once.
It is idempotent at the representation level and uses the same optimistic-concurrency response as
the full replacement endpoint. Existing triggers remain pinned to their saved agent snapshot until
the user explicitly refreshes the trigger target.

`GET /users/v1/agent-capability-catalog` returns the backend's current tool and skill keys with
labels and categories plus `output_delivery_channels`. V1 selection does not upload or install
executable code.

## Replace Output Delivery

Output delivery is one agent-level decision, independent of its triggers and executable revision:

```http
PUT /users/v1/agents/{agent_id}/output-delivery
Content-Type: application/json
```

```json
{
  "channels": ["email_notification"],
  "expected_updated_at": "2026-08-09T10:30:00Z"
}
```

`channels: []` means run history only. `email_notification` is currently the only external
channel and sends through the CopilotGTM platform sender, never the user's connected mailbox.
Recipients are server-derived from each run's authorized execution principal or immutable
audience; the request cannot include addresses, mailboxes, sender settings, templates, or recipient
IDs.

The write atomically replaces the complete channel set and returns the new `output_delivery`
state. It changes `output_delivery.updated_at` but does not increment `revision`, refresh trigger
snapshots, or start provider work. A stale timestamp returns `409` with `Agent output delivery
changed since it was loaded; refresh and retry`. Changes apply only to runs created after the
write; each run keeps its activation-time snapshot.

## Archive Agent

```http
DELETE /users/v1/agents/{agent_id}?expected_revision=2
```

Success returns `204 No Content`. The row is updated to `archived` and its revision is incremented;
no SQL DELETE occurs. This preserves future trigger and run-history references. A stale revision
returns `409`; an already archived, foreign, or unknown id returns `404`.

## Validation

- tool names must exist in `src.agents.tools.registry.ToolName`
- skill names must identify a backend skill folder containing `SKILL.md`
- duplicate or blank tool/skill names are rejected
- input and output schemas must be valid JSON Schema Draft 2020-12 documents
- `output_mode = json` requires `output_schema`
- model configuration cannot contain credential-bearing keys, including provider-prefixed names
  such as `openai_api_key`
- prompt, text, capability arrays, model configuration, and schema documents are size-limited

Stored capability names are allowlists, not authorization grants. Runtime tool checks, tenant
checks, integration access, and approval policies still apply during execution.

## Errors

| Status | Meaning |
| --- | --- |
| `400` | Domain validation or unsupported archived-list request |
| `404` | Unknown, foreign-owned, or archived definition |
| `409` | Stale expected revision |
| `422` | Malformed request shape or field bounds |
| `429` | User rate limit exceeded |
| `500` | Sanitized unexpected server failure |
