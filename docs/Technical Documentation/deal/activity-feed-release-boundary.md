# CRM Activity Feed Release Boundary

## Current Source Of Truth

Tasks and notes are no longer source rows in `crm_activities`.

- `crm_tasks` stores canonical task state.
- `crm_notes` stores canonical note state.
- `crm_activities` stores compact feed-card projections.
- `crm_activity_links` stores object membership for deal, contact, and account feeds.

Runtime code should read task and note details from their source services or
source tables. It should read object activity feeds through
`crm_activity_links -> crm_activities`.

## Compatibility Boundary

`CRMActivity` still has legacy task/note/detail columns for compatibility with
existing schemas and feed projection rows. New task/note code must not use those
columns as source of truth.

Allowed `CRMActivity` usage in this branch:

- activity-feed projection writes and soft deletes,
- activity-feed read APIs,
- `CRMActivityLink` joins,
- schema and projection tests.

Blocked `CRMActivity` usage for task/note source behavior:

- selecting notes via `CRMActivity.deal_id` plus `activity_type = NOTE`,
- selecting tasks via `CRMActivity.activity_type in (TASK, TODO)`,
- reading task/note source details from `CRMActivity.properties`, `body`,
  `subject`, `owner_id`, or `external_assignee_*`,
- linking contacts/accounts through legacy `CRMActivity.contact_id` or
  `CRMActivity.company_id`.

## Out Of Scope

This branch does not implement full HubSpot/Salesforce all-activity ingestion.
Calls, CRM emails, events, meetings, and broader historical provider activity
models remain separate follow-up work.

## Release Gate

Before dropping legacy `crm_activities` columns, verify:

- source-table task/note APIs and agent tools pass,
- provider task/note writers target `CRMTask` and `CRMNote`,
- contact privacy deletion uses `crm_activity_links` and source rows,
- deal memory note eligibility uses `CRMNote`,
- activity-feed APIs for deals, contacts, and accounts pass,
- archived docs are clearly marked historical where they teach the old model.
