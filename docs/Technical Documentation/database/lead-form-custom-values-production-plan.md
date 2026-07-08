# Lead Form Custom Values Production Plan

Date: 2026-07-06
Scope: CRM lead forms, public form submissions, and private mapped form submissions.

## Executive Summary

The backend does not have a hard "2 or 3 fields only" limit. In the current code, authenticated lead-form creation accepts up to `100` configured fields per form, and public/private mapped submissions accept up to `200` submitted field keys per request.

The real backend limitation is different:

- Fixed lead columns are allowlisted in `src/service/crm/access/lead_forms_service.py` through `_LEAD_FIELD_TARGETS`.
- Arbitrary configured values can already be stored through `target_type = "property"` into `CRMLead.properties`.
- On the base branch those lead properties are raw JSONB and are not backed by a lead custom-field registry, typed option catalogs, searchable metadata, or reusable validation like deal custom fields.
- This implementation extends the existing CRM custom-field registry with `CRMCustomFieldEntity.LEAD` and validates new form property mappings against active internal lead custom-field definitions.
- The UI-facing custom-field CRUD service already exists in `src/service/crm/internal/custom_field_write_service.py`; this implementation keeps deal usage checks scoped to deals and clears archived lead field values from `CRMLead.properties`.

So the production-level answer is not "raise a 2-field limit." The right build is to add a lead custom-field schema layer and make forms map to it, while preserving the existing `LeadFormField -> CRMLead.properties` storage path for backward compatibility.

Second-pass production review: expanding custom values is not enough by itself. Before this is production-grade lead generation, the mapped form submission path also needs stricter payload budgets, typed validation, spam controls, duplicate-submit handling, PII retention rules, dedupe/routing semantics, and incident diagnostics. The direct private lead API already has its own payload budget guardrails; do not rebuild those in the form work. The remaining form-specific gates are hard gates, not nice-to-have cleanup.

## Current Production Behavior

Production runtime check on 2026-07-06:

- Kubernetes context checked: `copilot-gtm-aks-prod`.
- API deployment: `copilotgtm/copilot-backend`.
- API image: `copilotgtmacrprod.azurecr.io/copilot-backend-api:1749`.
- Replicas available: `2`.
- Direct deployment env does not expose `RATE_LIMIT_PUBLIC_FORM_*` overrides; the deployment reads most settings from `copilot-backend-secret`. Do not document secret values.

Code behavior in the current worktree:

- Authenticated management routes are mounted under `/users/crm/lead-forms`.
- Public browser routes are mounted under `/public/v1/forms/{public_form_key}`.
- Private mapped form route is mounted under `/private/v1/forms/submissions`.
- Direct private lead ingestion remains separate at `/private/v1/crm/leads`.
- Public form keys live in the public/master `public_ingress_keys` table and resolve to an org-schema `lead_forms.form_id`.
- Form configuration lives in org-schema `lead_forms` and `lead_form_fields`.
- Form submissions live in org-schema `lead_form_submissions`.
- Leads created from form submissions live in org-schema `crm_leads`.

Current limits:

| Area | Current Limit | Source |
| --- | ---: | --- |
| Allowed origins per form | 50 | `MAX_ALLOWED_ORIGINS` |
| Configured fields per form | 100 | `MAX_LEAD_FORM_FIELDS` |
| Submitted field keys per request | 200 | `MAX_PUBLIC_SUBMISSION_FIELDS` |
| Public context keys | 50 | `MAX_PUBLIC_CONTEXT_KEYS` |
| Field key length | 128 chars | `MAX_PUBLIC_FIELD_KEY_LENGTH` |
| Scalar string value length | 10,000 chars | `MAX_PUBLIC_SCALAR_STRING_LENGTH` |
| Mapped form payload bytes | 256 KB | `_MAPPED_FORM_PAYLOAD_BYTES` |
| Mapped form payload depth | 8 | `_MAPPED_FORM_PAYLOAD_DEPTH` |
| Mapped form payload nodes | 2,000 | `_MAPPED_FORM_PAYLOAD_NODES` |
| Mapped form list items | 200 | `_MAPPED_FORM_PAYLOAD_LIST_ITEMS` |
| Direct private payload bytes | 256 KB | `MAX_PRIVATE_PAYLOAD_BYTES` |
| Direct private payload depth | 8 | `MAX_PRIVATE_PAYLOAD_DEPTH` |
| Direct private payload nodes | 2,000 | `MAX_PRIVATE_PAYLOAD_NODES` |
| Direct private list items | 200 | `MAX_PRIVATE_PAYLOAD_LIST_ITEMS` |
| Public definition rate limit default | 240/min, 5000/hour | `rate_limit_config` defaults |
| Public submit rate limit default | 180/min, 3000/hour, 100/day | `rate_limit_config` defaults |

Submission behavior:

- Unknown submitted field keys are rejected.
- Required configured fields are enforced.
- A submission must map to at least one of `email`, `phone`, or `full_name`.
- `target_type = "lead_field"` writes to one of the allowlisted CRM lead columns.
- `target_type = "property"` writes to `CRMLead.properties[target_key]`.
- `target_type = "ignore"` accepts a configured field but does not write it to the lead.
- Public submissions create `CRMLead.source_type = "form"`.
- Private mapped form submissions create `CRMLead.source_type = "api"` while still linking `form_id` and `form_submission_id`.
- Private mapped form submissions require `Idempotency-Key` and replay through persisted idempotency columns.
- Direct private lead ingestion has separate payload budget limits: 256 KB encoded payload, depth 8, 2,000 nodes, 200 list items, and 10,000-character scalar strings.
- Chat/agent lead creation exists in this worktree through `ToolName.CREATE_LEAD`; it creates manual chat-sourced leads through `LeadsAccessService.create_lead(...)` and is separate from lead forms.

Implemented in this branch:

- `email`, `phone`, `date`, `datetime`, `number`, `boolean`, and `json` form field types are coerced/validated in mapped submissions.
- Public submissions accept an optional `idempotency_key`; matching browser retries replay the original submission.
- Public and private mapped form submissions enforce total request bytes, JSON depth, total node count, list length, top-level key count, field-key length, and scalar string length.
- Public forms have provider-neutral spam hooks in `settings.spam_protection` for honeypot fields and minimum-fill-time checks.
- Property mappings on new/updated forms resolve to active internal lead custom-field definitions, while existing legacy raw-property mappings remain compatible.
- Submitted property values are coerced through the lead custom-field validator when a definition exists, including select/multiselect option membership.
- Existing property-mapped form fields can be backfilled into lead custom-field definitions with `scripts/maintenance/backfill_lead_form_custom_fields.py`; this stays out of Alembic because it is data-entry backfill, not schema evolution.
- `required + ignore` form fields are rejected so admins cannot require a value that the backend intentionally drops.

Remaining current gaps and explicit product decisions:

- Public forms still do not have a selected CAPTCHA/challenge provider, disposable-email controls, or domain/blocklist controls.
- `lead_form_submissions.raw_payload` stores submitted PII without a documented retention/minimization policy.
- `LeadFormsService.create_form()` writes org-schema form rows and master-schema ingress key rows using two independent sessions; rollback exists, but cross-database/session atomicity is not guaranteed if one commit succeeds and the other fails.
- Existing form fields are updated in place with `form_version`, so historical field-definition reconstruction depends on the submission's raw/mapped payload rather than immutable field snapshots.

## Why Teams Feel Blocked

From the backend, many fields can be configured today. The likely product gap is that teams need "custom values" to behave like first-class CRM fields, not anonymous JSON:

- Admins need to define many fields once and reuse them across forms.
- Forms need typed custom fields with labels, help text, options, required rules, and lifecycle state.
- Select/multiselect values need option catalogs instead of free-form strings.
- Lead list/detail APIs need a predictable schema for rendering custom fields.
- Conversion from lead to deal needs an explicit mapping story.
- Reporting/search eventually needs indexes or projections for high-value custom fields.
- Raw JSON properties are hard to validate, migrate, deprecate, or audit.

## Recommended Architecture

Extend the existing CRM custom-field system instead of creating a second custom-field concept.

### Data Model

Change `CRMCustomFieldEntity` from deal-only to:

```python
class CRMCustomFieldEntity(str, Enum):
    DEAL = "deal"
    LEAD = "lead"
```

Keep storing actual lead custom values in `CRMLead.properties` for now. That is the smallest production-grade path because it reuses the existing JSONB storage already on leads and the existing custom-field definition table already used by deals.

Required schema/index migration:

- Add `lead` as a supported entity value at the model/API level.
- No table shape change is required if `entity_type` is already varchar.
- Add optional JSONB GIN index only if product needs ad hoc property filtering immediately. Do not add it speculatively.

### Services Folder Structure

Current reusable pieces:

- `src/api/routers/crm/custom_fields_handler.py`
- `src/api/schemas/crm_custom_field_models.py`
- `src/service/crm/internal/custom_field_write_service.py`
- `src/service/crm/internal/custom_field_service.py`
- `src/service/crm/deals/custom_field_service.py`

Target structure after cleanup, only if the first implementation creates real duplication:

```text
src/service/crm/
  custom_fields/
    __init__.py
    key_normalization.py
    value_coercion.py
    definition_read_service.py
    definition_write_service.py
    validation_service.py
  leads/
    custom_field_service.py
  deals/
    custom_field_service.py
  access/
    lead_forms_service.py
```

Minimal first implementation can be smaller:

- Extend `CustomFieldWriteService` to work for `CRMCustomFieldEntity.LEAD`.
- Keep deal-specific usage counting and delete blocking scoped to `DEAL`.
- Add a lead-specific value validator only if form-submission coercion cannot reuse existing helpers cleanly.
- Keep `LeadFormsService` as the form orchestration service, but delegate custom-property validation to the existing custom-field validation path.

Avoid building a separate `lead_form_custom_values` table unless we need per-submission analytics independent of leads. Current submission audit rows already preserve raw and mapped payloads.

### API Contract

Add or extend custom-field endpoints to accept `entity = "lead"`:

- `GET /users/crm/custom-fields?entity=lead`
- `POST /users/crm/custom-fields` with `"entity": "lead"`
- `PATCH /users/crm/custom-fields/{field_id}`
- `DELETE /users/crm/custom-fields/{field_id}` as soft delete

Lead form field mapping stays the same shape:

```json
{
  "external_key": "company_size",
  "label": "Company size",
  "target_type": "property",
  "target_key": "company_size",
  "data_type": "number",
  "required": false,
  "display_order": 3
}
```

New rule: if `target_type = "property"`, `target_key` must resolve to an active internal lead custom-field definition unless the org is explicitly using legacy raw-property mode.

### Compatibility

Existing forms may already have property mappings with no definition row. Do not break them on deploy.

Compatibility strategy:

1. Add `lead` custom-field support.
2. Keep existing property-mapped forms submitting even when no definition row exists.
3. Use normalized keys for generated definitions.
4. Backfill definitions from existing active `lead_form_fields` where `target_type = "property"` only through the explicit dry-run/apply maintenance script.
5. Preserve existing `CRMLead.properties` values as-is.
6. New/updated forms must use known lead custom-field definitions.

## Mapping Rules

Lead field mappings stay limited to stable columns:

- Identity: `first_name`, `last_name`, `full_name`, `email`, `phone`
- Person/company profile: `job_title`, `linkedin_url`, `company_name`, `company_domain`, `company_website`, `industry`, `country`, `region`, `city`
- Intent: `message`, `interest`, `budget_amount`, `budget_currency`
- Attribution: `utm_source`, `utm_medium`, `utm_campaign`, `utm_term`, `utm_content`

Everything else should be a lead custom field.

Recommended key rules:

- Lowercase snake_case.
- Must match `^[a-z][a-z0-9_]{1,62}$`.
- Normalize spaces and hyphens to underscores.
- Reject duplicates after normalization.
- Do not silently create two fields that normalize to the same key.

Recommended type rules:

- `text`: trim string.
- `integer`: reject booleans and fractional values.
- `number`, `currency`: parse numeric strings and reject booleans.
- `percentage`: enforce 0 through 100.
- `boolean`: accept true/false, 1/0, yes/no.
- `date`: store ISO date.
- `datetime`: store timezone-aware ISO string.
- `select`: value must be one active configured option.
- `multiselect`: every submitted value must be an active configured option.
- `formula`: read-only; never writable from forms.

## Public and Private Form Behavior After Change

Public form definition:

- Continue hiding internal `target_type` and `target_key`.
- Include browser-safe type/options/validation metadata.
- Do not expose inactive options.
- Do not expose provider-only fields unless explicitly made available to forms.

Public submission:

- Reject unknown submitted field keys.
- Reject malformed email fields, not just missing email fields.
- Enforce type-compatible mappings: for example `budget_amount` must be numeric and date fields must parse as dates.
- Reject property mappings whose target custom field is inactive/deleted.
- Reject invalid type coercion.
- Reject inactive select/multiselect options.
- Enforce total payload byte, depth, node-count, and list-length limits before recursive validation.
- Support an optional public idempotency token or form-render nonce so browser retries can be safely replayed.
- Apply bot/spam defenses before lead creation.
- Persist raw payload in `lead_form_submissions.raw_payload`.
- Persist mapped lead columns in `lead_form_submissions.mapped_payload`.
- Persist typed custom values in `CRMLead.properties`.

Private mapped submission:

- Same mapping and validation as public.
- Keep idempotency behavior unchanged.
- Replay should return the original accepted result even if the form definition changed after the first submit.

## Outliers and Edge Cases

Form lifecycle:

- Draft form cannot be loaded publicly.
- Paused/archived form cannot be submitted.
- Public ingress key state must match form active/paused state.
- Deleted fields must not be accepted.
- Updating a form increments `current_version`.
- Submission stores the form version used at submit time.

Field schema:

- Duplicate `external_key` in create/update is rejected.
- Duplicate custom keys after normalization are rejected.
- Existing deleted field keys need unique archived suffixes.
- Empty required field is rejected.
- Default value is applied before required validation.
- `ignore` fields cannot be required; requiring a value that is intentionally dropped is confusing and is rejected on create/update.
- A form with no identity mapping can be saved as draft, but active submission must still produce email, phone, or full_name.

Payload shape:

- Unknown field key is rejected.
- More than 200 submitted fields is rejected.
- More than 100 configured fields is rejected.
- Oversized scalar string is rejected.
- Total request body bytes must be capped before Pydantic/model validation.
- Nested JSON values are currently accepted for `json` fields; add depth/node/list limits if JSON custom fields remain enabled for public forms.
- Lists with many small values must not bypass scalar-length and top-level-field-count limits.
- Deeply nested objects must not trigger recursive validation failures or stack exhaustion.
- Empty string is treated as missing.
- Whitespace-only required value is rejected.

Security:

- Public form key must be active and resource-scoped to `lead_form`.
- Public CORS origin must match the form's `allowed_origins`, but Origin must not be treated as authentication because server-side callers can omit or forge it.
- Private key must be `private_api` and resource-scoped to `lead_form`.
- Private key expiry, IP allowlist, and rate limits must be enforced before org DB writes.
- Public errors must not leak tenant IDs, form IDs, lead IDs, or target mapping internals.
- Public submit response should continue hiding `lead_id`.
- Public routes need spam controls: CAPTCHA or equivalent challenge, honeypot fields, minimum-fill-time heuristics, disposable-domain controls, and block/allow lists per form or org.
- Do not log raw submitted values, bearer secrets, idempotency keys, or full request bodies.

Data integrity:

- Submission and lead creation must stay in one org transaction.
- Public master key lookup and org form lookup must remain tenant-bound.
- Form create/update must either make org form rows and master ingress keys recoverably consistent or ship with a reconciliation job that repairs orphaned rows/keys.
- Private retry races must continue resolving through unique idempotency indexes.
- A replayed private submission should not re-run validation against a changed form.
- Public duplicate submits should be deduped with a nonce/idempotency token or with a conservative server-side duplicate window.

Lead quality and routing:

- Normalize emails to lowercase and trim whitespace before storage/search.
- Normalize company domains from URLs before storage/search.
- Validate configured email fields with real syntax checks.
- Keep phone validation permissive but bounded; do not reject valid international numbers with US-only rules.
- Support owner/routing rules as explicit form settings, not hard-coded defaults.
- Define dedupe rules before assignment: exact email, email+form replay token, company domain, or configured matching policy.
- Record duplicate decisions in submission context so support can explain why a lead was merged, skipped, or created.

Privacy and compliance:

- Store consent fields separately from arbitrary properties when they drive marketing/legal behavior.
- Capture consent timestamp, consent text/version, source URL, and IP only if needed.
- Define retention for `raw_payload`, especially for sensitive custom fields.
- Support redaction or deletion workflows for lead/submission PII.
- Prevent sensitive fields such as passwords, card numbers, or secrets from being configured as normal form fields.

Conversion:

- Lead-to-deal conversion must define whether lead custom fields copy to deal custom fields.
- Recommended v1: copy only fields with an explicit lead-to-deal mapping, not all properties.
- Store unmapped lead properties on the lead only.
- If a lead custom field and deal custom field share a key, copy only if both definitions are active and compatible.

Search/reporting:

- Do not promise arbitrary custom-field filtering until indexes/projections are designed.
- For first release, support display and capture.
- Add filtering only for selected high-value fields or through a later indexed projection.

Operations:

- Track rejected submissions by error code and form key.
- Track accepted submissions by form key and source path.
- Add log context for `form_id`, `public_form_key`, `field`, and error code without logging submitted PII values.
- Keep public rate limiter fail-open behavior visible in logs.
- Track auth-stage failures separately from validation-stage and persistence-stage failures.
- Expose enough request correlation to prove whether a private submission failed before auth, after auth, or after org DB write.
- Keep `public_ingress_keys.last_used_at` semantics: it should update only after successful private authentication, so a null value remains useful incident evidence.

## What This System Should Not Have

- No silent creation of custom fields from unauthenticated public submissions.
- No reliance on CORS Origin as a security boundary.
- No unbounded JSON fields on public forms.
- No raw PII values in application logs or metrics labels.
- No plaintext private secrets after create response.
- No auto-copy of all lead custom fields into deals during conversion.
- No broad new table or service layer when extending the existing custom-field registry is enough.
- No promise of arbitrary custom-field filtering/search until indexing or projection strategy is implemented.
- No breaking change for existing property-mapped forms during the rollout.

## Implementation Slices

### Slice 0: Mapped Form Ingestion Hardening Before Wider Custom Fields

Files:

- `src/api/schemas/lead_form_models.py`
- `src/service/crm/access/lead_forms_service.py`
- `tests/service/crm/test_lead_forms_service.py`
- `tests/api/test_public_lead_form_rate_limit.py`
- `tests/api/test_private_lead_ingestion_handler.py`

Work:

- Add total payload byte, JSON depth, node-count, and list-length limits to public/private mapped form submissions.
- Keep direct `/private/v1/crm/leads` behavior unchanged; it already has payload budget enforcement in `PrivateLeadIngestionService`.
- Validate `email`, `date`, `datetime`, `number`, `boolean`, and JSON values consistently.
- Add target/data-type compatibility checks for lead-field mappings.
- Add optional public idempotency/nonce support for browser retries.
- Add spam-control hooks in form settings without forcing one provider-specific CAPTCHA dependency.
- Add PII-safe structured logs for auth, validation, and persistence failure stages.

### Slice 1: Lead Custom-Field Definitions

Files:

- `src/domain/models/organisation_tables/sync/crm/custom_fields.py`
- `src/api/schemas/crm_custom_field_models.py`
- `src/api/routers/crm/custom_fields_handler.py`
- `src/service/crm/internal/custom_field_write_service.py`
- `tests/service/crm/test_custom_field_source_isolation.py`
- New focused lead custom-field tests.

Work:

- Add `CRMCustomFieldEntity.LEAD`.
- Ensure API accepts `entity = lead`.
- Prevent deal-only usage counts and deal JSONB delete checks from running for lead fields.
- Keep existing deal behavior unchanged.
- Add tests proving deal-only existing behavior still passes and lead definitions can be created/listed.

### Slice 2: Shared Custom-Field Validation

Files:

- `src/service/crm/internal/custom_field_write_service.py`
- `src/service/crm/deals/custom_field_service.py`
- `src/service/crm/leads/custom_field_service.py`

Work:

- Reuse existing key normalization and definition validation first.
- Add lead value coercion for form submissions.
- Extract shared coercion only if keeping deal and lead validators separate starts duplicating meaningful code.
- Reject formula writes.
- Enforce select/multiselect options.
- Add tests for all data types.

### Slice 3: Lead Form Mapping Validation

Files:

- `src/service/crm/access/lead_forms_service.py`
- `tests/service/crm/test_lead_forms_service.py`

Work:

- On create/update, validate `target_type = property` against active lead custom-field definitions.
- On submit, coerce and validate custom values before writing `CRMLead.properties`.
- Keep legacy forms working through a compatibility path.
- Add tests for active/inactive/deleted fields and invalid option values.

### Slice 4: Backfill Existing Form Properties

Files:

- `scripts/maintenance/backfill_lead_form_custom_fields.py`

Work:

- Find active `lead_form_fields` with `target_type = property`.
- Create missing lead custom-field definitions using `target_key`, `label`, `data_type`, `validation`, and `display_order`.
- Default to dry-run; require `--apply` to write.
- Resolve key collisions deterministically and report skipped collisions.

### Slice 5: Documentation and API Examples

Files:

- `docs/API Documentation/deals/crm-lead-ingestion-api.md`
- `docs/API Documentation/deals/crm-custom-fields-api.md`

Work:

- Document lead custom fields.
- Document compatibility behavior.
- Add examples for public form, private mapped form, select values, and invalid option errors.

## Test Matrix

Management APIs:

- Create lead custom field for every supported writable type.
- Reject invalid key format.
- Reject duplicate key per org/entity.
- Allow same key on `deal` and `lead` as separate entity scopes.
- Soft-delete field, then allow recreation with the same key if existing uniqueness semantics allow it.
- Reject provider-field provenance mismatch.
- List only active/non-deleted fields by default.

Form create/update:

- Create form with 1 lead field and 20 property fields.
- Create form with 100 fields succeeds.
- Create form with 101 fields fails.
- Duplicate `external_key` fails.
- Property target without `target_key` fails.
- Property target with unknown lead custom field fails for new forms.
- Property target with inactive/deleted lead custom field fails.
- Lead field target outside `_LEAD_FIELD_TARGETS` fails.
- Lead field target with incompatible data type fails.
- Required `ignore` field is either rejected or explicitly documented as allowed.
- Updating form archives removed fields and increments version.
- Updating form preserves existing field IDs for unchanged external keys.
- Simulated master commit failure after org flush does not leave an unrecoverable published form.
- Reconciliation detects orphaned form rows or orphaned ingress keys.

Public definition:

- Active form returns browser-safe fields.
- Draft/paused/archived form returns not found/unavailable.
- Target mapping internals are not returned.
- Inactive select options are not returned.
- Allowed origin succeeds.
- Disallowed origin fails.
- Missing origin with non-empty allowlist fails.
- Empty allowlist allows browser/server callers.

Public submission:

- 20 custom property values succeed.
- 100 configured fields with sparse submitted values succeeds.
- 200 submitted field keys succeeds if configured.
- 201 submitted field keys fails.
- Unknown field key fails.
- Missing required field fails.
- Default value satisfies a required field when configured.
- Email/phone/full_name all absent fails.
- Oversized scalar fails with 413.
- Invalid number/boolean/date/datetime fails.
- Invalid email syntax fails for email-typed fields.
- Deep nested JSON fails before recursion exhaustion.
- Huge list under one field fails even when top-level field count is low.
- Total payload bytes over the configured cap fails.
- Select value not in active options fails.
- Multiselect with one invalid option fails.
- Formula field mapping fails.
- Browser retry with the same nonce/idempotency token does not create a second lead.
- CAPTCHA/honeypot failure blocks lead creation.
- Submission creates one `LeadFormSubmission` and one `CRMLead`.
- Public response does not include `lead_id`.
- Raw payload is preserved.
- Coerced custom properties are stored on `CRMLead.properties`.

Private mapped submission:

- Missing key id fails.
- Missing bearer secret fails.
- Wrong resource type key fails.
- Missing `Idempotency-Key` fails.
- First valid request returns 201.
- Replay with same idempotency key returns 200 and original IDs.
- Concurrent replay unique conflict rolls back and returns winner row.
- Changed form definition after first submit does not alter replay response.
- Private source type remains `api` and links form/submission IDs.
- Auth failure leaves `public_ingress_keys.last_used_at` unchanged.
- Validation failure after auth does not create submission or lead rows.
- Persistence failure rolls back both submission and lead rows.

Direct private lead API:

- Direct `/private/v1/crm/leads` remains unaffected.
- Unknown top-level fields still fail.
- `source_type = form` still fails.
- `status = converted` still fails.
- Existing payload byte, depth, node-count, list-length, and scalar-length limits still apply.
- Existing direct `properties` behavior is unchanged unless explicitly routed through lead custom-field validation in a later slice.

Agent lead tool:

- `ToolName.CREATE_LEAD` remains registered for `GeneralAgent` and `VoiceRealtimeAgent`-style tool profiles where allowed by channel.
- Agent-created leads continue using `source_type = manual` and `source_channel = chat`.
- Agent lead creation does not create `LeadFormSubmission` rows.

Conversion:

- Lead with custom fields converts without losing original lead properties.
- Explicit compatible lead-to-deal mapping copies values.
- Unmapped lead custom fields do not appear on the deal.
- Incompatible target type blocks copy or records a skipped mapping, depending on chosen product behavior.

Performance:

- Loading public form definition with 100 fields stays within acceptable latency.
- Submitting 100 fields performs bounded DB queries.
- Validation loads field definitions in one query, not one per field.
- No N+1 queries for option validation.

Observability:

- Rejected submissions log error code and field key, not submitted PII values.
- Accepted submissions emit form key/form ID/source path counters.
- Rate-limited responses include `Retry-After`.
- Redis limiter fail-open path logs warning.
- Auth failures, validation failures, dedupe skips, spam rejects, and DB persistence failures have distinct error codes/metrics.
- Request IDs let support correlate API logs with `lead_form_submissions` and `crm_leads`.
- No metric label includes email, phone, full name, message text, or raw custom-field values.

## Open Product Decisions

- Maximum fields per form: keep current 100, or raise only with UI/performance proof.
- Should admins create custom fields inside the form builder, or only in CRM settings?
- Should direct private lead API validate `properties` against lead custom-field definitions, or stay advanced/raw?
- Which custom fields should be searchable/filterable in v1?
- Should lead custom fields copy to deal custom fields automatically on matching keys, or only via explicit mapping?
- What anti-spam provider or in-house challenge should public forms use?
- What retention period should apply to `lead_form_submissions.raw_payload`?
- Should public forms support a first-party render nonce, caller-provided idempotency key, or both?
- What dedupe policy should be default: create every lead, merge exact email, or flag possible duplicates?

## Recommended First Build

Build the smallest production-grade slice:

1. Harden mapped form ingestion payload limits and typed validation.
2. Add public retry/idempotency support and spam-control hooks.
3. Add `CRMCustomFieldEntity.LEAD`.
4. Reuse/extract existing deal custom-field validation only where needed.
5. Validate new/updated form property mappings against active lead custom fields.
6. Coerce submitted property values through the lead custom-field validator.
7. Backfill definitions from existing property-mapped forms through an explicit maintenance script, not an Alembic data migration.
8. Leave storage in `CRMLead.properties`.

This gives teams many custom values without creating a parallel schema system, avoids a new dependency, keeps existing form submissions compatible, and leaves search/reporting as an explicit later decision instead of smuggling it into the capture feature. It also closes the production loopholes that matter most before increasing the amount of untrusted data accepted from public forms.
