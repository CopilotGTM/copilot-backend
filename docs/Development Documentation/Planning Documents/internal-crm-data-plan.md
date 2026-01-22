# Internal CRM (Native CRM Data) Plan

> Date: 2026-01-20
> Status: Draft
> Owner: Copilot Backend

---

## Table of Contents

1. Executive Summary
2. Current State Research
3. Goals and Non-Goals
4. Terminology
5. Core Design Decisions
6. Data Model and ID Strategy
7. API Plan (Backend)
8. Validation and Consistency Checks
9. Import Pipeline (Excel/CSV)
10. Frontend Plan
11. Background Jobs and Side Effects
12. Security, Audit, and Observability
13. Testing Strategy
14. Phased Delivery Plan

---

## 1. Executive Summary

We will add first-class "internal CRM" support so organizations can manage deals, contacts, companies, and pipelines directly inside Copilot while continuing to ingest multiple external CRMs in read-only mode. The plan reuses existing CRM tables and workflows (deals, stages, participants, activities, stakeholders, timeline) and introduces:

- A dedicated internal CRM integration record per org (no external OAuth).
- CRUD APIs for internal CRM entities and associations.
- A bulk import pipeline for Excel/CSV with mapping and validation.
- Frontend flows for onboarding, pipeline management, and data entry.
- Consistency checks to keep stakeholders, timeline, and AI insights accurate.
- Clear read/write boundaries: external CRM data is read-only; internal CRM data is read/write.

---

## 2. Current State Research

### 2.1 Existing CRM Data Model (Org Schema)

Key tables already exist and are used by external CRM sync:

- `crm_deals`, `crm_deal_stages`, `crm_pipelines`
- `crm_contacts`, `crm_companies`, `crm_users`
- `crm_deal_participants` (deal -> contact/company/user links)
- `crm_deal_stage_history`
- `crm_activities` (notes, tasks, calls, meetings)
- `crm_object_associations` (generic association table, integration_id can be NULL)
- `deal_stakeholders` (stakeholder registry across sources; CRM participants are one input)

Important constraints:

- `crm_deals`, `crm_contacts`, `crm_companies`, `crm_pipelines`, `crm_deal_stages`,
  `crm_deal_participants` use `SyncedRecordMixin` (integration_id is required).
- `crm_activities` and `crm_object_associations` use `FlexibleSyncedRecordMixin`
  (integration_id can be NULL, used for user-created notes).
- `owner_id` foreign keys resolve to `crm_users.user_id`.
- `crm_deal_participants` is the canonical source for CRM-sourced stakeholders and deal
  team membership. `deal_stakeholders` also contains calendar, email, and manual sources.

### 2.2 Existing CRM APIs

Current APIs under `/users/crm` are mostly read-only:

- Deals list and detail
- Deal tasks list
- Deal timeline, stakeholders, meetings, risks/blockers
- Notes: GET and POST for user-created notes

No endpoints exist for creating or updating deals, contacts, companies, pipelines,
stages, or participants.

Source-aware write guards do not exist today; for example, the notes POST endpoint
can create user notes on any deal, including external CRM deals. This currently
acts as an internal annotation layer for external data. We must decide and
formalize whether this remains allowed (recommended) and how it is labeled.

### 2.3 Existing Sync and Enrichment Pipelines

- External CRM sync inserts normalized data into the CRM tables.
- Stakeholder sync uses `crm_deal_participants` only.
- Deal timeline uses deal stage history, email, and meeting associations.
- AI deal summary, risk analysis, and stakeholder analysis are triggered by
  notes or explicit job requests.

These pipelines should work for internal CRM data as long as we store data in
the same tables and maintain the same relationships.

---

## 3. Goals and Non-Goals

### Goals

1. Allow organizations to create and manage CRM data internally.
2. Support a default pipeline template (metadata) with stages visible in the frontend
   before creation; create the actual pipeline only after user approval.
3. Allow attaching contacts and companies to deals and managing deal team users.
4. Support bulk data import from Excel/CSV with validation and preview.
5. Keep downstream features (stakeholders, timeline, AI summaries) functional.
6. Enforce multi-source boundaries: external CRM core records are read-only; internal CRM core
   records are read/write; internal annotations (notes/tasks) can be enabled across sources.

### Non-Goals (Phase 1)

- Full parity with HubSpot/Salesforce feature sets.
- Custom field builder UI (we will store extra fields in JSON properties).
- Bi-directional sync with external CRMs for internal data.

---

## 4. Terminology

- Internal CRM: Data created and managed inside Copilot (no external provider).
- External CRM: Data synced from HubSpot, Salesforce, etc.
- Internal CRM Integration: A `knowledge_integrations` record that represents the
  internal CRM source for an org.
- Source system: The integration type that owns a record (e.g., internal_crm, hubspot).

---

## 5. Core Design Decisions

### 5.1 Use an Internal CRM Integration Record

Because most CRM tables require `integration_id`, the internal CRM will use a
dedicated integration record to keep schema and pipelines intact.

Plan:

- Add a new `IntegrationType` value, e.g., `internal_crm`.
- Create a single `KnowledgeIntegration` row per org for internal CRM.
- `connection_id` is NULL, `auto_sync_enabled` is FALSE.
- Mark as "internal" in `connection_metadata` or `properties`.

Default pipeline (auto-created):

- Created automatically during organization provisioning alongside the integration.
- Existing organizations are backfilled via `scripts/maintenance/ensure_internal_crm_setup.py`.
- No user approval step required; pipeline is ready to use immediately.
- Users can customize pipelines later via PATCH/DELETE endpoints.
- Pipeline name: "Default Pipeline"
- Stages:
  - Qualification (open, 10%)
  - Discovery (open, 25%)
  - Proposal (open, 50%)
  - Negotiation (open, 75%)
  - Closed Won (won, 100%)
  - Closed Lost (lost, 0%)

### 5.2 Keep External and Internal Data Together

Internal CRM data is stored in the same CRM tables as external data, so existing
analytics, matching, and AI workflows continue to work. The internal integration
ID is used to filter internal vs external data when needed.

### 5.3 Source-aware Read/Write Boundaries

We will treat external CRM core records as read-only in Copilot. Internal CRM core records are read/write.
"External" means integration_type != internal_crm.
Internal CRM integrations are excluded from token refresh and external CRM sync loops.

Write policy matrix:

| Entity | External CRM | Internal CRM |
| --- | --- | --- |
| Deals, Contacts, Companies | Read-only | Read/write |
| Pipelines, Stages | Read-only | Read/write |
| Participants, Associations | Read-only | Read/write |
| CRM-synced Activities | Read-only | Read-only |
| User-created Activities (notes/tasks) | Write (internal annotations) | Write |

Enforcement rule: all POST/PATCH/DELETE endpoints must verify the target record's
integration_id matches the internal CRM integration for the org, except for
user-created activities which are allowed as internal annotations when enabled.
If not allowed, return HTTP 409 (read-only source) or 403 (forbidden).

### 5.4 Discrepancies and Resolutions

- Notes POST currently allows user-created notes on any deal (including external).
  Resolution: keep user-created notes as internal annotations on any deal. They do
  not sync outward and are stored with integration_id NULL. Optionally gate this
  behavior behind an org setting for strict read-only.
- Planned CRUD endpoints are not yet scoped to internal-only.
  Resolution: add a global write guard at service layer for all CRM write operations.
- Cross-integration linking is possible (internal contact to external deal).
  Resolution: enforce same integration_id across linked entities and participants.
  Exception: user-created activities (notes/tasks) may reference external deals.
- Dedupe rules are listed as org-wide.
  Resolution: dedupe only within the internal CRM integration (never merge across
  external CRMs).
- CRM users from app users could contaminate external CRM users.
  Resolution: create CRM users from app users only for internal CRM integration;
  external CRM users remain sync-only.

---

## 6. Data Model and ID Strategy

### 6.1 Integration and Source Tagging

For internal CRM records:

- `integration_id`: internal CRM integration ID
- `source_created_at`: actual user timestamp (now for new data)
- `synced_at`: now
- `properties`: add `{"source": "internal_crm"}` and any import metadata
- API responses should expose `source_system` (derived from integration_id and
  integration_type) so the frontend can enforce read-only UI for external records.

### 6.2 External ID Strategy

We need stable `external_id` values for internal data because unique constraints
use `(integration_id, external_id)`:

- Deals: `external_id = str(deal_id)`
- Contacts: `external_id = str(contact_id)`
- Companies: `external_id = str(company_id)`
- Users: `external_id = str(app_user_id)`
- Pipelines: `external_id = str(pipeline_id)`
- Stages: `external_id = str(stage_id)`
- Participants: `external_id` should be a stable UUID (recommended: uuidv5 or
  a hash-based UUID from a normalized tuple). Avoid raw string concatenation
  because `relationship_role` is nullable and IDs may contain separators.
  Store the normalized tuple in `properties` for idempotent upserts.

Dedupe and matching must be scoped to the internal CRM integration only. External
CRM records are never merged or mutated.

Dedupe and matching must be scoped to the internal CRM integration only. External
CRM records are never merged or mutated.

### 6.3 CRM Users from App Users

Owners and internal team members are selected directly from the existing app user
list (`public.users`). There is no separate user creation flow for CRM users.
We will create or update `crm_users` rows on-demand to satisfy FKs and reuse
existing CRM filtering logic.

- `crm_users.external_id = users.user_id` (string)
- `crm_users.email`, `full_name`, `job_title` copied from `users`
- This enables `owner_id` FK resolution and participant user linking.
- This applies only to internal CRM; external CRM users remain read-only.

Role handling:

- Deal owner: single select from app users.
- Deal team members: multi-select from app users and stored as
  `crm_deal_participants` with `participant_type=USER`.
- Team roles (example: "SC") are stored in `relationship_role`.

### 6.4 Deal Participants and Associations

Internal association strategy:

- Use `crm_deal_participants` as canonical for deal -> contact/company/user.
- Optionally create `crm_object_associations` for broader relationships
  (contact -> company, deal -> company, deal -> contact).
- `association_category = internal` for internal CRM associations.
- Participants and associations must always link entities from the same integration.

### 6.5 Stage History

When a deal stage changes:

- Insert a `crm_deal_stage_history` record.
- Update `stage_entered_at`, `stage_updated_at`, and `deal_status`.
- Update `closed_won_at` or `closed_lost_at` for closed stages.

---

## 7. API Plan (Backend)

Write guard (global):

- All POST/PATCH/DELETE endpoints must verify the target record belongs to the
  internal CRM integration. External CRM records are read-only.
- Read endpoints should include `source_system` and optionally accept `source`
  filtering (internal_crm, hubspot, salesforce).

### 7.1 Internal CRM Provisioning (Org-Level)

- No public setup endpoint required.
- Internal CRM integration AND default pipeline are created automatically:
  - During organization provisioning via `OrganizationManagementService._setup_internal_crm()`
  - For existing organizations via backfill script `scripts/maintenance/ensure_internal_crm_setup.py`
- The `InternalCRMSetupService.setup_internal_crm()` method handles both integration and
  pipeline creation idempotently (safe to call multiple times).
- Frontend uses `GET /users/crm/pipelines?include_stages=true` to load the default pipeline;
  the pipeline is guaranteed to exist for all active organizations.

### 7.2 Pipelines and Stages

Read endpoints (implemented):
- `GET /users/crm/pipelines` (list pipelines with optional `include_stages=true`)
- `GET /users/crm/pipelines/{pipeline_id}/stages` (list stages for a pipeline)

Future customization endpoints (planned):
- `POST /users/crm/pipelines` (create additional pipelines)
- `PATCH /users/crm/pipelines/{pipeline_id}` (update pipeline name, display order)
- `DELETE /users/crm/pipelines/{pipeline_id}` (soft delete)
  - Only allowed if no active deals remain in the pipeline, or require a
    reassignment payload to move deals to another pipeline/stage.
- `POST /users/crm/pipelines/{pipeline_id}/stages` (add stages)
- `PATCH /users/crm/stages/{stage_id}` (update stage properties)
- `DELETE /users/crm/stages/{stage_id}` (soft delete)
  - Only allowed if no active deals remain in the stage, or require a
    reassignment payload to move deals to another stage.

Note: `GET /users/crm/pipeline-templates` is not required since the default pipeline
is auto-created during organization provisioning.

### 7.3 Deals

- `POST /users/crm/deals` (create internal deal)
- `PATCH /users/crm/deals/{deal_id}` (update core fields)
- `DELETE /users/crm/deals/{deal_id}` (soft delete)
- `POST /users/crm/deals/{deal_id}/stage` (move stage, updates history)
- `POST /users/crm/deals/{deal_id}/owner` (assign single owner)
- Team members are added via `POST /users/crm/deals/{deal_id}/participants`
  with `participant_type=USER`.

### 7.4 Contacts and Companies

- `GET /users/crm/contacts` (search, filter)
- `POST /users/crm/contacts`
- `PATCH /users/crm/contacts/{contact_id}`
- `DELETE /users/crm/contacts/{contact_id}` (soft delete)

- `GET /users/crm/companies`
- `POST /users/crm/companies`
- `PATCH /users/crm/companies/{company_id}`
- `DELETE /users/crm/companies/{company_id}` (soft delete)

### 7.5 Participants and Associations

Deal participants (used by stakeholders and deal team):

- `POST /users/crm/deals/{deal_id}/participants`
  - Adds contacts, companies, or users to a deal.
- `DELETE /users/crm/deals/{deal_id}/participants/{participant_id}`

Optional generic associations:

- `POST /users/crm/associations`
- `DELETE /users/crm/associations/{association_id}`

### 7.6 Activities (Tasks, Notes, Meetings)

Existing:

- `GET /users/crm/deals/{deal_id}/notes`
- `POST /users/crm/deals/{deal_id}/notes`
- `GET /users/crm/deals/{deal_id}/tasks`

Planned additions:

- `POST /users/crm/deals/{deal_id}/tasks`
- `PATCH /users/crm/tasks/{task_id}`
- `DELETE /users/crm/tasks/{task_id}`

Notes and tasks are write-enabled as internal annotations (integration_id NULL).
If strict read-only is required for an org, disable these writes for external
CRM deals via a setting or policy check.

### 7.7 App User Search (Team Selection)

Endpoints for selecting app users as owners/team members:

- `GET /users/crm/users?q={search}` (search app users by name or email)
  - Queries `public.users` table (app users), NOT `crm_users` table
  - Supports `limit`, `cursor`, and `q` parameters
  - Returns org members eligible for deal owner/team roles
  - Source: `src/api/routers/crm/users_handler.py`
- `POST /users/crm/deals/{deal_id}/owner` (assign owner from app users)
- `POST /users/crm/deals/{deal_id}/participants` (assign team members with roles, e.g., "SC")

Note: `crm_users` rows are created/updated automatically when a user is assigned
as an owner or participant. There is no separate CRM user management UI.

### 7.8 Bulk Import

- `POST /users/crm/imports` (upload file, create import job)
- `GET /users/crm/imports/{import_id}` (status)
- `POST /users/crm/imports/{import_id}/preview`
- `POST /users/crm/imports/{import_id}/commit`
- `GET /users/crm/imports/{import_id}/errors`

---

## 8. Validation and Consistency Checks

### 8.0 Source Ownership Checks

- Reject any write if the record's integration_id does not match the internal CRM integration,
  except for user-created activities (notes/tasks) which are allowed as internal annotations.
- Reject any participant/association link when integration_ids differ.
- Reject any import row mapped to an external CRM integration.

### 8.1 Deal Checks

- Pipeline and stage must belong to the same org and pipeline.
- `deal_status` must align with stage state:
  - stage_state WON -> deal_status WON
  - stage_state LOST -> deal_status LOST
- `probability` must be 0-100.
- Amount must be >= 0; currency must be ISO-4217.
- Stage changes must append `crm_deal_stage_history`.

### 8.2 Contact Checks

- Email must be valid format when provided.
- Dedupe rule (internal CRM): match by email + org.
- If email changes, update:
  - `crm_deal_participants.related_email`
  - `deal_stakeholders.email` (and mark for rematch)

### 8.3 Company Checks

- Normalize domain for dedupe (lowercase, strip www).
- If company name/domain changes, update:
  - `crm_deals.company_name` and `crm_deals.company_domain`
  - `crm_deal_participants.related_name`

### 8.4 CRM User Checks

- CRM users must map to existing app users.
- When app user name/email changes, update:
  - `crm_users` fields
  - `crm_deals.owner_name/email`
  - `crm_contacts.owner_name/email`
  - `crm_companies.owner_name/email`
  - `crm_activities.owner_name/email`
  - `crm_deal_participants.related_name/email` where participant_type=USER

### 8.5 Participant and Association Checks

- Prevent duplicate participant links using the unique key:
  `(deal_id, participant_type, related_external_id, relationship_role)`.
- Note: Postgres unique constraints treat NULLs as distinct, so duplicates are
  still possible when `relationship_role` is NULL. For internal CRM writes, we
  should normalize NULL to empty string or enforce app-level dedupe. Optionally
  add a DB constraint using `COALESCE(relationship_role, '')`.
- If a participant is removed, mark `is_deleted` and update stakeholders
  (approval_status=REMOVED).

---

## 9. Import Pipeline (Excel/CSV)

### 9.1 Supported Formats

- CSV (recommended)
- XLSX (single or multi-sheet)

### 9.2 Import Workflow

1. Upload file -> create import job record.
2. Parse file -> detect sheets, columns.
3. User maps columns to target fields (deal/contact/company).
4. Preview validation (row-level warnings/errors).
5. Commit -> async job writes to CRM tables.
6. Return summary + error report.

### 9.3 Matching and Dedupe Rules

- Contacts: match by email (exact, case-insensitive).
- Companies: match by domain if provided; else by name.
- Deals: match by (name + company) or optional external deal id column.
- Deals: avoid name-only matching. Prefer an external_id column or a
  multi-signal match (company + amount + close_date + stage + owner) with
  confidence scoring. Default to "create new" when confidence is low.
- Participants: link using contact/company ids from matched entities.

All imports target the internal CRM integration only. If a row matches an external
CRM record, flag it as a duplicate and skip or require manual review.

### 9.4 Import Metadata

Store in `properties`:

```
{
  "source": "import",
  "import_id": "...",
  "source_file": "...",
  "source_row": 123
}
```

---

## 10. Frontend Plan

### 10.1 Onboarding and Setup

- "Use Copilot CRM" choice for orgs without CRM integration.
- Show default pipeline template metadata; create pipeline only on approval.
- Select CRM users (from org user list).
- Option to import data (CSV/XLSX) or start empty.

### 10.2 Pipeline View

- Kanban board per pipeline and stage.
- Drag and drop to move deals (calls stage change API).
- Stage edit mode (reorder, rename, close-won/lost).

### 10.3 Deal Management

- Deal list view with filters (status, owner, stage, pipeline).
- Deal detail view with editable fields for internal deals.
- External CRM deals are read-only for core fields (disable edits, show source badge/link).
  Notes/tasks can remain enabled as internal annotations if policy allows.
- Attach contacts, company, and internal team members.
- Notes and tasks sections (existing notes UI, add tasks).
  - Owner selection is a single-select from app users.
  - Team selection is a multi-select from app users with a role label (e.g., "SC").

### 10.4 Contacts and Companies

- Directory views with search and bulk import.
- Contact detail (linked deals, activities).
- Company detail (linked deals, contacts).

### 10.5 Import Wizard

Multi-step:

1. Upload file
2. Map columns
3. Preview + errors
4. Confirm and run import
5. Results summary + download error rows

---

## 11. Background Jobs and Side Effects

- After deal create/update:
  - Trigger stakeholder sync if participants changed.
  - Trigger deal timeline sync for stage changes.
  - Optionally enqueue AI deal summary job.

- After contact updates:
  - Re-run stakeholder sync for affected deals.
  - Re-run email/calendar matching if primary contact changed.

- After import completion:
  - Run enrichment to resolve owner_id and participant names/emails.
  - Trigger timeline backfill for imported data.

External CRM records only change via sync; internal update jobs should not run
for external records except when triggered by external sync.

Stakeholder dedupe rule: if a manual stakeholder exists and a CRM participant is
added with the same email, keep a single `deal_stakeholder` row (unique constraint
enforces this). Define precedence: CRM source should set `source_reference_id`
and `approval_status=APPROVED` while preserving manual enrichment fields.

Note creation already triggers the risk_blocker -> stakeholder -> deal_summary
pipeline. Decide whether this stays enabled for internal annotations on external
deals; default to keep behavior for consistency.

---

## 12. Security, Audit, and Observability

- All endpoints require org-scoped auth (existing `get_current_user`).
- Admin-only actions: pipeline edit, bulk import, delete operations.
- Enforce source-aware read-only policy in service layer for all write endpoints.
- Add audit log entries for create/update/delete operations:
  - actor_id, entity_type, entity_id, changes, timestamp.
- Add rate limiting for all new write endpoints (including notes/tasks). The
  current notes POST endpoint is not rate-limited and should be updated.

Metrics and logs:

- Import job counts, failures, and time.
- Deal create/update rate.
- Stakeholder sync triggers from internal changes.

---

## 13. Testing Strategy

- Unit tests for validation rules (deal stage transitions, dedupe).
- Service tests for CRUD and association consistency.
- Import pipeline tests:
  - CSV parsing
  - Mapping validation
  - Dedupe and update behavior
- API tests for role-based access (admin vs member).

---

## 14. Phased Delivery Plan

### Phase 1: Core Internal CRM

- Internal CRM integration + default pipeline creation (implemented)
  - Auto-created during org provisioning via `OrganizationManagementService._setup_internal_crm()`
  - Backfill script for existing orgs: `scripts/maintenance/ensure_internal_crm_setup.py`
  - `InternalCRMSetupService.setup_internal_crm()` creates integration + pipeline + 6 stages
- Pipeline read endpoints (implemented)
  - `GET /users/crm/pipelines`
  - `GET /users/crm/pipelines/{pipeline_id}/stages`
- Deal CRUD + stage move (implemented)
  - `POST /users/crm/deals`
  - `PATCH /users/crm/deals/{deal_id}`
  - `DELETE /users/crm/deals/{deal_id}`
  - `POST /users/crm/deals/{deal_id}/stage`
  - `POST /users/crm/deals/{deal_id}/owner`
- CRM users mapping (implemented)
  - `GET /users/crm/users`

### Phase 2: Contacts, Companies, and Associations

- Contact and company CRUD (implemented)
  - `GET /users/crm/contacts`
  - `POST /users/crm/contacts`
  - `PATCH /users/crm/contacts/{contact_id}`
  - `DELETE /users/crm/contacts/{contact_id}`
  - `GET /users/crm/companies`
  - `POST /users/crm/companies`
  - `PATCH /users/crm/companies/{company_id}`
  - `DELETE /users/crm/companies/{company_id}`
- Deal participants and team members (implemented)
  - `POST /users/crm/deals/{deal_id}/participants`
  - `DELETE /users/crm/deals/{deal_id}/participants/{participant_id}`
- Stakeholder sync triggers (implemented for contact participant add/remove and contact updates)

### Phase 3: Activities and Notes

- Task creation/edit/delete (implemented)
- Notes enhancements (edit/delete for internal notes) (implemented)

### Phase 4: Import and Onboarding

- CSV/XLSX import pipeline
- Import wizard UI
- Post-import enrichment and timeline sync

### Phase 5: Polishing and Analytics

- Bulk edits and exports
- Dedupe/merge UI
- Advanced reporting filters
