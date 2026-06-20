# CRM Write Service Enhancement Plan

## Why This Plan Exists

We paused the workflow phases because the CRM write surface is already showing
frontend behavior regression risk. The current backend has a strong mutation
event direction, but the runtime write contract is still split:

- Deal detail reads custom fields as `custom_columns`.
- Deal update writes custom fields as `custom_properties`.
- Deal creation can already accept `custom_properties` through the service
  payload, but those values are merged into `CRMDeal.properties` without the
  same custom-field validation used by updates.
- `PATCH /users/crm/deals/{deal_id}` returns a minimal `DealWriteResponse`, not
  the same shape the frontend detail page reads.
- `DealWriteService` is carrying core deal writes, participants, custom fields,
  lifecycle, and mutation events in one large class.

For an agentic CRM, custom fields cannot be treated as loose JSON extras. They
need to be first-class editable CRM data with schema validation, audit history,
and later workflow trigger support.

## Decision

Do not continue Phase 3 until the deal write boundary is stable for frontend and
agent writes.

Keep `DealWriteService` as the public facade for now, but split stable internal
responsibilities behind it. The goal is not a big rewrite. The goal is to make
the frontend/API contract deterministic before routing creation and general
updates through mutation events.

Correction after loophole review: Phase A should not require the write endpoint
to return the full `DealDetail` shape in the first slice. Returning full detail
from the write handler would duplicate the large read-side hydration path
(owners, team members, custom definitions, associated counts, job visibility).
The safer production v1 is:

```text
write endpoint returns a compact write response + frontend refetches deal detail
```

If the frontend needs no-refetch optimistic updates later, add a dedicated
`DealWriteDetailResponse` only after the backend has one shared detail mapper.

## Target Shape

```text
API handler
  -> DealWriteService facade
      -> DealScalarFieldWritePolicy
      -> DealCustomFieldWritePolicy
      -> DealMutationDiffPolicy
      -> DealMutationEventBuilder
      -> participant/account/contact helpers
  -> commit
  -> response mapper
```

The frontend and agents should both write through the same canonical request
shape. Internally, custom fields should still be stored in `CRMDeal.properties`
for now, but the API contract should make them explicit.

Canonical API naming:

```text
read:  DealDetail.custom_columns
write: DealCreateRequest.custom_columns, DealUpdateRequest.custom_columns
legacy write alias: custom_properties
internal storage: CRMDeal.properties
```

`custom_properties` remains a server-side/internal name and a temporary incoming
alias only. It should not be the preferred frontend contract.

## Phase A: Restore And Freeze Frontend Write Contract

Goal: frontend edit flows should have one predictable read/write shape.

Status on 2026-06-17: backend Phase A is implemented for request
normalization and custom-field validation/clearing. Frontend migration and
detail refetch remain Phase D.

1. Support `custom_columns` as the public API write name for deal custom fields.
2. Support `custom_columns` on both deal update and deal creation.
3. Keep accepting `custom_properties` temporarily as a backward-compatible alias.
   Mark it deprecated in API docs once frontend has migrated.
4. Normalize both names into one internal `custom_properties` dict before service
   validation.
5. Reject requests that send both names with conflicting values.
6. Preserve explicit custom-field clears on update:
   - omitted custom field = no change
   - custom field present with `null` = clear the value by removing the stored
     JSON property
   - empty string follows the field type coercion rules, not silent omission
7. On creation, validate custom fields before merging them into
   `CRMDeal.properties`. No custom field value should bypass definition lookup,
   type coercion, formula read-only checks, inactive/deleted checks, or reserved
   key protection.
8. Keep formula, inactive, deleted, and unknown custom fields rejected through
   the same validation path used today.
9. Add explicit handling for all supported custom field data types:
   - `text`
   - `integer`
   - `number`
   - `percentage`
   - `boolean`
   - `date`
   - `datetime`
   - `currency`
   - `select`
   - `multiselect`
   - `formula` as read-only
10. Keep the write response compact and require frontend detail refetch after a
    successful write. Do not duplicate `DealDetail` hydration in the write
    handler yet.
11. Add API/service tests for:
    - update scalar field only
    - update custom field only through `custom_columns`
    - update scalar plus custom fields in one request
    - create deal with `custom_columns`
    - conflicting `custom_columns` and `custom_properties`
    - clearing a custom field with `null`
    - formula/inactive/unknown custom field rejection
    - percentage custom field coercion
    - reserved custom key rejection

Verification:

```bash
uv run pytest tests/api/routers/crm/test_deals_handler.py -k "custom_columns or update_deal" -q
uv run pytest tests/service/crm/test_deal_write_service_account_updates.py -q
uv run pytest tests/agents/tools/crm/test_create_deal_direct.py tests/agents/tools/crm/test_deal_update_tools.py -q
```

## Phase B: Extract Deal Mutation Policies

Goal: reduce `DealWriteService` size without changing callers.

Extract only stable, pure logic first, but do not move DB-backed custom field
validation yet. The first extraction must be behavior-preserving and low-risk.

1. `DealMutationDiffPolicy`
   - auditable field allowlist
   - UUID/datetime/status normalization
   - old/new `changed_fields` construction
   - semantic event type derivation

2. `DealMutationEventBuilder`
   - event type resolution
   - payload hash construction
   - event summary helpers
   - mutation event model construction

3. Later, `DealCustomFieldWritePolicy`
   - active custom field lookup
   - key normalization
   - type coercion
   - formula field read-only enforcement
   - reserved property collision protection

Do not split account/contact/team participant logic yet. That area has separate
frontend behavior history and should be extracted only after write contract tests
are stable.

Verification:

```bash
uv run pytest tests/service/crm/test_deal_write_service_stage_history.py -q
uv run pytest tests/service/crm/test_custom_field_source_isolation.py -q
```

## Phase C: Make Custom Fields Agentic-CRM Ready

Goal: custom fields are safe for frontend and agent writes, and ready for future
workflow triggers.

1. Treat custom field changes as canonical changed-field keys:
   - `custom.<field_key>`
   - example: `custom.renewal_risk = {"old": "medium", "new": "high"}`
2. Keep custom field values in the mutation event `changed_fields`.
3. Store custom field diffs only after values are normalized/coerced to their
   definition type so audit and workflow comparisons do not depend on frontend
   input formatting.
4. Do not make every custom field workflow-triggerable by default.
5. Later, allow a custom field definition to opt into trigger semantics:
   - `crm.deal.custom_field_changed`
   - matched field key stored as `custom.<field_key>`
6. Ensure formula fields remain read-only for both frontend and agent writes.
7. Ensure inactive/deleted custom fields cannot be written.

Verification:

```bash
uv run pytest tests/service/crm/test_custom_field_source_isolation.py -q
uv run pytest tests/service/crm/test_deal_write_service_stage_history.py -q
```

## Phase D: Frontend Alignment

Goal: make the web app use the stable backend contract without guessing field
names or relying on partial write responses.

1. Frontend writes custom fields as `custom_columns`.
2. Frontend stops sending `custom_properties` once backend alias support is
   deployed.
3. Frontend refetches deal detail after successful write instead of assuming the
   compact write response contains all read-side fields.
4. Frontend keeps using `custom_column_definitions` from detail for typed
   controls and validation hints.
5. Frontend sends explicit `null` when the user clears a custom field.
6. Frontend omits untouched custom fields from PATCH payloads.

Verification should happen in `D:\CopilotGTM\copilot-app-frontend` after the
backend slice lands:

```bash
yarn test:jest __tests__/unit/components/deals/create-deal-drawer.test.tsx
yarn test:jest __tests__/unit/components/deals/deal-detail-layout-client.test.tsx
```

## Phase E: Resume Mutation Contract Phases

After A-D are stable:

1. Phase 3: route deal creation through mutation contract.
2. Phase 4: route general deal updates through mutation contract.
3. Phase 5: route lifecycle operations through mutation contract.
4. TODO future: Phase 6 dispatcher/workflow trigger fanout.

## Loopholes To Avoid

1. Do not expose `properties` as the public API write contract. It contains
   reserved internal metadata and will become messy.
2. Do not force the frontend to know backend storage names like
   `custom_properties` if read-side calls them `custom_columns`.
3. Do not make every custom field a workflow trigger. That will create noise and
   expensive dispatch.
4. Do not split `DealWriteService` into many caller-facing services yet. Keep one
   public write facade until behavior is stable.
5. Do not change account/contact participant behavior in the same refactor. That
   path already had frontend-sensitive regressions before.
6. Do not dispatch workflows from the API transaction. Continue using the
   mutation outbox boundary.
7. Do not make the write endpoint rebuild full `DealDetail` unless the detail
   mapping is shared. Otherwise write responses and read responses will drift.
8. Do not treat `None` and omitted custom fields the same. That would make the
   frontend unable to clear a custom field.
9. Do not extract DB-backed custom field validation before the alias/refetch
   contract is tested. That combines behavior repair with structural refactor.
10. Do not let `custom_columns` bypass reserved-key protection. A custom field
   key must never overwrite `is_internal_crm`, `created_via`, `updated_via`,
   `deleted_via`, `deal_external_ref`, or `import_source_ref`.
11. Do not fix update-only and forget creation. Create and update must share
   the same custom-field validation before workflow mutation events are added.
12. Do not ignore `percentage`. It is already a declared custom field type, so
   either coerce it explicitly or reject it consistently. Silent pass-through is
   not production-grade.
13. Do not implement backend alias support without a frontend migration task.
   Otherwise both names will live forever and future agents will learn the wrong
   contract.

## Recommended Next Implementation Slice

Start with Phase A only.

Reason: it directly addresses the frontend regression risk and gives us a clean
contract for later mutation-event routing.

Concrete first slice:

1. Add `custom_columns` to `DealUpdateRequest`.
2. Add `custom_columns` to `DealCreateRequest`.
3. Normalize `custom_columns` and `custom_properties` in the API handler or
   request model.
4. Add conflict validation if both are sent differently.
5. Route creation and update custom fields through the same validation/coercion
   path.
6. Add explicit percentage custom field coercion.
7. Keep the write response compact; frontend should refetch deal detail after
   success.
8. Write tests for frontend-shaped payloads, conflict handling, null clears,
   creation validation, percentage coercion, and validation failures.
9. Do not change workflow dispatch, mutation-event creation routing, lifecycle
   routing, or account/contact participant behavior yet.
