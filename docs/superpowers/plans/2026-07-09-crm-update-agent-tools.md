# CRM Update Agent Tools Production Plan

## Objective

Add production-grade agent tools for updating CRM deals, contacts, and accounts.

This document intentionally stops before implementation. The current codebase
already has most of the write services. The next implementation should reuse
those services instead of creating parallel mutation paths.

## Current State

### Agent Tool Surface

| Entity | Current agent create tool | Current search/read helper | Current update agent tool |
| --- | --- | --- | --- |
| Deal | `src/agents/tools/crm/create_deal.py` | `search_deals`, `get_deal_details`, `get_deal_change_history` | Partial: `update_deal_fields`, `update_deal_lifecycle`, `update_deal_account`, `update_deal_stage` |
| Contact | `src/agents/tools/crm/create_contact.py` | `search_contacts` | Missing |
| Account | `src/agents/tools/crm/create_account.py` | `search_accounts` | Missing |

`src/agents/tools/registry.py` currently exposes `CREATE_DEAL`,
`CREATE_CONTACT`, and `CREATE_ACCOUNT` to `GeneralAgent`. It also exposes the
deal update tools, but there are no `UPDATE_CONTACT` or `UPDATE_ACCOUNT`
`ToolName` entries, factories, category mappings, channel entries, or agent
allow-list entries.

### Existing Write Services To Reuse

| Entity | Service | Existing update method | Important current behavior |
| --- | --- | --- | --- |
| Deal | `src/service/crm/deals/write_service.py` | `update_deal`, `update_account`, `update_primary_contact`, lifecycle/stage helpers | Org-scoped load, internal CRM read-only guard, custom-field validation, mutation events, activity projection, explicit service errors |
| Contact | `src/service/crm/contacts/write_service.py` | `update_contact` | Org-scoped active-contact load, protected email changes, protected account relinking, person LinkedIn validation, mutation events |
| Account | `src/service/crm/accounts/write_service.py` | `update_account` | Org-scoped active-account load, domain duplicate protection, account LinkedIn validation, domain derivation from website, mutation events |

The correct production move is not to build new write services. Add thin agent
tools that validate input, resolve ambiguity, then call these services.

### Existing API Surface

The frontend API already has update routes:

- `PATCH /users/crm/deals/{deal_id}`
- `PATCH /users/crm/deals/{deal_id}/account`
- `PATCH /users/crm/deals/{deal_id}/primary-contact`
- `PATCH /users/crm/contacts/{contact_id}`
- `PATCH /users/crm/accounts/{company_id}`

Those routes already own transaction commit/rollback and call the same write
services listed above.

## Recommendation

Implement only two new agent tools now:

1. `update_contact`
2. `update_account`

Keep the existing deal update tools instead of adding a generic `update_deal`
tool in this first slice. Deal updates already have separate tools for scalar
fields, lifecycle, and account data. A new broad deal tool would duplicate policy
logic and increase the chance of wrong writes. If the product wants a single
deal-update tool later, consolidate the existing deal tools after proving the
contact/account tools.

## Tool Contracts

### `update_contact`

Input:

```text
contact_id: UUID
fields:
  email?: string
  first_name?: string
  last_name?: string
  full_name?: string
  job_title?: string
  phone?: string
  mobile_phone?: string
  linkedin_url?: string
  company_id?: UUID
```

Do not expose `company_name` as an update field in the agent tool. The service
currently accepts it if sent, but for agent writes this is risky because it can
change a presentation snapshot without linking to a real account. Agents should
use `search_accounts` and then set `company_id`.

Required guards:

- Require `organisation_id` from `AgentContext`.
- Require a valid `contact_id`; do not update by name/email directly.
- Require at least one supported field.
- Reject unknown fields before calling the service.
- Reuse `ContactWriteService.update_contact`.
- Pass `ContactWriteContext(source="agent_tool", actor_user_id=context.user_id, ...)`.
- Commit only after service success; rollback on any error.
- Return the updated ID, stable display fields, changed requested fields, and an
  explicit success/error status.

Service behavior already handles:

- contact must belong to the org and not be deleted
- existing email cannot be changed
- new email cannot duplicate another active contact
- linked account cannot be changed once set
- account link requires an email
- LinkedIn URL must be a person profile and cannot replace an existing URL
- mutation event recording

### `update_account`

Input:

```text
account_id: UUID
fields:
  name?: string
  domain?: string
  website?: string
  industry?: string
  lifecycle_stage?: string
  company_type?: string
  description?: string
  phone?: string
  linkedin_url?: string
  number_of_employees?: int
  annual_revenue?: float
  address?: string
  city?: string
  state?: string
  postal_code?: string
  country?: string
  timezone?: string
```

Required guards:

- Require `organisation_id` from `AgentContext`.
- Require a valid `account_id`; do not update by name/domain directly.
- Require at least one supported field.
- Reject unknown fields before calling the service.
- Reuse `AccountWriteService.update_account`.
- Pass `AccountWriteContext(source="agent_tool", actor_user_id=context.user_id, duplicate_scope="strict_org", ...)`.
- Commit only after service success; rollback on any error.
- Return the updated ID, stable display fields, changed requested fields, and an
  explicit success/error status.

Service behavior already handles:

- account must belong to the org and not be deleted
- domain/website normalization
- duplicate domain rejection inside the org
- LinkedIn URL must be an organization profile
- mutation event recording

## Deal Update Position

Keep these existing tools:

- `update_deal_fields`
- `update_deal_lifecycle`
- `update_deal_account`
- `update_deal_stage`

Do not add `update_deal` in the first implementation. The existing split is
safer because deal updates have different approval and validation policies:

- scalar fields can be direct or approval-gated
- lifecycle/stage changes can close, reopen, or move stages
- account updates can materialize/replace company participants
- primary contact updates exist in the service/API but do not yet have an agent
  tool

If primary-contact updates are required in the same rollout, add a narrow
`update_deal_primary_contact` tool instead of a generic deal mutation tool.

## Registry Changes

For contact/account update tools:

1. Add `ToolName.UPDATE_CONTACT = "update_contact"`.
2. Add `ToolName.UPDATE_ACCOUNT = "update_account"`.
3. Add both to `CHANNEL_ONLY_TOOLS` for `teams`, `slack`, `whatsapp`, `web`,
   and possibly `voice` only after voice schema/realtime behavior is reviewed.
4. Add both to `GENERAL_AGENT_TOOLS`.
5. Add both to `ToolCategory.CRM`.
6. Register factories in `ToolRegistry._register_default_tools`.
7. Export the factories from `src/agents/tools/crm/__init__.py`.

Voice should be conservative. Create tools are already exposed to voice through
the voice manifest. Update tools should remain excluded from voice until the
voice confirmation UX is explicitly validated.

## Approval And Safety Policy

Contact/account updates should start as direct-write tools only for low-risk
profile edits after the user has identified the exact record ID through search.
They should not infer a record from a fuzzy name and write it in the same call.

Production v1 policy:

- Search first when the user gives a name/domain/email.
- Update only when the tool receives a concrete `contact_id` or `account_id`.
- If multiple search results match, ask the user to choose.
- Do not batch multiple records in one tool call.
- Do not update provider-synced external CRM records from agent tools. Deals are
  already protected from mutating external CRM rows through `_load_internal_deal()`.
  Contact/account API services still allow org-owned active rows, so the agent
  tools must preflight the target row against the internal CRM integration before
  calling the shared write service.

Status after implementation: `update_contact` and `update_account` now enforce
this internal-CRM guard at the agent-tool boundary. This avoids changing existing
frontend/API semantics while keeping agent writes aligned with create-tool and
deal-update behavior.

## Minimum Implementation Plan

1. Add `src/agents/tools/crm/update_contact.py`.
   - Keep it shaped like `create_contact.py`.
   - Reuse `ContactWriteService.update_contact`.
   - Validate allow-listed fields in the tool before service call.

2. Add `src/agents/tools/crm/update_account.py`.
   - Keep it shaped like `create_account.py`.
   - Reuse `AccountWriteService.update_account`.
   - Validate allow-listed fields in the tool before service call.

3. Wire registry/export entries.
   - `ToolName`
   - `CHANNEL_ONLY_TOOLS`
   - `GENERAL_AGENT_TOOLS`
   - `ToolCategory.CRM`
   - `_tool_factories`
   - `src/agents/tools/crm/__init__.py`

4. Add focused tests.
   - registry exposes `UPDATE_CONTACT` and `UPDATE_ACCOUNT` to `GeneralAgent`
   - missing org context fails without opening a DB session
   - missing ID fails
   - empty fields fail
   - unknown fields fail
   - service error rolls back and returns an error
   - happy path calls the service, commits, and returns updated entity fields

5. Test the external-provider guard.
   - Agent tools must reject rows whose `integration_id` does not match the
     internal CRM integration.
   - Keep this as a tool-level guard unless product decides the frontend/API
     routes should also become internal-only.

## Non-Goals For First Slice

- No delete tools.
- No bulk update.
- No generic `update_crm_record` abstraction.
- No new dependency.
- No frontend API changes.
- No provider writeback to HubSpot/Salesforce.
- No workflow automation dispatch beyond existing mutation-event recording.

## Verification Checklist

Before implementation is called production-ready:

- `uv run pytest tests/agents/tools/crm -q`
- `uv run pytest tests/agents/tools/test_registry_deal_update_agent.py -q`
- targeted account/contact service tests if an internal-only guard is added
- `uv run pytest tests/service/crm/test_account_write_service.py tests/service/crm/test_contact_write_service.py -q`
- `uv run ruff check src/agents/tools/crm src/agents/tools/registry.py tests/agents/tools`

## Remaining Product Decision

Should frontend/API contact/account updates also be limited to internal CRM rows,
or should that restriction stay agent-only?

For production safety, agent updates are now internal-only unless there is an
explicit provider writeback design. This matches the deal write boundary and
avoids silently editing local synced snapshots that may later be overwritten by
HubSpot/Salesforce sync.
