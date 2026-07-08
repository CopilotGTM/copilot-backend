# Leads Service Architecture

## Decision

All lead-domain service code must live under `src/service/crm/leads/`.

`src/service/crm/access/` is deprecated for lead code. During migration it may contain compatibility imports only, but new lead behavior, queries, validation, key handling, and form submission logic must not be added there.

Routers must stay thin. A router may own FastAPI dependencies, rate-limit dependencies, auth dependencies, request parsing, transaction rollback orchestration, and choosing the HTTP response helper. It must not own response projection, public/private error body construction, URL construction, or structured log payload construction.

Presenters own API shape. If code builds a `LeadListItem`, `LeadFormItem`, `PublicLeadFormErrorResponse`, `PrivateLeadIngestionErrorResponse`, `public_form_url`, or log `extra={...}` payload, it belongs in `src/api/presenters/crm/`.

Services own domain behavior. If code reads/writes `CRMLead`, `LeadForm`, `LeadFormSubmission`, `PublicIngressKey`, validates form mappings, authenticates ingestion keys, or converts leads to deals, it belongs in `src/service/crm/leads/`.

## Original Problem

Lead code was split across these places:

- `src/service/crm/access/leads_access_service.py`
- `src/service/crm/access/lead_forms_service.py`
- `src/service/crm/access/lead_ingestion_key_service.py`
- `src/service/crm/access/private_lead_ingestion_auth_service.py`
- `src/service/crm/access/private_lead_ingestion_service.py`
- `src/service/crm/leads/write_service.py`
- `src/api/routers/crm/leads/leads_handler.py`
- `src/api/routers/crm/leads/lead_forms_handler.py`
- `src/api/routers/crm/leads/lead_ingestion_keys_handler.py`
- `src/api/routers/crm/leads/private_lead_ingestion_handler.py`

That made the boundary unclear. Some handlers contained presenter behavior; some services lived in `access`; the new `leads` package only had direct private ingestion write logic, so the package name said one thing and the code layout said another.

## Current Implementation Status

Implemented in the lead service refactor:

- Lead service imports now come directly from `src/service/crm/leads/`; no lead code imports `src.service.crm.access.*lead*`.
- Lead-related files were removed from `src/service/crm/access/`. That deprecated folder still exists for non-lead CRM domains only.
- Normal lead flows are split by workflow:
  - `contracts.py`: shared `LeadServiceError`
  - `read_service.py`: `LeadReadService` for list/detail reads
  - `write_service.py`: `LeadWriteService` for authenticated create/update
  - `participant_service.py`: `LeadParticipantService` for participant writes and participant-copy support
  - `conversion_service.py`: `LeadConversionService` for lead-to-deal conversion
- Lead form/key/ingestion flows now live in the same package:
  - `form_management_service.py`
  - `ingestion_key_service.py`
  - `ingestion_auth_service.py`
  - `private_ingestion_service.py`
- Lead router response projection and public-form error/log construction moved to API presenters:
  - `src/api/presenters/crm/lead_presenter.py`
  - `src/api/presenters/crm/lead_form_presenter.py`
  - `src/api/presenters/crm/private_lead_ingestion_presenter.py`
- `tests/service/crm/leads/test_leads_service_architecture.py` enforces the boundary: no lead access imports, no lead behavior under `access`, no presenter helpers in lead routers, and no write methods in `read_service.py`.

Not yet split because it is a larger behavior-preserving follow-up:

- `form_management_service.py` still contains both authenticated form management and `PublicLeadFormSubmissionService`. The target remains to split mapped submission behavior into `form_submission_service.py` and reusable mapping rules into `field_mapping_service.py` when that logic is touched next.

## Target Folder Structure

```text
src/service/crm/leads/
  __init__.py
  contracts.py
  read_service.py
  write_service.py
  conversion_service.py
  participant_service.py
  form_management_service.py
  form_submission_service.py
  ingestion_key_service.py
  ingestion_auth_service.py
  private_ingestion_service.py
  field_mapping_service.py
```

```text
src/api/presenters/crm/
  lead_presenter.py
  lead_form_presenter.py
  private_lead_ingestion_presenter.py
```

The target does not require every file to be created in one PR. Create a file only when moving real behavior into it. Empty symmetry files are not allowed.

## Service Files

### `contracts.py`

Shared lead-domain contracts and HTTP-shaped service errors.

Owns:

- `LeadServiceError`
- `LeadFormsServiceError`
- `LeadIngestionKeyServiceError`
- `PrivateLeadIngestionAuthError`
- `PrivateLeadIngestionServiceError`
- result dataclasses such as `LeadConversionResult`

Rule: service errors live with services, not routers or presenters.

### `read_service.py`

Read-only lead access.

Owns:

- `list_leads`
- `get_lead`
- loading participants for detail responses

Does not own:

- create/update/convert
- response DTO construction
- pagination response model construction

### `write_service.py`

Authenticated first-party lead mutations.

Owns:

- `create_lead`
- `update_lead`
- status transition audit rows
- direct lead validation that applies to authenticated writes

Does not own:

- deal conversion orchestration
- public/private form submission mapping
- API response DTOs

### `participant_service.py`

Lead participant writes and participant-copy helpers.

Owns:

- `add_participant`
- participant identity normalization
- lead participant to deal participant copy support

This can start inside `conversion_service.py` if only conversion uses it, but it should be extracted once participant behavior has more than one caller.

### `conversion_service.py`

Lead-to-deal orchestration.

Owns:

- `convert_lead`
- interaction with `DealWriteService`
- conversion metadata
- lead status mutation to `converted`
- participant copy into deal participants

Reason: conversion is a cross-domain workflow, not a simple lead write. Keeping it out of `write_service.py` makes the write service readable.

### `form_management_service.py`

Authenticated lead-form configuration.

Owns:

- list/create/get/update lead form definitions
- status changes
- creation/update of public form ingress keys
- validation of field mappings on create/update

This replaces the management side of `src/service/crm/access/lead_forms_service.py`.

### `form_submission_service.py`

Public and private mapped form submissions.

Owns:

- public form definition loading
- public submissions
- private mapped submissions
- payload budget checks
- spam checks
- field coercion
- idempotency/replay for form submissions
- creation of `LeadFormSubmission`
- creation of form-sourced `CRMLead`

This replaces `PublicLeadFormSubmissionService`.

### `field_mapping_service.py`

Reusable lead-form field mapping and custom-field validation.

Owns:

- built-in lead target allowlist
- lead form data-type compatibility checks
- lead custom-field lookup/validation
- conversion from incoming payload fields to lead columns/custom values

Reason: the current lead form service is large. Field mapping is high-risk business logic and should be readable without scrolling through form CRUD and submission persistence.

### `ingestion_key_service.py`

Private ingestion key lifecycle.

Owns:

- create/list/update private key records
- secret generation
- secret hashing/verification
- conversion of key rows to key API item data

This replaces `src/service/crm/access/lead_ingestion_key_service.py`.

### `ingestion_auth_service.py`

External private-ingestion authentication.

Owns:

- key id validation
- bearer secret extraction
- key lookup
- key status/expiry policy
- allowed IP validation
- private ingestion rate-limit enforcement
- `last_used_at` update

This replaces `src/service/crm/access/private_lead_ingestion_auth_service.py`.

### `private_ingestion_service.py`

Direct private lead ingestion for `/private/v1/crm/leads`.

Owns:

- direct private lead payload validation
- direct private lead idempotency
- normalization of email/domain
- creation of direct API-sourced `CRMLead`

This file should contain the current direct private ingestion behavior that temporarily lives in `write_service.py`. Direct private ingestion is a write, but it has a separate external contract, idempotency policy, and auth flow, so it deserves its own service file.

## Presenter Files

### `lead_presenter.py`

Owns:

- `LeadListItem` construction
- `LeadDetail` construction
- `LeadParticipantItem` construction
- `LeadConversionResponse` construction
- pagination envelope data such as `LeadsPage`

Move these out of `leads_handler.py`:

- `_lead_item`
- `_participant_item`
- `_lead_detail`
- `_conversion_response`

### `lead_form_presenter.py`

Owns:

- `LeadFormFieldItem` construction
- `LeadFormItem` construction
- public form URL construction
- public form error code mapping
- public form error response construction
- public rate-limit error response construction
- public unexpected error response construction
- structured public lead form failure logging

Move these out of `lead_forms_handler.py`:

- `_field_item`
- `_public_form_url`
- `_form_item`
- `_public_error_code`
- `_log_public_form_failure`
- `_public_error_response`
- `_public_rate_limit_error_response`
- `_public_unexpected_error_response`

`_load_active_public_form_key` should not go to a presenter because it queries the database. Move it to `form_submission_service.py` or a private helper inside `form_management_service.py`, depending on caller ownership.

### `private_lead_ingestion_presenter.py`

Owns:

- private ingestion error response construction
- private ingestion validation error field extraction
- private ingestion structured failure logging
- direct/private mapped success response construction

This file already exists and should remain the API presentation boundary for `private_lead_ingestion_handler.py`.

## Router Rules

Routers may do:

- declare routes
- declare FastAPI dependencies
- call rate-limit functions
- call auth/write-scope dependencies
- acquire org/master sessions through dependencies or existing session factories
- instantiate services
- call services
- rollback service sessions on service/write failures where the service owns the transaction boundary
- call presenter functions
- return `Response.*` or `JSONResponse`

Routers may not do:

- build response DTOs from ORM objects
- build public/private error response objects
- build structured log dictionaries
- normalize lead/form fields
- validate field mappings
- query domain tables directly, except short-lived migration shims while moving logic into services

## Deprecating `src/service/crm/access`

Target rule:

- No import from `src.service.crm.access.*lead*` in routers, services, agents, workers, or tests.
- No lead behavior remains in `src/service/crm/access`.
- Compatibility shims may exist temporarily, but only as imports from `src.service.crm.leads.*`.

Allowed temporary shim shape:

```python
"""Compatibility imports for migrated lead service."""

from src.service.crm.leads.read_service import LeadReadService

__all__ = ["LeadReadService"]
```

Not allowed:

- copied logic in both old and new files
- new behavior added to `access`
- tests importing from `access` after the migration task that moves that service

## Migration Order

### Phase 1: Presenters

Move handler helper functions into presenters without changing service behavior.

Files:

- create/modify `src/api/presenters/crm/lead_presenter.py`
- create/modify `src/api/presenters/crm/lead_form_presenter.py`
- modify `src/api/routers/crm/leads/leads_handler.py`
- modify `src/api/routers/crm/leads/lead_forms_handler.py`

Tests:

- add presenter tests for lead detail/list/conversion projection
- add presenter tests for public lead form URL/error/log projection
- run existing lead handler tests

### Phase 2: Lead Read/Write/Conversion

Move `LeadsAccessService` into real lead services.

Files:

- create `src/service/crm/leads/contracts.py`
- create `src/service/crm/leads/read_service.py`
- create/modify `src/service/crm/leads/write_service.py`
- create `src/service/crm/leads/conversion_service.py`
- create temporary shim `src/service/crm/access/leads_access_service.py`
- modify `src/api/routers/crm/leads/leads_handler.py`

Router dependency target:

- `get_lead_read_service`
- `get_lead_write_service`
- `get_lead_conversion_service`

Tests:

- migrate imports in lead service tests from `access` to `leads`
- keep one temporary compatibility test only if shims remain

### Phase 3: Lead Forms

Split `lead_forms_service.py`.

Files:

- create `src/service/crm/leads/form_management_service.py`
- create `src/service/crm/leads/form_submission_service.py`
- create `src/service/crm/leads/field_mapping_service.py`
- create temporary shim `src/service/crm/access/lead_forms_service.py`
- modify `src/api/routers/crm/leads/lead_forms_handler.py`
- modify `src/api/routers/crm/leads/private_lead_ingestion_handler.py`

Tests:

- split tests by behavior, not by old file name:
  - `tests/service/crm/leads/test_form_management_service.py`
  - `tests/service/crm/leads/test_form_submission_service.py`
  - `tests/service/crm/leads/test_field_mapping_service.py`

### Phase 4: Ingestion Keys and Auth

Move key lifecycle and private auth.

Files:

- create `src/service/crm/leads/ingestion_key_service.py`
- create `src/service/crm/leads/ingestion_auth_service.py`
- create `src/service/crm/leads/private_ingestion_service.py`
- create temporary shims in `src/service/crm/access/`
- modify `src/api/routers/crm/leads/lead_ingestion_keys_handler.py`
- modify `src/api/routers/crm/leads/private_lead_ingestion_handler.py`

Tests:

- migrate `tests/service/crm/test_private_lead_ingestion_service.py` into `tests/service/crm/leads/`
- migrate auth/key tests to import from `src.service.crm.leads`

### Phase 5: Remove Shims

Remove lead-related files from `src/service/crm/access/` after all imports are migrated.

Delete:

- `src/service/crm/access/leads_access_service.py`
- `src/service/crm/access/lead_forms_service.py`
- `src/service/crm/access/lead_ingestion_key_service.py`
- `src/service/crm/access/private_lead_ingestion_auth_service.py`
- `src/service/crm/access/private_lead_ingestion_service.py`

Verification:

```powershell
rg -n "src\.service\.crm\.access\.(lead|leads|private_lead)" src tests
```

Expected result: no matches.

## Naming Rules

Use `LeadReadService`, not `LeadsAccessService`.

Use `LeadWriteService`, not `LeadsAccessService`.

Use `LeadConversionService`, not a conversion method buried inside a generic access service.

Use `LeadFormManagementService` for authenticated form CRUD.

Use `LeadFormSubmissionService` for public/private mapped submission workflows.

Use `LeadIngestionKeyService` for key lifecycle.

Use `PrivateLeadIngestionAuthService` for external private auth.

Use `PrivateLeadIngestionService` for direct private lead ingestion.

Use `LeadFieldMappingService` for mapping/field compatibility rules.

Do not use `AccessService` for new code.

## Human Readability Rules

Each service file should be readable top to bottom:

- constants first
- service error/result imports from `contracts.py`
- public service methods before private helpers
- one workflow per public method
- no API schema construction inside services unless the schema is the service input contract already used today
- comments only for invariants, data ownership, security boundaries, and intentional shortcuts

If a file exceeds roughly 500 lines and has unrelated workflows, split it by workflow. `lead_forms_service.py` is already past that point and should be split.

## What The Asked-About Handler Helpers Should Become

The helpers below should not stay in `lead_forms_handler.py`:

- `_field_item`: move to `lead_form_presenter.py`
- `_public_form_url`: move to `lead_form_presenter.py`
- `_form_item`: move to `lead_form_presenter.py`
- `_public_error_code`: move to `lead_form_presenter.py`
- `_log_public_form_failure`: move to `lead_form_presenter.py`
- `_public_error_response`: move to `lead_form_presenter.py`
- `_public_rate_limit_error_response`: move to `lead_form_presenter.py`
- `_public_unexpected_error_response`: move to `lead_form_presenter.py`

The helper below should not stay in `lead_forms_handler.py`, but it should not go to a presenter:

- `_load_active_public_form_key`: move to `form_submission_service.py`, because it is a database lookup and part of public form resolution.

## Non-Goals For The First Leads Refactor

- Do not redesign lead tables.
- Do not change endpoint paths.
- Do not change response contracts unless a test proves the current contract is wrong.
- Do not introduce repository classes just to avoid SQLAlchemy in services.
- Do not create empty services for symmetry.
- Do not remove non-lead services from `src/service/crm/access` in the lead migration PR.
