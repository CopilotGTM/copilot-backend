# Agent CRM Lifecycle Triggers Implementation Plan

## Goal

Expose and execute the six CRM events already produced by the CRM mutation ledger:

- `crm.deal.created`
- `crm.deal.stage_changed`
- `crm.contact.created`
- `crm.contact.deleted`
- `crm.account.created`
- `crm.account.deleted`

The frontend remains catalog-driven. It must not hard-code or manufacture trigger definitions that
the backend cannot validate and activate.

## Design

1. Keep one backend-owned registry mapping each supported event type to its versioned schema key.
2. Build the trigger catalog from those supported contracts while retaining event-specific fields.
3. Validate each catalog definition strictly: deal events may filter by pipeline; contact/account
   lifecycle events have no V1 filters beyond their schema key.
4. Activate supported semantics from the shared CRM mutation recorder so contact, account, and deal
   writes use one hook. Remove the deal-only duplicate dispatch.
5. Resolve the event entity and its owner within the organization, match owner-level triggers, and
   create replay-safe pending agent runs. Deleted entities remain loadable for delete events.
6. Document the exact catalog payload and event-specific configuration for frontend rendering.

## Success Criteria

- The catalog endpoint returns schedule plus all six CRM events.
- Trigger create/update validation accepts only the documented configurations and schema versions.
- Recording any supported CRM semantic event attempts activation exactly once.
- A matching active owner trigger creates one pending run; replay does not duplicate it.
- Unsupported CRM mutation semantics remain audit-only.
- Existing schedule and deal-stage behavior remains covered by tests.

## Verification

- Focused catalog, validator, activation, access, and CRM mutation-service tests.
- Existing automation trigger API/service tests.
- Formatting/type checks scoped to changed Python files, plus `git diff --check`.
