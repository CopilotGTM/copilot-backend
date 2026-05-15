# CRM Governance Preferences Production Plan

## Goal

Make organisation-level CRM write preferences and messaging channel availability a backend-owned
governance contract. Admin changes the policy, backend enforces it on every write path, and
frontends receive a normalized policy snapshot during login/session bootstrap so they can hide or
disable unsupported UI.

This document is backend-first. Admin frontend changes should wait until the backend contract and
enforcement points below are implemented.

## Current Backend State

The current staged backend changes already introduce the right starting point:

- `src/service/orgnisation/organisation_preferences.py` normalizes organisation preferences into canonical
  `crm`, `channels`, and `capabilities` namespaces.
- `src/api/routers/admin/organizations_handler.py` exposes
  `PATCH /admin/organisations/{organisation_id}/app-settings`.
- `src/service/admin/admin_organisation_service.py` merges admin-controlled app settings into
  `public.organisations.preferences` while preserving unrelated preference branches.
- `src/api/routers/users/auth_handler.py` returns normalized `organisation_preferences` from
  `POST /users/login` and `GET /users/me`.
- `src/api/dependencies/crm_write_guard.py` adds reusable HTTP guards for CRM write scopes.
- Several authenticated CRM write routers now use `require_crm_write_scope(...)`.

The implementation is directionally correct, but it is not production-complete yet because
preference enforcement is not centralized across all write surfaces.

## Production Issues Found

### 1. Agent tool writes bypass the HTTP guard

The new `require_crm_write_scope(...)` dependency protects FastAPI endpoints only. Agent tools call
internal write services directly:

- `src/agents/tools/crm/create_deal.py`
- `src/agents/tools/crm/create_contact.py`
- `src/agents/tools/crm/create_account.py`
- `src/agents/tools/crm/add_stakeholder.py`
- `src/agents/tools/crm/add_deal_note.py`
- `src/agents/tools/crm/update_deal_fields.py`
- `src/agents/tools/crm/update_deal_lifecycle.py`
- `src/agents/tools/crm/update_deal_account.py`
- `src/agents/tools/tasks/create_deal_task.py`

If an organisation is switched to HubSpot or Salesforce, the HTTP API may reject internal writes,
but chat, voice, Teams, Slack, or WhatsApp tools can still mutate internal CRM data unless each tool
checks the same policy.

### 2. Bot form submissions bypass the HTTP guard

Interactive card submissions are handled outside FastAPI router dependencies:

- `src/service/bots/form_handlers/create_deal_handler.py`
- `src/service/bots/form_handlers/update_deal_stage_handler.py`
- `src/service/bots/form_handlers/add_deal_note_handler.py`
- `src/service/bots/form_handlers/add_stakeholder_handler.py`

These handlers resolve the submitting user and call services directly. They need the same write
policy checks as the original agent action that created the card. Otherwise old cards remain a
write bypass after an admin disables internal CRM writes.

### 3. Messaging channel preference is exposed but not enforced

`channels.enabled` is normalized and returned to clients, but backend message entry points do not
currently reject disabled providers. This means a disabled Slack, Teams, WhatsApp, or Google Chat
channel can still process inbound bot messages if the platform webhook is installed and reaches the
worker.

This must be enforced near bot webhook or worker ingestion, not only in the frontend. Frontend
gating improves UX, but webhooks and workers are the security boundary.

### 4. Scope taxonomy is incomplete for existing write operations

The initial scope list covers core CRM entities, but several write operations are not represented:

- deal notes
- stakeholders
- risk and blocker updates
- stakeholder insights updates
- deal asset writes if these should be governed as CRM data
- reminder or task writes if admin later wants channel-specific task controls

Before implementation, decide which of these are CRM governance writes versus separate product
features. The backend should encode that decision explicitly instead of relying on route coverage by
accident.

### 5. Admin request models allow derived fields from clients

`UpdateOrganisationAppSettingsRequest.crm` accepts the full `OrganisationCrmSettings`, including
derived fields such as `mode` and policy output fields, even though the backend derives them from
`provider`.

That is not currently dangerous because the validator re-derives values, but a production contract
should accept only writable inputs:

```json
{
  "crm": { "provider": "copilotgtm" },
  "channels": { "enabled": ["slack", "teams"] }
}
```

Derived response fields should remain output-only. The admin request should only accept the CRM
provider and enabled channels.

### 6. Preference reads are repeated per request without a shared service boundary

`crm_write_guard.py` opens a master session and reads the organisation row per guarded request.
That is acceptable as a first pass, but production enforcement should live behind one shared policy
service so HTTP guards, agent tools, form handlers, and workers use identical logic and can later add
request-local caching or Redis caching without duplicating behavior.

### 7. Tests cover normalization only

`tests/service/test_organisation_preferences.py` validates preference normalization, but the risky
parts are enforcement and contract propagation. Missing tests:

- admin app-settings update contract and audit history
- login and `/me` response shape
- HTTP write rejection for external CRM provider
- agent tool rejection for external CRM provider
- bot form rejection for external CRM provider
- inbound disabled-channel rejection
- legacy preference migration behavior

## Recommended Production Architecture

### Single policy module

Keep `src/service/orgnisation/organisation_preferences.py` as the normalization module, with the
shared policy service beside it:

- `src/service/orgnisation/organisation_policy_service.py`
  - loads normalized preferences for an organisation
  - checks CRM write scopes
  - checks messaging channel availability
  - returns a typed policy snapshot for login and admin responses

The service should expose methods similar to:

```python
async def get_policy_snapshot(organisation_id: UUID) -> OrganisationPolicySnapshot: ...
async def require_crm_write(organisation_id: UUID, scope: str) -> None: ...
async def require_channel_enabled(organisation_id: UUID, channel: str) -> None: ...
```

HTTP dependencies should call this service. Agent tools, form handlers, and workers should call the
same service directly.

### Typed contract

Introduce typed models for the normalized response instead of returning `dict[str, Any]` everywhere:

- `OrganisationCrmWritePolicy`
- `OrganisationCrmSettings`
- `OrganisationChannelSettings`
- `OrganisationCapabilities`
- `OrganisationPolicySnapshot`
- `UpdateOrganisationAppSettingsRequest`

Input model:

```python
class UpdateOrganisationCrmSettingsRequest(BaseModel):
    provider: Literal["copilotgtm", "hubspot", "salesforce"]

class UpdateOrganisationAppSettingsRequest(BaseModel):
    crm: UpdateOrganisationCrmSettingsRequest | None = None
    channels: OrganisationChannelSettings | None = None
```

Output model:

```python
class OrganisationCrmWritePolicy(BaseModel):
    allowed_scopes: list[OrganisationCrmWriteScope]

class OrganisationCrmSettings(BaseModel):
    mode: Literal["internal", "external"]
    provider: Literal["copilotgtm", "hubspot", "salesforce"]
    write_policy: OrganisationCrmWritePolicy

class OrganisationPolicySnapshot(BaseModel):
    crm: OrganisationCrmSettings
    channels: OrganisationChannelSettings
    capabilities: OrganisationCapabilities
```

`UserLoginResponse.organisation_preferences` can remain backward compatible for one release, but it
should be typed as the snapshot once frontend compatibility is confirmed.

Policy shape should avoid duplicate booleans. `crm.mode` and `crm.provider` describe which CRM owns
the organisation, while `crm.write_policy.allowed_scopes` is the only backend write-control list.
An empty `allowed_scopes` list means internal CopilotGTM CRM writes are not allowed. The previous
`internal_writes_enabled` and `write_policy.internal_records` flags are redundant and should not be
part of the production response contract.

CRM ownership is mutable only before CRM records exist. Once any deal row exists in the organisation
tenant schema, changing `crm.provider` from one CRM source to another must be rejected by the backend.
Messaging channel settings can still be updated after deals exist.

### Enforcement surfaces

Apply policy checks at every write boundary:

| Surface | Enforcement |
| --- | --- |
| Authenticated CRM HTTP writes | `Depends(require_crm_write_scope(scope))` |
| Public/private lead ingestion | explicit `require_crm_write(..., "lead_forms:ingest")` or `"leads:create"` |
| Agent direct writes | call policy service before write service construction |
| Voice realtime tools | filter tool manifest and enforce inside tools |
| Bot form submissions | call policy service after user resolution and before service mutation |
| Inbound bot messages | call `require_channel_enabled(org_id, provider)` before running GeneralAgent |
| Reminder execution over messaging | call `require_channel_enabled(org_id, delivery_channel)` before sending |

Enforcement must be backend-side even when the frontend hides controls.

### Tool registry integration

The agent registry already filters tools by channel. Extend that concept with organisation policy:

- If `crm.write_policy.allowed_scopes` excludes a tool's scope, do not expose the tool to the LLM.
- Still enforce inside the tool function so stale tool calls or direct tests cannot bypass policy.
- If a channel is disabled, do not process inbound messages from that channel.

Add a central map:

```python
TOOL_CRM_WRITE_SCOPES = {
    ToolName.CREATE_DEAL: "deals:create",
    ToolName.CREATE_CONTACT: "contacts:create",
    ToolName.CREATE_ACCOUNT: "accounts:create",
    ToolName.ADD_STAKEHOLDER: "deal_participants:create",
    ToolName.UPDATE_DEAL_FIELDS: "deals:update",
    ToolName.UPDATE_DEAL_LIFECYCLE: "deals:update",
    ToolName.UPDATE_DEAL_ACCOUNT: "deals:update",
    ToolName.UPDATE_DEAL_STAGE: "deals:update",
    ToolName.CREATE_TASK: "tasks:create",
}
```

This keeps prompt/tool availability aligned with backend enforcement.

## API Contract

### Admin update

`PATCH /admin/organisations/{organisation_id}/app-settings`

Request:

```json
{
  "crm": {
    "provider": "copilotgtm"
  },
  "channels": {
    "enabled": ["slack", "teams", "whatsapp", "google_chat"]
  }
}
```

Response should include the full normalized policy:

```json
{
  "organisation_id": "uuid",
  "preferences": {
    "crm": {
      "mode": "internal",
      "provider": "copilotgtm",
      "write_policy": {
        "allowed_scopes": ["deals:create", "deals:update"]
      }
    },
    "channels": {
      "enabled": ["slack", "teams", "whatsapp", "google_chat"]
    },
    "capabilities": {
      "can_create_deals": true,
      "can_edit_deals": true,
      "can_create_leads": true,
      "can_edit_leads": true,
      "can_manage_lead_forms": true
    }
  }
}
```

### User bootstrap

`POST /users/login` and `GET /users/me` should include the same normalized snapshot:

```json
{
  "organisation_id": "uuid",
  "organisation_preferences": {
    "crm": {
      "mode": "external",
      "provider": "hubspot",
      "write_policy": {
        "allowed_scopes": []
      }
    },
    "channels": {
      "enabled": ["slack", "teams"]
    },
    "capabilities": {
      "can_create_deals": false,
      "can_edit_deals": false,
      "can_create_leads": false,
      "can_edit_leads": false,
      "can_manage_lead_forms": false
    }
  }
}
```

Frontend should use this snapshot for UI gating, but the backend remains authoritative.

## Implementation Plan

### Phase 1: Stabilize the backend contract

1. Add typed policy models.
2. Narrow admin update request to writable fields only.
3. Make `normalize_organisation_preferences(...)` return a contract-compatible dict or typed model.
4. Add tests for default, legacy, internal, and external provider snapshots.
5. Add tests for admin update preserving unrelated preference branches and writing audit history.

### Phase 2: Centralize enforcement

1. Add `OrganisationPolicyService`.
2. Move HTTP guard implementation to the service.
3. Keep existing FastAPI dependency names as thin wrappers for backward readability.
4. Add tests for allowed and rejected HTTP writes using an external provider.

### Phase 3: Govern agent and form writes

1. Add CRM-write-scope checks to direct agent write tools.
2. Add CRM-write-scope checks to bot form handlers after user resolution.
3. Add `TOOL_CRM_WRITE_SCOPES` to the agent registry and hide unavailable write tools.
4. Add regression tests proving external CRM provider removes or blocks write tools.

### Phase 4: Govern messaging channels

1. Add `require_channel_enabled(...)`.
2. Enforce disabled channels before bot worker processing runs GeneralAgent.
3. Enforce disabled delivery channels before reminders send proactive messages.
4. Decide whether disabled channels should return a silent drop, a user-facing disabled message, or
   an admin-facing audit event. Recommended: user-facing disabled message for direct user messages,
   silent plus log for background reminder sends.

### Phase 5: API documentation and frontend handoff

1. Update `docs/API Documentation/admin/` with the app-settings endpoint.
2. Update `docs/API Documentation/users-auth-api.md` with login and `/me` policy snapshot.
3. Document frontend gating rules:
   - use `capabilities` for CRM create/edit affordances
   - use `channels.enabled` for channel availability
   - never infer backend permission from local UI state
4. Only after this is merged should the admin frontend settings page be finalized.

## Test Matrix

| Test area | Required cases |
| --- | --- |
| Preference normalization | missing prefs, legacy `crm.source`, legacy `integrations.enabled_sources`, invalid provider, duplicate channels |
| Admin update | provider only, channels only, both together, unrelated preference preservation, audit entry |
| User bootstrap | login active, login password-change-required, `/me`, external provider snapshot |
| HTTP writes | create deal allowed internally, create deal rejected externally, public lead form ingest rejected externally |
| Agent tools | create deal hidden and rejected externally, update deal hidden and rejected externally |
| Bot form handlers | stale create-deal card rejected externally, stale stage-update card rejected externally |
| Messaging channels | disabled Slack/Teams/WhatsApp/Google Chat inbound message blocked before agent execution |
| Reminder delivery | disabled delivery channel does not send proactive message |

## Rollout Notes

- Existing organisations without preferences should continue to behave as internal CopilotGTM CRM
  with all messaging channels enabled.
- Legacy `crm.source = "copilot_gtm"` and `integrations.enabled_sources` should remain readable for
  one release cycle.
- Do not expose per-scope admin toggles yet. Derive all CRM write scopes from provider until the
  product explicitly needs finer policy.
- Log rejected write attempts with organisation ID, channel, scope, and entry surface. Do not log
  secrets, tokens, form payloads, or lead body content.
- If frontend caches `organisation_preferences`, it must refresh on login and `/me`. Backend
  enforcement still applies immediately after admin changes.
