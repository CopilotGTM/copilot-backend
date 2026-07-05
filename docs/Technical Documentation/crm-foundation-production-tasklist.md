# CRM Foundation Production Tasklist

Date: 2026-07-03

Source review: `docs/Technical Documentation/crm-foundation-production-readiness-review.md`

Execution rule: complete one task at a time, in order. Each task should leave behind focused backend/frontend tests or a small verification note before moving to the next task.

Review update: the first development pass should harden shared backend services and contracts, not individual screens. The main loopholes to close before development are source ownership, non-UI write paths, provider/import pipeline bypasses, concurrent settings writes, and database backstops for uniqueness.

## Phase 1: Backend invariants

### Task 1: Enforce usable pipeline shape in backend (completed)

Goal: pipeline correctness must not depend on the React settings page.

Scope:

- Update `PipelineWriteService`.
- A pipeline that can become default or accept deals must have at least one `open`, one `won`, and one `lost` stage.
- Backend must block deleting the last stage for any required stage state.
- Keep the invariant source-aware:
  - internal settings writes can reject invalid shapes
  - CSV import/provider sync should keep ingesting external truth and mark unusable shapes instead of failing sync
- Empty internal pipelines are created inactive/unusable until stages are configured.

Done when:

- API clients cannot create/promote/use an invalid pipeline shape.
- Existing frontend last-stage delete guard has a matching backend guard.
- Deal create/move cannot use an unusable internal pipeline.
- Tests cover create, promote/default, stage delete, invalid shape errors, and provider/import pipeline behavior.

Implemented 2026-07-03:

- Shared pipeline-shape validation requires at least one `open`, one `won`, and one `lost` stage.
- Empty/incomplete internal pipelines are stored as inactive/unusable drafts.
- Default promotion rejects unusable internal pipelines.
- Stage delete rejects removal of the last required state.
- Deal creation rejects unusable internal pipelines, covering API, agent, bot, and lead-conversion callers that use `DealWriteService`.
- Deal stage moves reject unusable internal pipelines.
- CSV import and provider-synced pipelines continue to be created, but incomplete shapes are marked inactive/unusable instead of rejected.
- API docs now describe draft pipeline behavior and unusable-pipeline conflicts.

Left for later tasks:

- Deeper provider usability surfacing belongs with schema/settings metadata work.
- Atomic reorder and concurrency preconditions are tracked in Task 8-10.

### Task 2: Enforce stage uniqueness and stage key stability (completed)

Goal: prevent duplicate or drifting stage identifiers inside a pipeline.

Scope:

- Prevent duplicate stage names/keys within the same pipeline.
- Make `stage_key` immutable after create for internal CRM stages.
- Rename should update display name/label and deal snapshots, but not rewrite `stage_key`.
- Add a partial unique database constraint/index for active, non-deleted internal stage keys per pipeline.
- Add a service-level duplicate-name check for admin ergonomics before the DB constraint fires.

Done when:

- Done: duplicate initial stage/create/rename paths return deterministic 409s before commit.
- Done: rename updates display name/label and deal snapshots while keeping `stage_key` stable.
- Done: active non-deleted stage keys are protected by a partial unique database index.
- Done: focused service and migration contract tests cover the behavior.

### Task 3: Enforce required custom fields on deal writes (completed)

Goal: the `required` flag must affect real CRM writes, not only metadata display.

Scope:

- Update `DealWriteService`/`DealCustomFieldWriteService`, not individual routes.
- Required active internal custom fields must be present on create.
- Update semantics for the first cut:
  - reject clearing a required field through `custom_properties`
  - validate required fields before close/won/lost lifecycle transitions
  - do not block unrelated partial edits that do not touch required custom fields
- Ignore provider-discovered schema rows for internal required-field enforcement unless a provider-write path is explicitly added.

Done when:

- Done: missing required custom fields fail with a clear 400.
- Done: clearing a required custom field fails with a clear 400.
- Done: closing/moving to terminal states fails if required internal fields are missing.
- Done: existing valid create/update flows still pass.
- Done: tests cover required text, required typed fields, archived fields, provider fields, and formula fields.
- Done: validation lives in `DealWriteService`/`DealCustomFieldWriteService`, so API route, agent/bot, and lead-conversion callers hit the same service-level rule.

Implemented 2026-07-05:

- Deal creation validates required active internal custom fields before internal CRM setup/pipeline selection.
- Deal updates reject explicit clearing of required active internal custom fields through `custom_properties`.
- Deal close and terminal stage moves validate required active internal custom fields before stage/lifecycle mutation.
- Unrelated partial edits remain allowed when they do not touch required custom fields.
- Provider-discovered, inactive/deleted, and formula fields are ignored for internal required-field enforcement.

### Task 4: Enforce select and multiselect option membership (completed)

Goal: prevent invalid option values from entering `CRMDeal.properties`.

Scope:

- Extend `DealCustomFieldWriteService`.
- For `select`, the value must match an active configured option.
- For `multiselect`, every submitted value must match an active configured option.
- First cut: validate by exact stored option value.
- Preserve room for stable option ids in the schema response so option rename/merge can be added without another contract redesign.

Done when:

- Done: invalid select/multiselect values fail before write.
- Done: valid values are still coerced predictably.
- Done: tests cover single select, multiselect, empty values, and option removal behavior.

Implemented 2026-07-05:

- `DealCustomFieldWriteService` validates select and multiselect values against active `options.values` before returning values for `CRMDeal.properties`.
- Validation uses the stored option `value` exactly after existing write-time trimming/coercion.
- Empty/null option values remain allowed for optional fields; required-field validation continues to decide whether they are acceptable.
- Removed options marked with `active: false` or `is_active: false` are rejected for new writes.
- Custom field schema responses continue passing option metadata through unchanged, preserving room for future stable option ids.

### Phase 1 status: complete

Verified 2026-07-05:

- Task 1: pipeline usability shape is enforced in backend services, while provider/import writers mark invalid external shapes inactive/unusable instead of failing sync.
- Task 2: internal stage names/keys are service-checked, database-backed, and `stage_key` remains stable on rename.
- Task 3: active internal required custom fields are enforced on create, clear attempts, and terminal lifecycle transitions.
- Task 4: select and multiselect values are validated against active configured option values before `CRMDeal.properties` writes.
- Phase 2 can start with Task 5, the unified deal schema metadata endpoint.

## Phase 2: Schema metadata

### Task 5: Add unified CRM schema metadata endpoint for deals (completed)

Goal: one canonical API should describe deal fields for UI, imports, agents, and filters.

Scope:

- Add a shared backend schema service first; the route should be a thin wrapper.
- Add a backend endpoint such as `GET /users/crm/schema?entity=deal`.
- Return standard deal fields and active custom fields in one ordered payload.
- Include:
  - key, label, type
  - required, read-only, nullable, editable
  - sortable, filterable, searchable
  - input control metadata
  - source/ownership metadata
  - lifecycle state
  - writable/editable semantics and read-only reason
  - internal-write validation flags

Done when:

- Done: deal schema can be fetched without loading a specific deal.
- Done: current custom field definitions are represented without losing existing metadata.
- Done: internal fields and provider fields are distinguishable, and provider fields are not presented as internal writable inputs.
- Done: existing deal-detail custom-column metadata is explicitly kept as a temporary compatibility projection.
- Done: tests lock the response shape and ordering.

Implemented 2026-07-05:

- Added `DealSchemaService` as the shared backend schema source for deal metadata.
- Added `GET /users/crm/schema?entity=deal` as a thin route wrapper.
- Standard deal fields are returned first, followed by active custom field definitions.
- Internal custom fields expose `custom_columns.<key>` write paths and internal-write validation flags.
- Provider-mirrored fields remain visible for metadata, filtering/import planning, and agent context, but are marked read-only for internal CRM writes.
- Select and multiselect option metadata is preserved, including future stable option ids.
- Deal detail `custom_column_definitions` remains a compatibility projection until Task 6 moves consumers to the schema endpoint.
- Standard schema fields explicitly return null custom/provider metadata (`field_id`, `integration_id`, `provider_name`, `provider_field_key`) so the response shape stays stable for clients and static analysis.

### Task 6: Move deal create/edit metadata consumers to schema endpoint

Goal: stop each frontend/backend consumer from reconstructing schema rules differently.

Scope:

- Use the schema endpoint for deal create/edit custom fields.
- Keep existing deal-detail custom column payload temporarily if needed for compatibility.
- Avoid breaking current deal detail rendering.

Done when:

- Deal create/edit forms get custom field metadata from the schema endpoint.
- Existing typed custom field inputs still render.
- Tests cover text, number, currency, date, datetime, boolean, select, multiselect, and formula/read-only fields.

Implemented backend slice 2026-07-05:

- Deal detail `custom_column_definitions` is now built by `DealSchemaService`, not by a route-local duplicate mapper.
- The compatibility projection still includes typed edit metadata for existing detail consumers.
- Active internal custom-field null filling now uses the same schema service source.
- Remaining work: migrate frontend create/edit consumers to `GET /users/crm/schema?entity=deal`; that consumer code is outside this backend checkout.

### Task 7: Use schema metadata for table columns, import mapping, and agent surfaces

Goal: make the schema endpoint the shared contract beyond the edit drawer.

Scope:

- Wire deals table column picker to schema metadata.
- Wire CSV import mapping to schema metadata.
- Audit agent/voice/chat create-deal schemas and route them through the same field metadata where practical.

Done when:

- One field definition change is visible consistently in table, import, and create/edit surfaces.
- No surface relies on a hard-coded custom field list.
- Focused tests or snapshots cover the key consumers.

Implemented backend slice 2026-07-05:

- Agent create-deal now accepts `custom_columns` and passes them through the existing `DealWriteService` custom-field validation path.
- Agent create-deal checks `DealSchemaService` before direct creation and returns `needs_input` with `required_custom_fields` when required writable custom fields are missing.
- Voice realtime `create_deal` schema exposes `custom_columns`, so collected required custom field answers can be sent back through the same agent tool path.
- CSV import is currently disabled/reserved in the backend API, so no import-mapping implementation is needed in this phase; the rebuild should consume `GET /users/crm/schema?entity=deal`.
- Remaining work: frontend table column picker and frontend create/edit consumers still need to consume `GET /users/crm/schema?entity=deal`.

## Phase 3: Reorder and bulk operations

### Task 8: Add atomic stage reorder endpoint (backend slice completed)

Goal: stage ordering should save as one operation, not many independent patch calls.

Scope:

- Add `PATCH /pipelines/{pipeline_id}/stages/reorder`.
- Accept ordered stage ids and update `display_order` transactionally.
- Validate every stage belongs to the pipeline and org.
- Require an `updated_at`/version precondition so reorder does not silently overwrite another admin.

Done when:

- Done: reorder is atomic.
- Done: partial/foreign stage ids are rejected.
- Done: stale reorder requests return a deterministic conflict.
- Pipeline settings frontend uses the bulk endpoint.

Implemented backend slice 2026-07-05:

- Added `PATCH /users/crm/pipelines/{pipeline_id}/stages/reorder`.
- Request uses a complete ordered `stage_ids` list plus `if_updated_at` from `PipelineItem.updated_at`.
- Service locks the pipeline row, rejects stale versions with `409`, rejects duplicate ids with `400`, and rejects missing/foreign/deleted stage ids with `409`.
- Stage `display_order` values are updated in one transaction and normalized to `1..n`.
- Stage create/delete and single-stage `display_order` updates now bump the parent pipeline `updated_at`, so bulk reorder cannot silently overwrite those order changes.
- Remaining work: pipeline settings frontend must switch drag/drop saves to the bulk endpoint.

### Task 9: Add atomic custom column reorder endpoint (backend slice completed)

Goal: admins should control custom column order safely.

Scope:

- Add `PATCH /custom-fields/reorder`.
- Accept ordered field ids and update `display_order` transactionally.
- Validate every field belongs to the current org/entity/source.
- Require an `updated_at`/version precondition so reorder does not silently overwrite another admin.

Done when:

- Done: reorder is atomic.
- Custom columns settings UI exposes drag/order controls.
- Done: stale reorder requests return a deterministic conflict.
- Done: deal table/detail ordering follows the saved order because existing schema/detail/list metadata already sorts by `display_order`.

Implemented backend slice 2026-07-05:

- Added `PATCH /users/crm/custom-fields/reorder`.
- Request uses a complete ordered `field_ids` list for one entity plus `if_updated_at`, the max `updated_at` from the loaded custom-field list.
- Service row-locks current non-deleted internal custom fields for the entity, rejects stale versions with `409`, rejects duplicate ids with `400`, and rejects missing/foreign/provider/deleted ids with `409`.
- Custom column `display_order` values are updated in one transaction and normalized to `1..n`.
- Remaining work: custom columns settings frontend must expose drag/order controls and save through the bulk endpoint.

### Task 10: Add optimistic concurrency for settings writes (backend slice completed)

Goal: two admins should not silently overwrite each other.

Scope:

- Add `updated_at` precondition support for custom field, pipeline, and stage updates.
- Return a clear conflict when the row changed after the client loaded it.
- Use request-body precondition fields such as `if_updated_at` so frontend mutations and tests stay explicit.
- Reuse the same conflict contract as reorder endpoints.

Done when:

- Done: settings writes require `if_updated_at`; HTTP callers missing it fail schema validation, defensive service guards reject non-HTTP callers, and stale rows return `409`.
- Frontend shows a useful reload/retry message.
- Done: tests cover stale custom field, pipeline, and stage updates.

Implemented backend slice 2026-07-05:

- `PATCH /users/crm/custom-fields/{field_id}` requires `if_updated_at` and returns `409` when the custom column row changed after the client loaded it.
- `PATCH /users/crm/pipelines/{pipeline_id}` requires `if_updated_at` and returns `409` when the pipeline row changed after the client loaded it.
- `PATCH /users/crm/pipelines/{pipeline_id}/stages/{stage_id}` requires `if_updated_at` and returns `409` when the stage row changed after the client loaded it.
- Successful settings updates bump the touched row's `updated_at`; stage updates also bump the parent pipeline version so bulk reorder cannot silently overwrite stage settings changes.
- Remaining work: settings frontend should send `if_updated_at` on these PATCH calls and show a refresh/retry message on `409`.

## Phase 4: Admin lifecycle and migration tools

### Task 11: Add usage counts for custom columns, pipelines, and stages (backend slice completed)

Goal: admins must see blast radius before changing schema or pipelines.

Scope:

- Add usage count queries for:
  - custom field values present on non-deleted deals
  - deals per pipeline
  - deals per stage
- Include counts in settings responses or a dedicated usage endpoint.

Done when:

- Done: backend settings responses expose usage counts without N+1 count calls.
- Delete dialogs use real counts from backend.
- Done: focused tests cover zero, one, and many usage cases.

Implemented backend slice 2026-07-05:

- `GET /users/crm/custom-fields?entity=deal` returns `usage_count`, the number of non-deleted deals whose `properties` JSONB contains the custom field key.
- `GET /users/crm/pipelines` returns `deal_count` for each pipeline.
- `GET /users/crm/pipelines?include_stages=true` and `GET /users/crm/pipelines/{pipeline_id}/stages` return `deal_count` for each stage.
- Pipeline list stage loading now uses a bulk stage query when `include_stages=true`, so counts do not add per-pipeline count calls.
- Remaining work: frontend settings tables and delete dialogs should display these counts and use them in blocked-delete copy/actions.

### Task 12: Add safe custom column delete/migration workflow (backend clear-values slice completed)

Goal: admins need a path to delete or retire fields that already contain data.

Scope:

- Keep hard delete blocked while values exist.
- Add a supported way to clear values from matching deals or migrate values to another field.
- Make archive/delete semantics explicit in API and UI.

Done when:

- Done: a used custom field can be archived safely with `PATCH /users/crm/custom-fields/{field_id}` and `is_active=false`.
- Done: an archived used custom field can be cleared and then deleted.
- Mutation events capture the field lifecycle change.

Implemented backend clear-values slice 2026-07-05:

- Added `POST /users/crm/custom-fields/{field_id}/clear-values`.
- The endpoint removes the archived field key from `CRMDeal.properties` for all non-deleted deals in the organisation.
- Clearing is rejected while the field is still active, forcing the safe flow: archive, clear values, delete.
- Clearing is rejected for predefined fields.
- Existing delete guard stays strict and still blocks deletion until no non-deleted deals carry the field key.
- Remaining work: field-to-field migration is deferred until there is a type-compatibility and conflict policy; settings mutation events are tracked in Phase 5 Task 16.

### Task 13: Add select option lifecycle management (backend option-shape hardening completed)

Goal: select fields should not accumulate invalid or invisible values.

Scope:

- Support option rename.
- Support option merge/deprecate.
- Introduce stable option ids before rename/merge so historical values remain understandable.
- Update validation to respect active/deprecated option state.

Done when:

- Done: backend rejects empty or duplicate select/multiselect option values before saving definitions.
- Done: new writes cannot use inactive option values.
- Tests cover rename, merge, delete/deprecate, and existing data display.

Implemented backend option-shape slice 2026-07-05:

- Select and multiselect definitions now require every `options.values` entry to have a non-empty `value`.
- Option values must be unique after trimming, whether supplied as strings or option objects.
- Existing write-time validation already rejects option objects marked `active: false` or `is_active: false` for new deal writes.
- Existing schema/settings responses preserve option object metadata, including caller-supplied option ids and labels.
- Remaining work: dedicated rename/merge/deprecate workflows and stable option-id generation/storage policy are deferred until product defines how historical stored values should migrate or display.

### Task 14: Add pipeline and stage move-before-delete workflow (stage backend slice completed)

Goal: admins should be able to remove old stages/pipelines without manual DB help.

Scope:

- Add backend operation to move deals from one stage/pipeline to another valid destination.
- Preserve stage history and mutation events.
- Update delete dialogs to offer move-first action.

Done when:

- Done: a stage with deals can be retired by moving deals to another stage first.
- Done: moved deals land in the selected valid destination stage.
- Done: stage history and snapshots remain correct because the batch path delegates each deal to `DealWriteService.move_stage`.

Implemented stage backend slice 2026-07-05:

- Added `POST /users/crm/pipelines/{pipeline_id}/stages/{stage_id}/move-deals`.
- Added `DealWriteService.move_stage_deals(source_stage_id, destination_stage_id, pipeline_id=...)`.
- Source and destination stages must be different and must belong to the same pipeline.
- Each deal is moved through the existing single-deal stage writer, preserving stage history, lifecycle side effects, projections, and mutation events.
- Remaining work: full pipeline-to-pipeline move-before-delete is deferred because cross-pipeline moves need explicit lifecycle and destination-stage mapping policy.

### Task 15: Add clone/default/preset helpers for pipelines (backend clone slice completed)

Goal: reduce setup friction for admins creating production pipelines.

Scope:

- Clone an existing pipeline with stages.
- Add default stage probability presets.
- Add warnings for forecasting-invalid stage probabilities.

Done when:

- Done: a user can create a new pipeline quickly from an existing internal pipeline's stage shape.
- Done: won/lost stage probability presets are covered by a regression test on the shared stage builder.
- Done: cloned pipelines reuse the existing create path, so name uniqueness, default promotion, active/inactive calculation, and required lifecycle-state checks stay centralized.

Implemented backend slice 2026-07-05:

- Added `POST /users/crm/pipelines/{pipeline_id}/clone`.
- Added `PipelineWriteService.clone_pipeline(...)`, which copies stage name, label, lifecycle state, probability, and display order into a normal `CreatePipelineRequest`.
- Added `ClonePipelineRequest` to the pipeline write schema and API documentation.
- Remaining work: forecast-invalid probability warnings are deferred until product defines whether invalid setup should warn or block and what thresholds count as invalid.

## Phase 5: Auditability and rollback

### Task 16: Emit CRM settings mutation events (backend completed)

Goal: settings changes that affect revenue workflows must be auditable.

Scope:

- Emit events for custom field create/update/archive/delete/reorder.
- Emit events for pipeline create/update/default/delete/reorder.
- Emit events for stage create/update/delete/reorder.
- Store actor, timestamp, old value, new value, and source surface.
- Reuse the existing CRM mutation event writer where practical, but support settings object references (`field_id`, `pipeline_id`, `stage_id`) without pretending every event belongs to a deal.

Done when:

- Done: custom field create/update/archive/delete/reorder/restore writes CRM settings mutation events.
- Done: pipeline create/update/default/delete and stage create/update/delete/reorder writes CRM settings mutation events.
- Done: event payloads include old/new values, actor, source, timestamp, entity type, and entity id without setting `deal_id`.
- Done: settings events are tenant-scoped through the existing `crm_mutation_events` ledger.
- Done: settings events can be queried back through `CRMMutationEventService.latest_events_by_entity(...)`.

Implemented backend slice 2026-07-05:

- Expanded the shared CRM mutation contract to support `custom_field`, `pipeline`, and `stage` entity types.
- Reused `CRMMutationEventService` instead of introducing a second settings audit table.
- Added tests for settings entity events, old/new payloads, and tenant-scoped latest-event lookup.

### Task 17: Show last-changed metadata in settings (backend completed)

Goal: admins should know who changed a setting and when.

Scope:

- Expose recent mutation metadata in settings responses or a small audit endpoint.
- Show lightweight "last changed by" details in custom column and pipeline settings.

Done when:

- Done: custom field settings responses include optional `last_changed` metadata.
- Done: pipeline and stage settings responses include optional `last_changed` metadata.
- Done: empty/no-history state is represented as `last_changed: null`.
- Done: tests cover API response helper shape.

Implemented backend slice 2026-07-05:

- Added `CRMSettingsLastChanged` response metadata.
- `GET /users/crm/custom-fields` attaches the newest `custom_field` audit event per field.
- `GET /users/crm/pipelines` and `GET /users/crm/pipelines/{pipeline_id}/stages` attach the newest pipeline/stage audit event.

### Task 18: Add restore for archived fields and inactive pipeline objects (custom-field backend completed)

Goal: common admin mistakes should be recoverable.

Scope:

- Support restore for archived custom columns.
- If pipeline/stage inactive state is introduced, support restore/reactivation.
- Keep hard-deleted or data-migrated states explicit.

Done when:

- Done: archived custom fields can be restored with `POST /users/crm/custom-fields/{field_id}/restore`.
- Done: restored custom fields become active again and reappear in schema/deal surfaces on refetch.
- Done: restore is guarded by `if_updated_at` and rejects stale/already-active rows.
- Done: restore emits a settings mutation event with old/new `is_active` values.

Implemented backend slice 2026-07-05:

- Added `RestoreCustomFieldRequest`.
- Added `CustomFieldWriteService.restore_field(...)`.
- Added API documentation for restore and last-changed settings metadata.
- Remaining work: inactive pipeline/stage restore is deferred because pipeline `is_active` is derived from required lifecycle shape and stages are soft-deleted, not archived as a reversible user state.

## Phase 6: Frontend production UX

Frontend work should now consume the backend contracts added in Phases 1-5 instead of recreating rules in the UI:

- `GET /users/crm/schema?entity=deal` is the source of truth for deal create/edit custom field metadata.
- `GET /users/crm/custom-fields?entity=deal&include_inactive=true` returns `usage_count`, `last_changed`, archived state, and the rows needed for settings.
- Custom field update, archive, restore, and reorder writes are guarded by `if_updated_at`.
- `GET /users/crm/pipelines?include_stages=true` returns pipeline/stage `deal_count`, `last_changed`, `is_active`, and stage lifecycle state.
- Deal creation pickers should use `GET /users/crm/pipelines?include_stages=true&active_only=true`.
- Pipeline/stage update and stage reorder writes are guarded by `if_updated_at`.
- CSV import remains disabled; frontend should not show import reconnect/SSE flows while `active_import_job` is always `null`.

### Task 19: Upgrade custom columns settings table

Goal: make the page usable for larger customer schemas.

Scope:

- Load settings rows from `GET /users/crm/custom-fields?entity=deal&include_inactive=true`.
- Show label, key, type, required, options count, `usage_count`, archived state, `last_changed`, and description without requiring row expansion for core facts.
- Add sorting by label, key, type, required, usage, archived state, updated time, and last-changed time.
- Add filters for type, required, archived/active, and used/unused.
- Save drag/drop order through `PATCH /users/crm/custom-fields/reorder` with the complete field-id list and the max loaded `updated_at` as `if_updated_at`.
- Archive through `PATCH /users/crm/custom-fields/{field_id}` with `{ "is_active": false, "if_updated_at": field.updated_at }`.
- Restore archived fields through `POST /users/crm/custom-fields/{field_id}/restore` with the loaded `if_updated_at`.
- On `409`, show a refresh/retry message and refetch instead of overwriting another admin's change.

Done when:

- The page remains scannable with dozens of fields.
- Sorting/filtering is accessible and keyboard usable.
- Reorder, archive, and restore all use backend version guards.
- Archived fields are visible in settings but excluded from active create/edit metadata after refetch.
- `last_changed: null` is rendered as a quiet no-history state, not as an error.
- Focused component/API integration tests cover active, archived, stale-version, and empty-list states.

### Task 20: Upgrade pipeline settings editor

Goal: make pipeline editing safe and clear for production admins.

Scope:

- Load editor data from `GET /users/crm/pipelines?include_stages=true`.
- Use `GET /users/crm/pipelines?include_stages=true&active_only=true` for deal creation pipeline/stage pickers so draft pipelines cannot receive new deals.
- Show `deal_count`, `last_changed`, `is_active`, default state, and open/won/lost stage grouping for each pipeline.
- Add dirty-state protection before switching selected pipeline.
- Send `if_updated_at` for pipeline PATCH, stage PATCH, and stage reorder.
- Save stage drag/drop through `PATCH /users/crm/pipelines/{pipeline_id}/stages/reorder` with the complete stage-id list and loaded `PipelineItem.updated_at`.
- Keep `stage_state` editable only on create; existing stages can edit only name, label, probability, and display order.
- Make default promotion explicit: `set_as_default` on create/clone, or `PATCH is_default=true` with `if_updated_at`.
- Use `/stages/{stage_id}/move-deals` before deleting a stage with deals.
- Explain inactive draft pipelines as "missing open/won/lost stage" and guide admins to add the missing stage state.

Done when:

- Unsaved edits are not lost silently.
- Stage reorder saves as one operation.
- Counts and lifecycle state are visible without visual noise.
- `409` stale-version errors trigger refetch/retry UX.
- Default promotion is blocked in the UI until the pipeline has at least one open, won, and lost stage.
- Focused tests cover draft pipeline, default promotion, reorder, stale-version, and move-before-delete flows.

### Task 21: Upgrade deal custom field controls

Goal: deal edit/create forms should render each custom field with the correct control.

Scope:

- Use `GET /users/crm/schema?entity=deal` for create/edit metadata; keep deal-detail `custom_column_definitions` only as the compatibility projection for existing detail surfaces.
- Submit writable custom values under `custom_columns.<key>` / `custom_columns` using the schema `write_path`.
- Render select as a select/listbox.
- Render multiselect as a multiselect control.
- Surface required indicators and description/help text.
- Preserve formula read-only behavior.
- Treat provider-backed fields and formula fields as read-only using `internal_write`, `editable`, `write_policy`, and `read_only_reason`.
- Validate required internal custom fields before submit using `internal_write_validation.required`.
- Validate select/multiselect options from `options.values`, including inactive/removed options for display while preventing new writes to inactive options.
- Keep deal list/search lightweight; do not expect custom values there. Detail/create/edit surfaces own custom field rendering.
- Hide CSV import entry points or show disabled-state copy until the import API is rebuilt.

Done when:

- Every supported custom field type has a matching input control.
- Required field errors are visible and accessible.
- Tests cover all custom field data types.
- Create and update flows both use `custom_columns`, while `custom_properties` remains only a deprecated compatibility alias.
- Provider-backed and formula fields cannot be edited through internal deal writes.

### Task 22: Improve blocked-delete dialogs

Goal: blocked deletes should guide users to the next action.

Scope:

- Use backend usage counts.
- Explain why delete is blocked.
- Offer archive, move, clear, migrate, or cancel depending on object type.
- For custom columns, use `usage_count` to decide between delete, archive, clear-values, and restore flows.
- For pipelines/stages, use `deal_count` to decide whether delete can proceed or whether move-before-delete is required.
- Show exact backend conflict messages for stale versions, default pipeline deletion, predefined custom columns, and last required stage deletion.
- After successful archive/clear/delete/restore/move, refetch the relevant settings list and schema metadata.

Done when:

- Users can resolve common blocked deletes without guessing.
- Dialogs are covered by focused component tests.
- Dialogs never offer a destructive path that the backend will reject because a safer prerequisite exists.

## Phase 7: Multi-entity schema support

### Task 23: Extend schema model beyond deals

Goal: avoid another redesign when custom fields expand to accounts and contacts.

Scope:

- Add schema support for accounts/companies and contacts at the backend contract level.
- Do not build all settings UI yet unless required.
- Validate entity-specific standard fields.

Done when:

- `GET /users/crm/schema?entity=account|contact|deal` works.
- Entity-specific fields are separated cleanly.
- Existing deal behavior is unchanged.

### Task 24: Add first non-deal custom field surface only after schema contract is stable

Goal: expand only after the shared contract is proven.

Scope:

- Choose the first non-deal entity based on product need.
- Reuse the same settings and validation patterns.

Done when:

- One non-deal entity supports custom fields end to end.
- No duplicate schema implementation is introduced.

## Phase 8: Reporting and filtering readiness

### Task 25: Define custom field indexing and query strategy

Goal: custom field filters and reports should not become slow or inconsistent.

Scope:

- Decide JSONB GIN vs generated/indexed columns vs selective reportable fields.
- Document when a field is filterable/sortable/searchable/analytics-enabled.
- Add migration/index changes only where justified.

Done when:

- The strategy is documented and tested with representative queries.
- The schema endpoint exposes field capability flags.
- No broad indexing is added without a query plan reason.

### Task 26: Add typed custom field filter helpers

Goal: avoid hand-rolled JSONB comparisons across list/reporting code.

Scope:

- Add backend helpers for typed custom field filtering.
- Support text, number, currency, percentage, boolean, date/datetime, select, and multiselect.
- Reuse metadata from the schema contract.

Done when:

- Deal list filters can use custom fields safely.
- Invalid filter operators fail early.
- Tests cover each supported type.

## Suggested execution order

1. Task 11
2. Task 16
3. Task 19
4. Task 20
5. Task 21

Tasks 7, 12-15, 17-18, and 22-26 can follow once the first production foundation is stable.
