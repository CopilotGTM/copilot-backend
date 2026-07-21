# Agent Templates API

Base path: `/users/v1/agent-templates`

This API exposes platform base agents stored in backend metadata. Templates are not executable and
cannot own triggers or runs. `Use Template` copies one exact template version into a normal private
user agent; that copied agent uses the existing agent, trigger, worker, and run APIs.

Templates are an optional starting point. Users can instead create a private agent directly through
`POST /users/v1/agents`. V1 does not expose user or admin template-creation APIs.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/users/v1/agent-templates` | List current template cards |
| `GET` | `/users/v1/agent-templates/{template_id}` | Get the current complete template |
| `POST` | `/users/v1/agent-templates/{template_id}/use` | Create a user-owned agent from an exact version |

All routes require the normal authenticated user context and user rate limits.

## List Templates

```http
GET /users/v1/agent-templates
```

The list is read directly from code metadata and performs no database write. The response contains
stable identity/version, display fields, capability keys, output mode, and publication time.

## Get Template

```http
GET /users/v1/agent-templates/6e2f986d-3db4-4bf8-8e75-8fa596aa0101
```

The detail adds the prompt, model configuration, turn limit, and input/output schemas. Unknown IDs
return `404 Agent template not found`.

## Use Template

```http
POST /users/v1/agent-templates/6e2f986d-3db4-4bf8-8e75-8fa596aa0101/use
Content-Type: application/json
```

```json
{
  "version": 1
}
```

Success returns `201 Created`, `Location: /users/v1/agents/{agent_id}`, and the normal complete
`AgentDetail`. The new row has authenticated organisation/owner identity, `source_type=system`, the
template UUID in `source_agent_id`, `source_version=1`, `visibility=private`, `status=active`, and
`revision=1`.

The version is mandatory. If the ID/version pair is not available, the API returns `404` instead of
copying a different version.

## Current Template

`Deal Stage Health Review` version 1 is a read-only sales template designed for the existing CRM
deal-stage trigger. It uses `deal-health-check` plus read-only deal evidence tools and emits JSON
containing overall health, confidence, risks, and prioritized recommended actions.

## Future Administration

There are no public/admin mutation APIs in V1. A future admin UI can move this metadata into
template/version tables while preserving stable IDs, versions, these user routes, and the existing
user-agent materialisation flow.
