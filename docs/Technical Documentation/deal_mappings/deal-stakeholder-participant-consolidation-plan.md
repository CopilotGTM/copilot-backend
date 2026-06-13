# Deal Stakeholder Participant Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `crm_deal_participants` the canonical deal-person/stakeholder table, keep `crm_contacts` as the authoritative person/profile table, and retire `deal_stakeholders` only after backfill and production validation.

**Architecture:** A stakeholder is an active `CONTACT` participant on a deal. Public API payloads expose `participant_id` as the canonical `CRMDealParticipant.participant_id`; "stakeholder" remains the feature/resource language, not a second durable ID. Durable contact profile and research live on `CRMContact`; deal-specific behavior and display snapshots live on `CRMDealParticipant` so list reads do not always need a contact join.

**Tech Stack:** Python 3.11, SQLModel/SQLAlchemy async sessions, PostgreSQL JSONB, Alembic org migrations, FastAPI, Celery, pytest.

---

## Final Data Contract

### Identity

1. `CRMDealParticipant.participant_id` is the only durable stakeholder identity.
2. Public stakeholder/contact-involvement payloads expose `participant_id`, not a duplicate `stakeholder_id` alias.
3. Do not add `legacy_stakeholder_id`, `old_stakeholder_id`, `canonical_stakeholder_participant_id`, or any other persisted alias for `DealStakeholder.stakeholder_id`.
4. Do not add `is_stakeholder`. Every active `CONTACT` participant on a deal is the stakeholder row.
5. Enforce one active contact participant per deal/contact and one active contact participant per deal/normalized email. The same email/contact must not appear twice as a participant on one deal after migration hardening.
6. `DealStakeholder.stakeholder_id` is a migration input only. When creating an app-owned participant from an old stakeholder row, seed `CRMDealParticipant.participant_id` with that UUID.

### Contact vs Participant Ownership

1. `CRMContact` owns durable person/profile fields: name, email, job title, phone/mobile, company linkage, provider properties, profile enrichment, `contact_research`, and `last_researched_at`.
2. `CRMDealParticipant` owns deal-specific behavior: `approval_status`, `stance`, `decision_power`, `motivation`, `pain_points`, `keywords`, `last_contact_at`, `temperature`, `created_by_user_id`, `participant_source`, and `source_reference_id`.
3. `CRMDealParticipant` keeps display snapshots for fast list reads: `related_name`, `related_email`, `job_title`, `department`, `phone_number`, and `external_links`.
4. `CRMDealParticipant` also keeps `stakeholder_research` and `last_researched_at` because stakeholder list/detail surfaces need to show deal-scoped research without requiring a contact join.
5. When a contact exists, write durable profile/research fields to `CRMContact` first, then refresh participant snapshots.
6. Deal-specific fields must never be copied from one deal participant to another just because they share the same contact.

### Explicit Non-Goals

Do not add or migrate these fields to `CRMDealParticipant`:

```text
is_stakeholder
legacy_stakeholder_id
canonical_stakeholder_participant_id
stakeholder_created_by_user_id
stakeholder_source
stakeholder_source_reference_id
stakeholder_data
stakeholder_engagement_history
champion_potential
engagement_score
```

Use these participant-neutral names instead:

```text
created_by_user_id
participant_source
source_reference_id
```

`participant_source` should store the existing `StakeholderSource` values (`CRM`, `CALENDAR_DISCOVERY`, `EMAIL_DISCOVERY`, `MANUAL`). Keep the database column as string unless the enum is moved to a neutral module that does not create an import cycle with `deals.py`.

---

## Current Coupling To Move

These surfaces currently depend on `DealStakeholder`, `deal_stakeholders`, or old stakeholder IDs and must move in stages:

- `src/domain/models/organisation_tables/sync/crm/stakeholders.py`
- `src/domain/models/organisation_tables/sync/crm/deals.py`
- `src/domain/models/organisation_tables/sync/crm/contacts.py`
- `src/domain/models/organisation_tables/job_models.py`
- `src/domain/models/organisation_tables/sync/stakeholder_insight_models.py`
- `src/service/crm/insights/stakeholders_service.py`
- `src/service/crm/internal/deal_participant_service.py`
- `src/service/crm/internal/deal_write_service.py`
- `src/service/crm/access/leads_access_service.py`
- `src/service/crm/access/contacts_access_service.py`
- `src/service/crm/insights/stakeholder_insight_service.py`
- `src/service/crm/timeline/deal_timeline_sync_service.py`
- `src/service/crm/jobs/deal_job_service.py`
- `src/service/crm/jobs/stakeholder_job_visibility_service.py`
- `src/workers/deal_worker/services/deal_mapper_services/stakeholder_sync_service.py`
- `src/workers/deal_worker/services/deal_mapper_services/deal_calendar_matcher.py`
- `src/workers/deal_worker/services/deal_mapper_services/deal_email_matcher.py`
- `src/workers/deal_worker/services/deal_mapper_services/deal_meeting_matcher.py`
- `src/workers/deal_worker/services/deal_insight_services/stakeholder_analysis_service.py`
- `src/workers/deal_worker/services/deal_insight_services/stakeholder_research_service.py`
- `src/workers/deal_worker/tasks/deal_insight_tasks/stakeholder_research_tasks.py`
- `src/workers/deal_worker/utils/stakeholder_research_dispatch.py`
- `src/workers/deal_worker/utils/deal_job_utils.py`
- `src/workers/deal_worker/services/stakeholder_lookup_service.py`
- `src/workers/deal_worker/utils/pipeline_dispatch.py`
- `src/service/calendar_invites/attendee_resolver.py`
- `src/service/bots/form_handlers/add_stakeholder_handler.py`
- `src/service/crm/discovery/calendar_contact_account_discovery_jobs.py`
- `src/service/integrations/crm/records/hubspot/deals/participant_writer.py`
- `src/service/integrations/crm/records/salesforce/deals/participant_writer.py`
- `src/service/integrations/webhooks/hubspot/contacts/privacy_deletion.py`
- `src/contracts/crm/stakeholders.py`
- `src/api/schemas/crm_models.py`
- `src/api/schemas/crm_contact_models.py`
- `src/agents/tools/crm/list_stakeholders.py`
- `src/agents/tools/crm/get_stakeholder_details.py`
- `src/agents/tools/crm/upsert_stakeholder.py`
- `src/agents/tools/crm/add_stakeholder.py`
- `src/agents/tools/crm/create_stakeholder_if_missing.py`
- `src/agents/tools/crm/search_contacts.py`
- `src/agents/tools/crm/get_deal_details.py`
- `src/agents/tools/crm/enrich_stakeholder.py`
- `src/agents/tools/crm/list_stakeholder_insights.py`
- `src/agents/tools/research/research_stakeholder.py`
- `src/agents/workflows/stakeholder_research/agent.py`
- `src/agents/workflows/stakeholder_research/prompts.py`
- `src/agents/workflows/stakeholder_analysis/models.py`
- `src/agents/workflows/briefing/prompts.py`
- `src/agents/workflows/deal_memory/agent.py`
- `src/agents/tools/registry.py`
- `src/agents/tools/README.md`
- stakeholder API docs and router tests under `docs/API Documentation/` and `tests/api/routers/crm`

Do not confuse this work with `integration_participants`. That table is cross-integration identity and is not the target for deal-level stakeholder state.

---

## Column Mapping

| Current `DealStakeholder` field | Authoritative target | Participant read-model target |
| --- | --- | --- |
| `stakeholder_id` | migration input only; public identity is `CRMDealParticipant.participant_id` exposed as `participant_id` | none |
| `deal_id` | existing `CRMDealParticipant.deal_id` | existing `CRMDealParticipant.deal_id` |
| `contact_id` | existing `CRMContact.contact_id` when present | existing `CRMDealParticipant.related_local_id` |
| `email` | existing `CRMContact.email` | existing `CRMDealParticipant.related_email` snapshot |
| `name` | existing `CRMContact.full_name` | existing `CRMDealParticipant.related_name` snapshot |
| `job_title` | existing `CRMContact.job_title` | new `CRMDealParticipant.job_title` snapshot |
| `department` | `CRMContact.properties["department"]` until a dedicated contact column exists | new `CRMDealParticipant.department` snapshot |
| `phone_number` | existing `CRMContact.phone` or `CRMContact.mobile_phone` | new `CRMDealParticipant.phone_number` snapshot |
| `external_links` | `CRMContact.properties["external_links"]` until a dedicated contact column exists | new `CRMDealParticipant.external_links` snapshot |
| `stance` | new `CRMDealParticipant.stance` | same |
| `decision_power` | new `CRMDealParticipant.decision_power` | same |
| `motivation` | new `CRMDealParticipant.motivation` | same |
| `pain_points` | new `CRMDealParticipant.pain_points` | same |
| `keywords` | new `CRMDealParticipant.keywords` | same |
| `last_contact_at` | new `CRMDealParticipant.last_contact_at` | same |
| `temperature` | new `CRMDealParticipant.temperature` | same |
| `source` | new `CRMDealParticipant.participant_source` | same |
| `source_reference_id` | new `CRMDealParticipant.source_reference_id` when it points at a real source record | same |
| `approval_status` | new `CRMDealParticipant.approval_status` | same |
| `stakeholder_research` | `CRMContact.contact_research` for reusable person research | `CRMDealParticipant.stakeholder_research` for deal display |
| `last_researched_at` | `CRMContact.last_researched_at` for reusable person research | `CRMDealParticipant.last_researched_at` for deal display |
| `created_by_user_id` | new `CRMDealParticipant.created_by_user_id` | same |
| `champion_potential` | do not migrate in this participant slice | none |
| `engagement_score` | do not migrate in this participant slice | none |
| `stakeholder_data` | do not migrate as a JSONB blob | none |
| `stakeholder_engagement_history` | do not migrate as a JSONB blob | none |

`relationship_role` must not be reused as `job_title`. It is CRM association metadata.

---

## Production Rules

1. Every query must remain scoped by `organisation_id`.
2. Participant-backed stakeholders are only `participant_type == CONTACT` and `is_deleted == False`.
3. `USER`, `COMPANY`, `TICKET`, `LINE_ITEM`, and `CUSTOM` participants are not stakeholders.
4. API responses should expose `participant_id`; keep `stakeholder_id` only where an existing persisted job/insight/timeline field or tool input name already requires that compatibility name.
5. New jobs, insights, tools, and timeline rows should write participant IDs. Existing rows with old stakeholder UUIDs must be migrated before runtime reads cut over.
6. Manual, CRM, calendar-discovered, and email-discovered stakeholders must work. Do not build a solution that only works for CRM-synced participants.
7. `deal_stakeholders` is not dropped in the first PR. It is retired only after participant row-count validation and production runtime-reference checks pass.
8. Do not rely on `CRMDealParticipant.properties`; the current model does not define that column.
9. Construct manual participant rows with `CRMDealParticipant(...)`, not `CRMDealParticipant.model_validate(...)`, because `integration_id` is required by `SyncedRecordMixin`.
10. Any write path that creates or reactivates a participant by contact/email must be race-safe: normalize the email, lock the deal row or candidate participant rows consistently, and recover duplicate insert races by reloading the existing row inside a nested transaction/savepoint.
11. Provider sync owns association fields: `relationship_role`, `related_local_id`, `related_name`, `related_email`, `synced_at`, `source_modified_at`, and `last_seen_at`.
12. Contact profile logic owns durable profile fields on `CRMContact`: name, email, job title, phone/mobile, company linkage, provider properties, `contact_research`, and `last_researched_at`.
13. Participant profile snapshot fields are `related_name`, `related_email`, `job_title`, `department`, `phone_number`, and `external_links`. They exist for fast stakeholder list reads and must be refreshed from `CRMContact` when a contact is known.
14. App stakeholder logic owns deal behavior fields: `created_by_user_id`, `stance`, `decision_power`, `motivation`, `pain_points`, `keywords`, `last_contact_at`, `temperature`, `participant_source`, `source_reference_id`, `approval_status`, `stakeholder_research`, and `last_researched_at`.
15. Provider participant writers must not clear participant profile snapshots from sparse association payloads and must not clear or overwrite app-owned deal behavior fields when upserting CRM participant rows.
16. Manual/API/agent-created stakeholders on external CRM deals first attach to an existing contact participant by `related_local_id` or normalized `related_email`. If no participant exists, create an app-owned participant under the internal CRM integration.
17. Queries must not require `CRMDealParticipant.integration_id == CRMDeal.integration_id` for stakeholder rows. They must rely on `organisation_id`, `deal_id`, `participant_type`, and `is_deleted`.
18. Privacy deletion must scrub both `CRMContact` profile PII and participant display snapshots while the participant intentionally duplicates contact data for list reads.

---

## Release Boundaries

This migration is too broad for one production PR. Implement it in these releases:

1. **Schema + clean participant/contact primitives:** add participant behavior columns, participant profile snapshot columns, contact research columns, adapter, contact/profile snapshot helpers, and backfill script. No runtime reads move yet.
2. **Participant writer hardening before read cutover:** make HubSpot/Salesforce provider writers plus internal CRM and lead-conversion participant creation paths reuse one active contact participant per deal/contact/email, preserve deal-owned behavior, refresh profile snapshots from contacts, stop writing nonexistent participant `properties`, add duplicate audits, and keep the final uniqueness indexes in the existing `060` schema migration.
3. **Backfill + read cutover:** backfill old stakeholder rows into matched contacts and matched participant rows, migrate dependent job/insight/timeline references to participant IDs, then move stakeholder list/detail/contact involvement reads to participant-backed rows. Do not keep runtime fallback by old `DealStakeholder.stakeholder_id`.
4. **Write cutover:** move manual/API/agent add/update/remove/approve/reject flows, including `add_contacts_as_stakeholders()`.
5. **Worker cutover:** move stakeholder sync, calendar/email/meeting discovery, research dispatch, analysis, mapper utilities, attendee resolution, timeline sync, job visibility, and stakeholder lookup.
6. **Jobs, insights, tools, and privacy:** persist new job/insight rows with participant IDs, move agent tools, and scrub both participant-backed and old `deal_stakeholders` PII while the old table exists.
7. **Uniqueness validation:** run duplicate audits before applying the schema migration in any environment with existing participant data, and clean blocking duplicates before the partial unique indexes in `060` are applied.
8. **Old-table retirement:** remove runtime reads and drop `deal_stakeholders` in a later release.

Each release must pass its focused tests before the next release starts. Do not bundle table retirement with runtime cutover.

---

## Phase 1: Schema, Adapter, And Backfill Primitives

### Task 1: Add Participant And Contact Columns

**Files:**
- Modify: `src/domain/models/organisation_tables/sync/crm/deals.py`
- Modify: `src/domain/models/organisation_tables/sync/crm/contacts.py`
- Create/modify: `migrations/org_migrations/versions/3c7f79637739_060_add_stakeholder_fields_to_deal_participants.py`
- Test: `tests/domain/models/organisation_tables/sync/crm/test_crm_deal_participant_stakeholder_fields.py`

- [x] Add `CRMDealParticipant` columns:

```text
created_by_user_id
job_title
department
phone_number
external_links
stance
decision_power
motivation
pain_points
keywords
last_contact_at
temperature
participant_source
source_reference_id
approval_status
stakeholder_research
last_researched_at
```

- [x] Add `CRMContact` columns:

```text
contact_research
last_researched_at
```

- [x] Add a focused participant lookup index over:

```text
organisation_id
deal_id
participant_type
approval_status
is_deleted
```

- [x] Do not add any field from the Explicit Non-Goals list.

- [x] Run:

```bash
uv run pytest tests/domain/models/organisation_tables/sync/crm/test_crm_deal_participant_stakeholder_fields.py -v
uv run alembic -c migrations/org_migrations/alembic.ini heads
```

Expected: model tests pass and the new org migration is the Alembic head.

Implementation note:
- Participant/contact stakeholder fields are in `CRMDealParticipant`, `CRMContact`, and
  org migration `060`. Verification passed with the model test and
  `uv run alembic -c migrations/org_migrations/alembic.ini heads`.

### Task 2: Add Participant Stakeholder Adapter

**Files:**
- Create/modify: `src/service/crm/insights/participant_stakeholder_adapter.py`
- Test: `tests/service/crm/insights/test_participant_stakeholder_adapter.py`

- [x] Add an adapter that converts `CRMDealParticipant` to stakeholder response dictionaries.
- [x] Return `participant_id == participant.participant_id` without a duplicate `stakeholder_id` alias.
- [x] Prefer loaded `CRMContact` profile and research fields when supplied.
- [x] Fall back to participant display snapshots when no contact is supplied.
- [x] Exclude `champion_potential`, `engagement_score`, `stakeholder_data`, and `stakeholder_engagement_history`.
- [x] Run:

```bash
uv run pytest tests/service/crm/insights/test_participant_stakeholder_adapter.py -v
```

Expected: adapter tests pass with and without a joined contact.

Implementation note:
- `participant_stakeholder_adapter` maps participant rows to stakeholder response dictionaries
  with `participant_id` as the only public identity field.

### Task 3: Add Backfill Helpers

**Files:**
- Create/modify: `scripts/backfill_participant_stakeholder_fields.py`
- Test: `tests/scripts/test_backfill_participant_stakeholder_fields.py`

- [x] Match old stakeholder rows to an existing participant by `source_reference_id`, `related_local_id`, or normalized email.
- [x] Match or create `CRMContact` first by contact ID or normalized email.
- [x] Fill missing contact profile fields non-destructively.
- [x] Refresh participant snapshots from the matched contact.
- [x] Copy only accepted deal behavior and research fields to the participant.
- [x] When no participant exists, create an app-owned `CRMDealParticipant` using old `DealStakeholder.stakeholder_id` as the new `participant_id`.
- [x] Do not copy any field from the Explicit Non-Goals list.
- [x] Run:

```bash
uv run pytest tests/scripts/test_backfill_participant_stakeholder_fields.py -v
```

Expected: backfill tests pass and assert removed fields are not copied.

Implementation note:
- Backfill helpers copy durable profile/research into contacts and deal-scoped stakeholder
  behavior/research into participants without restoring deprecated score/blob fields.

---

## Phase 2: Writer Hardening

### Task 4: Reuse One Active Deal Contact Participant

**Files:**
- Modify: `src/service/integrations/crm/records/hubspot/deals/participant_writer.py`
- Modify: `src/service/integrations/crm/records/salesforce/deals/participant_writer.py`
- Modify: `src/service/crm/internal/deal_participant_service.py`
- Modify: `src/service/crm/internal/deal_write_service.py`
- Modify: `src/service/crm/access/leads_access_service.py`
- Test: existing focused participant writer, deal write, and lead conversion tests.

- [x] Before inserting a contact participant, search by `organisation_id`, `deal_id`, `participant_type == CONTACT`, `is_deleted == False`, `related_local_id`, and normalized `related_email`.
- [x] Reactivate or update the existing participant instead of inserting another row for the same deal/contact/email.
- [x] Preserve app-owned behavior fields when provider sync updates association fields.
- [x] Refresh display snapshots from `CRMContact` when a contact is resolved.
- [x] Do not use `participant.properties`.
- [x] Run focused tests for the modified writer surfaces.

Implementation note:
- Internal participant creation, lead conversion, and provider association writers now reuse
  active contact participants by local contact ID or normalized email and preserve app-owned
  stakeholder fields.

### Task 5: Add Duplicate Audits And Unique Indexes

**Files:**
- Create: `scripts/audit_duplicate_deal_contact_participants.py`
- Modify existing migration: `migrations/org_migrations/versions/3c7f79637739_060_add_stakeholder_fields_to_deal_participants.py`
- Test: `tests/scripts/test_audit_duplicate_deal_contact_participants.py`

- [x] Audit duplicate active contact participants by normalized email:

```sql
SELECT organisation_id, deal_id, lower(btrim(related_email)) AS email, count(*)
FROM crm_deal_participants
WHERE participant_type = 'contact'
  AND related_email IS NOT NULL
  AND btrim(related_email) <> ''
  AND is_deleted = false
GROUP BY organisation_id, deal_id, lower(btrim(related_email))
HAVING count(*) > 1;
```

- [x] Audit duplicate active contact participants by local contact ID:

```sql
SELECT organisation_id, deal_id, related_local_id, count(*)
FROM crm_deal_participants
WHERE participant_type = 'contact'
  AND related_local_id IS NOT NULL
  AND is_deleted = false
GROUP BY organisation_id, deal_id, related_local_id
HAVING count(*) > 1;
```

- [x] Keep partial unique indexes for active contact participants by deal/email and deal/contact in the existing `060` migration, not a separate `061`/`062` migration.
- [x] Do not include `relationship_role` or `is_stakeholder` in these uniqueness indexes.

Implementation note:
- Duplicate audit coverage and partial unique indexes live with migration `060`. HubSpot and
  Salesforce writer tests must be run as separate pytest commands because both files share the
  same basename under different package paths.

---

## Phase 3: Read And Write Cutover

### Task 6: Move Stakeholder Reads To Participants

**Files:**
- Modify: `src/service/crm/insights/stakeholders_service.py`
- Modify: `src/service/crm/access/contacts_access_service.py`
- Modify: `src/contracts/crm/stakeholders.py`
- Modify: `src/api/schemas/crm_models.py`
- Modify: `src/api/schemas/crm_contact_models.py`
- Modify: `src/agents/tools/crm/list_stakeholders.py`
- Modify: `src/agents/tools/crm/get_stakeholder_details.py`
- Modify: `src/agents/tools/crm/search_contacts.py`
- Modify: `src/agents/tools/crm/get_deal_details.py`
- Test: `tests/api/routers/crm/test_stakeholders_handler.py`
- Test: `tests/service/crm/insights/test_stakeholders_service.py`
- Test: `tests/api/routers/crm/test_contacts_handler.py`

- [x] Read stakeholders from `CRMDealParticipant` with:

```text
organisation_id == requested organisation
deal_id == requested deal
participant_type == CONTACT
is_deleted == False
```

- [x] Do not filter by `is_stakeholder`.
- [x] Return public `participant_id` as the canonical stakeholder route/detail identity.
- [x] Use participant snapshots for list views.
- [x] Left join `CRMContact` for detail/edit paths that need fresh canonical profile data.

Implementation note:
- Active stakeholder list/detail reads, contact deal involvement reads, available-contact
  service reads, and CRM agent read tools now use participant-backed rows. The
  `available-contacts` and `add-from-contacts` API routes are currently commented out in
  `stakeholders_handler.py`; do not treat those 405 route tests as Task 6 read-cutover
  failures.

### Task 7: Move Stakeholder Writes To Participants

**Files:**
- Modify: `src/service/crm/insights/stakeholders_service.py`
- Modify: `src/service/bots/form_handlers/add_stakeholder_handler.py`
- Modify: `src/agents/tools/crm/upsert_stakeholder.py`
- Modify: `src/agents/tools/crm/add_stakeholder.py`
- Modify: `src/agents/tools/crm/create_stakeholder_if_missing.py`
- Modify: `src/agents/tools/README.md`
- Test: `tests/service/agents/test_stakeholder_tools.py`
- Test: stakeholder API router tests.

- [x] Create/update `CRMContact` before writing contact profile fields.
- [x] Create/reuse a single participant row for the deal/contact/email.
- [x] Write only deal-specific fields to the participant.
- [x] Return only the canonical participant ID in public response payloads:

```json
{
  "participant_id": "<participant-id>"
}
```

- [x] Keep `stakeholder_id` as the argument name for existing agent/job workflows where changing the tool schema is out of scope, but resolve it as `CRMDealParticipant.participant_id` and return `participant_id` in tool outputs.

Implementation note:
- Manual API creates, updates, approve/reject/remove, add-from-contacts service logic,
  add-stakeholder agent/form flows, and direct upsert/create-if-missing agent helpers now
  resolve stakeholders through active `CONTACT` participants. Profile fields write to
  `CRMContact` when linked, participant snapshots are refreshed for list reads, and public
  responses expose `participant_id` without a duplicate `stakeholder_id`.

---

## Phase 4: Worker, Job, Insight, Timeline, And Privacy Cutover

### Task 8: Move Research And Analysis To Participants/Contacts

**Files:**
- Modify: `src/workers/deal_worker/tasks/deal_insight_tasks/stakeholder_research_tasks.py`
- Modify: `src/workers/deal_worker/utils/stakeholder_research_dispatch.py`
- Modify: `src/workers/deal_worker/services/deal_insight_services/stakeholder_research_service.py`
- Modify: `src/workers/deal_worker/services/deal_insight_services/stakeholder_analysis_service.py`
- Modify: `src/agents/tools/crm/enrich_stakeholder.py`
- Modify: `src/agents/tools/research/research_stakeholder.py`
- Test: `tests/workers/deal_worker/tasks/deal_insight_tasks/test_stakeholder_research_tasks.py`
- Test: `tests/workers/deal_worker/utils/test_stakeholder_research_dispatch.py`
- Test: `tests/workers/deal_worker/services/test_stakeholder_analysis_service.py`

- [x] Lookup research targets by `CRMDealParticipant.participant_id`.
- [x] Require `participant_type == CONTACT` and `is_deleted == False`.
- [x] Persist reusable person research/enrichment to `CRMContact.contact_research` and `CRMContact.last_researched_at`.
- [x] Persist deal-scoped research display values to `CRMDealParticipant.stakeholder_research` and `CRMDealParticipant.last_researched_at`.
- [x] Update participant snapshots from contact after enrichment.
- [x] Stakeholder analysis updates only accepted deal behavior fields:

```text
stance
decision_power
motivation
pain_points
keywords
temperature
```

Implementation note:
- Research dispatch, retry sweep, research service, analysis service, and Apollo-backed
  research/enrichment tools now resolve their existing `stakeholder_id` input as
  `CRMDealParticipant.participant_id` for active contact participants and return
  `participant_id` in outputs. Reusable profile
  research writes to `CRMContact`; deal-scoped display research and snapshots write to
  `CRMDealParticipant`. The `DealJobs.stakeholder_id` column name is still a compatibility
  name until Task 9 migrates/renames the jobs and insight surfaces.

### Task 9: Move Jobs, Insights, Timeline, Mapper Utilities, And Privacy

**Files:**
- Modify: `src/domain/models/organisation_tables/job_models.py`
- Modify: `src/domain/models/organisation_tables/sync/stakeholder_insight_models.py`
- Modify: `src/service/crm/jobs/deal_job_service.py`
- Modify: `src/service/crm/jobs/stakeholder_job_visibility_service.py`
- Modify: `src/service/crm/insights/stakeholder_insight_service.py`
- Modify: `src/service/crm/timeline/deal_timeline_sync_service.py`
- Modify: `src/workers/deal_worker/services/deal_mapper_services/stakeholder_sync_service.py`
- Modify: `src/workers/deal_worker/services/deal_mapper_services/deal_meeting_matcher.py` for calendar-event matching; this checkout has no separate `deal_calendar_matcher.py`.
- Modify: `src/workers/deal_worker/services/deal_mapper_services/deal_email_matcher.py`
- Modify: `src/workers/deal_worker/services/deal_mapper_services/deal_meeting_matcher.py`
- Modify: `src/workers/deal_worker/services/stakeholder_lookup_service.py`
- Modify: `src/service/calendar_invites/attendee_resolver.py`
- Modify: `src/service/integrations/webhooks/hubspot/contacts/privacy_deletion.py`
- Test: focused stakeholder mapper, job, timeline, insight, and privacy tests.

- [x] New `DealJobs` and `DealStakeholderInsight` rows write participant IDs.
- [x] Migrate existing old stakeholder UUID references to matching participant IDs before participant-only reads are enabled.
- [x] Mapper utilities read stakeholder emails from active contact participants.
- [x] Privacy deletion scrubs `CRMContact` profile PII and participant snapshots/research.
- [x] Keep old `DealStakeholder` privacy scrub while `deal_stakeholders` remains in production.

Implementation note:
- `DealJobs.stakeholder_id` and `DealStakeholderInsight.stakeholder_id` remain
  transition column names, but their model FKs no longer point at
  `deal_stakeholders`. Existing old stakeholder UUID references are rewritten in
  the existing `060` migration by matching `deal_stakeholders` to active contact
  participants by source participant ID, contact ID, or normalized email.
- Timeline stakeholder events now use active approved contact participants and
  store `CRMDealParticipant.participant_id` as both `source_record_id` and the
  timeline-compatible `stakeholder_id` metadata value.
- Email/meeting matcher contact rosters and last-contact updates read/write
  active approved contact participants. Email-discovered and calendar-discovered
  stakeholders are now persisted as contact participants, and research dispatch
  is queued with participant IDs.
- Calendar invite "deal stakeholders" expansion searches active approved contact
  participants while preserving the public `deal_stakeholder` source label.
- HubSpot privacy deletion now clears reusable contact research and participant
  profile snapshots, deal behavior fields, and deal-scoped research. After the
  runtime cutover, it does not update stale `deal_stakeholders` rows.

---

## Phase 5: Retire `deal_stakeholders`

### Task 10: Stop Runtime References

**Files:**
- Modify: remaining runtime files found by `rg -n "DealStakeholder|deal_stakeholders" src tests`
- Test: focused stakeholder, mapper, insight, timeline, privacy, and agent-tool suites.

- [x] Run:

```bash
rg -n "DealStakeholder|deal_stakeholders" src tests
```

Expected remaining references before table drop:

```text
migration files
backfill/audit scripts
product/API/tool/task names that still say stakeholder while using participant IDs
existing job/insight/timeline compatibility fields named stakeholder_id while storing participant IDs
DealStakeholderInsight model/table names, whose stakeholder_id now stores participant IDs
tests that assert stakeholder API payloads do not include stakeholder_id
```

- [x] Add temporary production validation comparing active old rows to active contact participants.
- [x] Leave `deal_stakeholders` as stale legacy data until production validation and final table retirement.

Implementation note:
- Runtime old-table imports/queries/writes were removed from stakeholder sync, deal job
  utilities, timeline detail enrichment, deal-note mention participant expansion, and
  HubSpot privacy deletion. The stricter guard in
  `tests/test_deal_stakeholder_runtime_references.py` permits only the old model definition
  until table retirement.
- `scripts/validate_deal_stakeholder_participant_cutover.py` compares active approved
  `deal_stakeholders` rows with active approved contact participants per deal, reports
  old stakeholder rows that do not match any participant by ID/contact/email, and reports
  active contact participants that are not backed by an active `CRMContact`.
- Raw `rg -n "DealStakeholder|deal_stakeholders" src tests` still finds public product names,
  Celery task names, tool names, `DealStakeholderInsight`, tests proving no old table access,
  and the legacy SQLModel class. These are not runtime reads or writes of the stale
  `deal_stakeholders` source table.

### Task 11: Defer Old Table Drop

**Files:**
- Modify: `src/domain/models/organisation_tables/sync/crm/stakeholders.py`
- Modify: docs under `docs/API Documentation/deals/`
- Modify: docs under `docs/Technical Documentation/deal_mappings/`

- [x] Do not add a drop-table migration in this participant cutover release.
- [x] Keep the `DealStakeholder` model available while the physical table remains in the database.
- [x] Stop runtime code from reading, writing, or privacy-scrubbing `deal_stakeholders`; it is stale legacy source data after cutover.
- [ ] Run production validation and explicit data migration before a later table-retirement release.
- [x] Update docs to explain that stakeholder API payloads use `participant_id` as the canonical `CRMDealParticipant.participant_id`.

Implementation note:
- No `063` drop migration is included in this branch.
- `DealStakeholder` remains importable for migration/backfill/validation code while the
  physical table exists, but runtime guards prevent app paths from importing or querying it.
- Runtime privacy deletion scrubs contacts, activities, participant snapshots, and deal
  primary-contact fields only. It intentionally does not update `deal_stakeholders`.
- Final table drop/model removal is a later release after production row parity and data
  migration are validated.

---

## Verification Commands

Run data preflight before applying the org schema migration in any environment
that already has participant rows. The `060` migration creates partial unique
indexes over active contact participants, so duplicate audit failures must be
cleaned before Alembic upgrade reaches `060`.

```bash
uv run python -m scripts.audit_duplicate_deal_contact_participants --organisation-id <org_id> --fail-on-duplicates
uv run alembic -c migrations/org_migrations/alembic.ini heads
uv run python -m scripts.run_org_migrations <org_id> --target-version head --fail-fast
uv run python -m scripts.backfill_participant_stakeholder_fields --organisation-id <org_id> --dry-run
uv run python -m scripts.backfill_participant_stakeholder_fields --organisation-id <org_id> --apply
uv run python -m scripts.validate_deal_stakeholder_participant_cutover --organisation-id <org_id> --only-mismatches --fail-on-mismatch
```

For all-org rollout, run the same audit/backfill/validation sequence per
organisation before promoting the release broadly. Do not apply `060` to an org
that reports duplicate active contact participants.

Run focused suites after each phase:

```bash
uv run pytest tests/domain/models/organisation_tables/sync/crm/test_crm_deal_participant_stakeholder_fields.py -v
uv run pytest tests/service/crm/insights/test_participant_stakeholder_adapter.py -v
uv run pytest tests/scripts/test_backfill_participant_stakeholder_fields.py -v
uv run pytest tests/api/routers/crm/test_stakeholders_handler.py tests/api/routers/crm/test_stakeholder_phone_number.py -v
uv run pytest tests/service/crm/insights/test_stakeholders_service.py -v
uv run pytest tests/api/routers/crm/test_contacts_handler.py -v
uv run pytest tests/service/crm/test_deal_participant_service_internal_ids.py tests/service/crm/test_deal_write_service_account_updates.py tests/service/crm/test_leads_access_service.py -v
uv run pytest tests/workers/deal_worker/services/test_stakeholder_sync_service.py tests/workers/deal_worker/tasks/deal_mappers/test_stakeholder_tasks.py -v
uv run pytest tests/workers/deal_worker/services/test_deal_calendar_matcher.py tests/workers/deal_worker/services/test_deal_email_matcher.py tests/workers/deal_worker/services/test_deal_meeting_matcher.py -v
uv run pytest tests/workers/deal_worker/tasks/deal_insight_tasks/test_stakeholder_research_tasks.py -v
uv run pytest tests/workers/deal_worker/utils/test_stakeholder_research_dispatch.py -v
uv run pytest tests/service/agents/test_stakeholder_tools.py -v
uv run pytest tests/service/integrations/crm/records/hubspot/deals/test_deal_child_source_mappings.py tests/service/integrations/crm/records/salesforce/deals/test_deal_child_source_mappings.py -v
uv run pytest tests/service/integrations/webhooks/hubspot/contacts/test_privacy_deletion.py -v
```

Run static reference checks before claiming the runtime cutover is complete:

```bash
rg -n "legacy_stakeholder_id|canonical_stakeholder_participant_id|old_stakeholder_id" src/domain src/service src/workers src/agents tests
rg -n "is_stakeholder" src/domain src/service src/workers src/agents tests
rg -n "stakeholder_created_by_user_id|stakeholder_source|stakeholder_source_reference_id" src/domain src/service src/workers src/agents tests
rg -n "stakeholder_data|stakeholder_engagement_history|champion_potential|engagement_score" src/domain src/service src/workers src/agents tests
rg -n "CRMDealParticipant\\(|\\.properties\\s*=|properties=\\{" src tests
rg -n "DealStakeholder|deal_stakeholders" src tests
```

Expected before old-table retirement:

```text
No runtime/model/migration/test path depends on legacy stakeholder aliases.
No runtime/model/migration/test path depends on is_stakeholder.
No runtime/model/migration/test path depends on removed stakeholder-owned blobs or scores.
No CRMDealParticipant construction or participant assignment relies on properties.
DealStakeholder/deal_stakeholders references remain only in the legacy model,
migration/backfill/validation code, product/API/task names, and tests that assert
the stale table is not used by runtime.
```

Before pushing or opening a PR, run repo-required quality checks:

```bash
uv run ruff check src/ tests/
uv run mypy src/
```

---

## Rollback Strategy

1. Phase 1 is additive and rollback-safe: old code ignores new participant/contact columns.
2. Backfill never deletes `deal_stakeholders`; rollback is switching reads back before read cutover.
3. Read/write cutover must not depend on old `DealStakeholder.stakeholder_id` at runtime.
4. Public stakeholder payloads use `participant_id`; compatibility `stakeholder_id` fields are limited to existing job/insight/timeline schemas and tool input names.
5. Writer hardening is rollback-safe because it reuses one active contact participant row instead of introducing a second marker system.
6. The schema migration containing uniqueness indexes must be applied only after duplicate audits return zero blocking rows in the target environment.
7. Old-table drop must be a separate release after production validation.

---

## Success Criteria

1. Stakeholder list/detail APIs work when only `crm_deal_participants` rows exist.
2. Manual, CRM, calendar-discovered, and email-discovered stakeholders all persist to participants.
3. A deal cannot have two active contact participants for the same contact or normalized email.
4. Calendar, email, and meeting deal matching read participant-backed stakeholder emails.
5. Stakeholder research writes reusable research to contacts and deal-scoped display research to participants.
6. Stakeholder analysis writes only accepted participant behavior fields.
7. New `deal_jobs` and stakeholder insights store participant IDs.
8. API callers receive `participant_id` as the stakeholder row UUID.
9. `deal_stakeholders` receives no new writes after the participant cutover.
10. Existing old stakeholder UUID references are rewritten to participant IDs where a matching participant exists.
11. Participant-backed create/readd paths are race-safe under duplicate normalized email writes and use savepoint recovery instead of broad session rollback.
12. Provider and internal participant writers preserve app-owned stakeholder fields while updating association fields.
13. Manual/API/agent stakeholders on external CRM deals work without requiring `participant.integration_id == deal.integration_id`.
14. `add_contacts_as_stakeholders()` writes participant-backed stakeholders and does not require `DealStakeholder` inserts.
15. No runtime code stores or resolves a persisted old stakeholder UUID alias.
16. No runtime code depends on `is_stakeholder`.
17. Focused stakeholder, contact, mapper, insight, timeline, privacy, participant-writer, and agent-tool suites pass.
