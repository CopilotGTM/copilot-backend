# CRM Foundation Production Readiness Review

Date: 2026-07-03

Scope: internal CRM pipelines, custom columns, and schema metadata foundations across backend and frontend.

## Executive summary

The current implementation is better than a raw table. It already has tenant-scoped pipeline and custom-field APIs, internal CRM write guards, rate limits, immutable custom-field keys, soft-delete protection, typed custom-field coercion on deal writes, frontend settings pages, and deal-detail metadata rendering.

It is still not at the production level where non-technical CRM admins can run it safely for a year without engineering help. The biggest gaps are not visual polish. They are backend-owned invariants, richer metadata, self-serve lifecycle tooling, audit/history, and bulk/reorder operations.

Recommended first cut: keep the current surfaces, but harden them into a metadata-backed CRM foundation rather than adding broad new CRM modules.

## Current implementation

### Backend

- `src/api/routers/crm/custom_fields_handler.py`
  - Lists, creates, updates, archives, deletes, and formula-validates custom field definitions.
  - Uses `require_crm_write_scope(...)` and read/write rate limits.

- `src/domain/models/organisation_tables/sync/crm/custom_fields.py`
  - Stores `CRMCustomFieldDefinition` rows with entity, source, integration/provider provenance, key, label, data type, required flag, active/deleted flags, display order, description, options, validation, formula, raw provider payload, and source timestamps.
  - Already supports internal and provider-discovered schema rows.

- `src/service/crm/internal/custom_field_write_service.py`
  - Enforces immutable keys/types on the settings path.
  - Blocks deletion when active deals still contain the custom field key in `CRMDeal.properties`.
  - Validates select/multiselect option presence and formula dependencies.

- `src/service/crm/deals/custom_field_service.py`
  - Validates internal deal write `custom_properties` against active field definitions.
  - Coerces text, integer, number, currency, percentage, boolean, date, datetime, select, and multiselect values.
  - Rejects unknown custom fields and formula writes.

- `src/api/routers/crm/deals_handler.py`
  - Loads active custom field definitions for deal list/detail responses.
  - Returns frontend-friendly custom column metadata for detail/edit rendering.

- `src/api/routers/crm/pipelines_handler.py`
  - Lists pipelines/stages and exposes create/update/delete endpoints for internal CRM pipelines.
  - Uses internal CRM write guards and rate limits.

- `src/service/crm/internal/pipeline_write_service.py`
  - Scopes writes to the internal CRM integration.
  - Propagates pipeline/stage name and label changes to deal snapshot columns.
  - Blocks deleting default pipelines and deleting pipelines/stages referenced by deals.

### Frontend

- `settings/custom-columns`
  - Searchable table with label, key, type, required flag, archived toggle, edit/archive/delete actions.
  - Create/edit dialog supports key auto-generation, immutable key/type on edit, description, required flag, select/multiselect options, and data-type badges.

- `settings/pipelines`
  - Master-detail layout for pipelines.
  - Supports create, rename, promote default, delete, add stage, rename stage, update probability, drag reorder, and delete stage.
  - URL-selected pipeline state is shareable via `?id=...`.

- Deal edit custom fields
  - Renders typed inputs from backend metadata for boolean, currency, date, datetime, integer, number, text, and read-only formula fields.

## Production gaps

### Additional loopholes found in this review

These are the places where the current plan could still leave production holes if implementation starts too literally:

- Source ownership must be explicit. Internal custom fields are writable by our deal write path, while provider-discovered fields are mirrored schema metadata. A unified schema endpoint must mark provider fields as read-only/non-editable for internal writes unless a separate provider-write path exists.
- Required-field validation must live in `DealWriteService`/`DealCustomFieldWriteService`, not the route or UI. Deals can be created from API routes, lead conversion, bots, and agent tools, so service-level validation is the smallest shared fix.
- Pipeline invariants must be source-aware. `PipelineWriteService` owns internal settings writes, but CSV import and provider pipeline sync also create pipeline/stage rows. Internal pipelines can reject invalid shapes; provider/import ingestion should normalize and flag unusable shapes without breaking sync.
- Stage uniqueness needs a database backstop. The model currently indexes stage names/order, but stage names/keys are not uniquely constrained per pipeline. Service checks alone can still race under concurrent settings writes.
- Settings audit should reuse the existing CRM mutation event recorder shape where possible, but settings events need nullable object IDs or a settings-specific object reference because they are not always deal/contact/account mutations.
- The schema endpoint should be the read contract, not just a convenience API. It needs compatibility semantics for existing deal-detail `custom_column_definitions` until frontend and agent consumers are migrated.
- Reorder endpoints need concurrency checks from the first implementation. Atomic reorder without an `updated_at`/version precondition can still overwrite another admin's order safely but silently.

### P0: Backend invariants must own what the UI currently assumes

Some rules are enforced only in the frontend. That is not production-safe because agents, imports, API clients, retries, and future mobile/voice surfaces can bypass the page.

Required changes:

- Enforce pipeline shape in `PipelineWriteService`:
  - A usable pipeline must have at least one `open`, one `won`, and one `lost` stage before it can become default or accept deals.
  - Deleting the last stage of a state must be blocked in backend, not only in React.
  - Creating a pipeline with zero stages should keep the pipeline inactive/unusable until stages are configured.
  - Apply this as an internal-CRM settings invariant. Provider/import pipeline ingestion should keep syncing but mark unusable shapes rather than rejecting external truth.

- Enforce stage uniqueness and key stability:
  - Prevent duplicate stage names/keys within a pipeline.
  - Avoid changing `stage_key` on rename if any downstream automation/filter stores the key. If key must change, expose that as a migration action.
  - Add a partial unique database constraint for active, non-deleted internal stages per pipeline to close concurrent-write races.

- Enforce required custom fields on deal create/update:
  - The `required` flag exists, but deal write validation currently validates only fields the caller supplied.
  - Required active fields should be enforced on create.
  - On update, reject clearing required fields and enforce required fields before terminal lifecycle moves such as close/won/lost.
  - Enforce only writable internal fields on internal deal writes; provider schema rows must not accidentally become required internal inputs.

- Enforce select/multiselect option membership:
  - Field definition creation requires `options.values`, but deal writes currently coerce select values without checking membership.
  - Add option validation to prevent long-term data drift.

### P0: Add schema metadata for quick access, not just storage

The schema table is a registry, but the app still lacks a first-class metadata contract that the UI, agents, imports, and filters can all consume.

Required changes:

- Add a CRM schema endpoint, for example `GET /users/crm/schema?entity=deal`, returning:
  - Standard fields and custom fields in one ordered payload.
  - Field key, label, type, required, read-only, nullable, editable, sortable, filterable, searchable.
  - Input control metadata: select values, currency defaults, date/datetime mode, min/max/pattern.
  - Lifecycle metadata: source, active/archived/deleted state, usage count, created/updated timestamps.
  - Field ownership: system/internal/provider/custom.
  - Write semantics: writable source, read-only reason, and whether the field participates in internal create/update validation.

- Use this endpoint for:
  - Deals table column picker.
  - Deal create/edit drawers.
  - CSV import mapping.
  - Agent tool schemas and voice/chat confirmation cards.
  - Filter builders and saved views.

- Keep `CRMCustomFieldDefinition` as storage, but stop making each consumer reconstruct schema rules differently.
- Keep the existing deal-detail custom-field metadata response during migration, but make it a compatibility projection of the same schema service.

### P0: Reorder and bulk operations

The UI allows drag reorder for stages, but saves each changed stage as separate requests. Custom columns have `display_order`, but the settings UI does not expose ordering.

Required changes:

- Add `PATCH /pipelines/{id}/stages/reorder` to update stage order atomically.
- Add `PATCH /custom-fields/reorder` to update custom column order atomically.
- Make ordering dense and deterministic.
- Add optimistic concurrency or `updated_at` precondition for settings writes and reorder requests so two admins do not silently overwrite each other.

### P1: Admin lifecycle and data migration tools

Production CRM admins need safe ways to evolve schema and pipelines after data exists.

Required changes:

- Custom columns:
  - Show usage count per column.
  - Add archive vs delete explanation in UI.
  - Add a "clear values from matching deals" or migration workflow before delete.
  - Add duplicate/copy column.
  - Add option rename/merge/deprecate for select fields. Removing an option should not leave invisible invalid values forever.
  - Add field-level default values.

- Pipelines:
  - Show deal counts per pipeline/stage.
  - Add "move deals before deleting stage/pipeline" workflow.
  - Add stage probability presets and warnings for invalid forecasting setup.
  - Add clone pipeline.
  - Add active/inactive pipeline state, separate from soft delete.

### P1: Auditability and rollback

Settings changes affect revenue workflows. They need an audit trail.

Required changes:

- Emit mutation events for:
  - Custom field create/update/archive/delete/reorder.
  - Pipeline create/update/default/delete/reorder.
  - Stage create/update/delete/reorder.

- Store actor, timestamp, old value, new value, and source surface.
- Reuse the existing CRM mutation event writer where it fits, but include an object reference that can represent settings objects such as field IDs, pipeline IDs, and stage IDs.
- Show a lightweight "last changed by" line in settings.
- Consider restore for recently archived columns and inactive pipelines/stages.

### P1: Frontend UX improvements

The current UI is clean and usable, but it is sparse for a production CRM settings area.

Custom columns:

- Add an entity/type filter once more entities are supported.
- Add sort by label/key/type/required/usage/updated.
- Add column order drag handles.
- Show description, options count, usage count, and archived state more clearly.
- Allow creating formula fields only after formula evaluation is implemented end-to-end. Until then, hiding formula from create is correct.
- Add empty/error states that explain blocked deletes using actual counts and next actions.

Pipelines:

- Show counts per pipeline/stage in the left pane and rows.
- Make default pipeline state more explicit and explain default behavior.
- Add dirty-state protection when switching pipelines with unsaved stage edits.
- Add bulk save/reorder status instead of multiple independent stage update calls.
- Make stage grouping less visually flat: open/won/lost groups should communicate lifecycle and forecast impact.

Deal edit custom fields:

- Render select/multiselect fields with select controls, not generic text inputs.
- Surface required custom fields in create/edit forms.
- Show field descriptions/help text where present.
- Keep formula fields read-only.

### P1: Multi-entity schema support

The model currently supports only `CRMCustomFieldEntity.DEAL`. For a production CRM foundation, schema metadata should cover at least:

- Deals
- Accounts/companies
- Contacts
- Tasks/activities if user-created fields are expected there

Do not build all UI at once. First add the schema contract so extending entities does not require another API design pass.

### P2: Search, filtering, and reporting readiness

Custom values live inside `CRMDeal.properties` JSONB. That is acceptable for first cut, but not enough for durable reporting if customers rely on custom columns heavily.

Required changes:

- Add documented indexing strategy for custom field filters:
  - GIN index on JSONB for existence/value checks, or generated/indexed columns for high-value fields.
  - Decide when a custom field becomes reportable/searchable.

- Add field metadata flags:
  - `filterable`
  - `sortable`
  - `searchable`
  - `analytics_enabled`

- Add typed query helpers so table filters do not hand-roll JSONB comparisons.

## First-cut production backlog

### Backend first

1. Add unified CRM schema metadata endpoint for deals.
2. Enforce required custom fields on deal create/update.
3. Enforce select/multiselect option membership.
4. Move last-stage-of-state and usable-pipeline invariants into `PipelineWriteService`.
5. Add atomic reorder endpoints for stages and custom fields.
6. Add usage-count queries for custom fields, pipelines, and stages.
7. Add mutation/audit events for CRM settings changes.

### Frontend second

1. Drive deal create/edit/table custom columns from the unified schema endpoint.
2. Add custom column reorder and usage count display.
3. Add stage/pipeline usage counts and move-before-delete workflows.
4. Add dirty-state protection in pipeline editor.
5. Render select/multiselect fields as proper controls in deal edit.
6. Improve blocked-delete dialogs with direct next actions.

### Later, not first cut

- Full formula builder and formula evaluation engine.
- Multi-entity custom-field settings UI.
- Field-level permissions.
- Advanced automation triggers based on schema changes.
- End-user layout builder for each object detail page.

## Readiness judgment

Current state: production-capable MVP for a controlled rollout where engineering can assist admins.

Not yet ready: one-year self-serve CRM foundation for arbitrary customers without engineering help.

Minimum bar to call this production-level:

- Backend owns all CRM settings invariants.
- Schema metadata is served through one canonical endpoint.
- Required/options validation applies during real deal writes.
- Admins can see usage counts and safely migrate before deleting.
- Reorder operations are atomic.
- Settings changes are auditable.

That is the smallest durable first cut. It avoids building a full Salesforce-style admin platform, but closes the gaps that would create data drift or support tickets after customers start configuring their CRM themselves.
