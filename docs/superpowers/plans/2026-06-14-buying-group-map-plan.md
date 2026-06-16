# Buying Group Map Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a durable buying-group map for a deal that can classify known stakeholders and
represent missing stakeholders without pretending that a missing person is a
`CRMDealParticipant`.

**Architecture:** Keep `crm_deal_participants` as the canonical table for real deal roster
entities. Add a separate app-owned buying-group map table for role coverage state, with nullable
links to participants or contacts when the slot maps to a known person.

**Tech Stack:** Python 3.11, FastAPI, SQLModel/SQLAlchemy, PostgreSQL JSONB, Alembic org
migrations, Celery deal worker.

---

## Current Repo Grounding

`crm_deal_participants` is currently the canonical deal roster table. It stores real deal-linked
entities such as:

- `contact`: external person on the deal.
- `company`: account/company linked to the deal.
- `user`: internal CRM/user collaborator.

The table also now owns stakeholder-facing fields for contact participants, including stance,
decision power, motivation, pain points, temperature, approval status, and stakeholder research.
Stakeholder API/service code treats active `contact` participants as the canonical stakeholder
rows.

There is already a separate `deal_stakeholder_insights` table for AI-generated deal insights. It
supports insight types such as `MISSING_DECISION_MAKER` and `COVERAGE_GAP`, but it is currently
an insight/notification lifecycle, not a stable buying-map model.

## Product Boundary

A buying-group map is not a fourth participant type.

It is a deal-level assessment over:

1. known contact participants;
2. known CRM contacts that are not yet deal participants;
3. inferred missing stakeholder slots where no person is known yet.

This means `participant_type="buying_map"` would be the wrong abstraction. It would force a map
slot into a table whose core identity is "this deal is linked to this real CRM entity". Missing
stakeholders do not have a real entity identity yet.

## Recommended Architecture

Introduce an app-owned buying-group map table, tentatively named
`deal_buying_group_members`.

Each row represents one role slot in the buying group for one deal. A slot may point to a known
participant or remain unresolved:

```text
deal_buying_group_members
- buying_group_member_id uuid primary key
- organisation_id uuid not null
- deal_id uuid not null
- participant_id uuid null
- contact_id uuid null
- role_key varchar(64) not null
- role_label varchar(128) not null
- member_status varchar(32) not null
- confidence numeric/float not null
- evidence jsonb not null
- rationale text null
- suggested_name varchar(255) null
- suggested_title varchar(255) null
- suggested_department varchar(128) null
- suggested_email varchar(255) null
- source_type varchar(64) not null
- source_record_id varchar(512) null
- generated_by varchar(64) null
- identified_at timestamptz not null
- resolved_at timestamptz null
- dismissed_at timestamptz null
- created_at timestamptz not null
- updated_at timestamptz not null
```

Suggested `member_status` values:

- `KNOWN`: role is mapped to an active contact participant.
- `POTENTIAL`: role maps to a known CRM contact, but that contact is not yet on the deal.
- `MISSING`: role is inferred as needed, but no specific person is known.
- `DISMISSED`: user/agent dismissed the slot as not relevant.
- `RESOLVED`: formerly missing or potential slot has been addressed.

Suggested `role_key` values for v1:

- `economic_buyer`
- `decision_maker`
- `champion`
- `technical_evaluator`
- `business_evaluator`
- `legal_procurement`
- `finance_approver`
- `end_user`
- `coach`
- `blocker`
- `unknown`

The v1 list should be intentionally small. Agent output can use `unknown` when evidence is weak
instead of inventing authority from job title alone.

## Relationship To `CRMDealParticipant`

Known stakeholders remain in `crm_deal_participants` as `contact` rows. The buying-map row points
at them through `participant_id`.

The participant row can still keep per-person behavioral fields:

- `stance`
- `decision_power`
- `motivation`
- `pain_points`
- `temperature`
- `stakeholder_research`

The buying-map row owns map-specific state:

- the role slot being evaluated;
- why this role matters for this deal;
- whether the role is known, potential, or missing;
- evidence and confidence for the classification;
- lifecycle state for resolving/dismissing gaps.

Do not duplicate every participant field into the buying map. Keep profile and engagement data on
the participant/contact; keep coverage/classification state on the map row.

## Relationship To `deal_stakeholder_insights`

Use `deal_stakeholder_insights` for notification cards and user-visible alerts.

Use `deal_buying_group_members` for durable map state.

Examples:

- A `MISSING` `economic_buyer` map row can create or update one active
  `MISSING_DECISION_MAKER` insight.
- A `MISSING` `legal_procurement` or `finance_approver` map row can create or update a
  `COVERAGE_GAP` insight.
- Dismissing the insight should not delete the map row; it should update the map row lifecycle
  only if the user explicitly dismisses that role gap.

This avoids using an alert table as the source of truth for a structured map.

## Agent Workflow

Create a buying-group analysis workflow that is evidence-first:

1. Load deal details and stage.
2. Load active contact participants and their stakeholder fields.
3. Load recent meetings, transcripts, notes, emails, and existing stakeholder insights.
4. Classify known participants into role slots only when there is evidence.
5. Detect missing slots based on deal context, stage, product complexity, and observed buying
   process evidence.
6. Upsert buying-map rows by `(organisation_id, deal_id, role_key, participant_id)` for known
   slots and by `(organisation_id, deal_id, role_key, member_status='MISSING')` for unresolved
   missing slots.
7. Create/update stakeholder insights only for actionable gaps.

Critical rule: title alone is insufficient evidence for authority. For example, "VP" may support
`potential economic buyer`, but the row should stay lower confidence unless meeting notes,
transcripts, emails, or CRM fields show budget/approval authority.

## API Shape

Add read endpoints first:

```text
GET /crm/deals/{deal_id}/buying-group-map
POST /crm/deals/{deal_id}/buying-group-map/analyze
PATCH /crm/deals/{deal_id}/buying-group-map/{buying_group_member_id}
```

Read response should group rows by status:

```json
{
  "known": [],
  "potential": [],
  "missing": [],
  "dismissed": []
}
```

Each item should include:

- buying group member ID;
- role key and label;
- status;
- confidence;
- linked participant summary when present;
- suggested person fields when unresolved;
- evidence summaries;
- rationale;
- timestamps.

The analyze endpoint should enqueue a deal-worker task and return a job reference rather than
holding an API request open during LLM analysis.

## Data Integrity Rules

Every query and write must include `organisation_id`.

Foreign-key candidates:

- `deal_id` should reference `crm_deals.deal_id`.
- `participant_id` should reference `crm_deal_participants.participant_id` when present.
- `contact_id` should reference `crm_contacts.contact_id` when present.

Indexes:

```text
ix_buying_group_org_deal_status
  (organisation_id, deal_id, member_status)

ix_buying_group_org_deal_role
  (organisation_id, deal_id, role_key)

ix_buying_group_participant
  (participant_id)
```

Recommended uniqueness:

```text
one active known role per participant:
  unique (organisation_id, deal_id, participant_id, role_key)
  where participant_id is not null and member_status in ('KNOWN', 'POTENTIAL')

one active unresolved missing slot per deal role:
  unique (organisation_id, deal_id, role_key)
  where participant_id is null and member_status = 'MISSING'
```

Do not make `suggested_email` unique. AI-inferred missing people may have partial or incorrect
identity, and shared/generic emails can be valid.

## Implementation Tasks

### Task 1: Schema and Model

Files:

- Create migration under `migrations/org_migrations/versions/`.
- Add model under `src/domain/models/organisation_tables/sync/crm/buying_group.py`.
- Export model from the relevant `__init__.py` files.

- [ ] Add the org migration for `deal_buying_group_members`.
- [ ] Add the SQLModel model and enums.
- [ ] Export the model so Alembic metadata can discover it.
- [ ] Add focused model/index tests.

Verification:

```bash
uv run alembic -c migrations/org_migrations/alembic.ini upgrade head
uv run pytest tests/domain -v -k "buying_group"
```

### Task 2: Contracts and Read Service

Files:

- Add contracts under `src/contracts/crm/buying_group.py`.
- Add service under `src/service/crm/insights/buying_group_map_service.py`.
- Add router under `src/api/routers/crm/buying_group_map_handler.py`.
- Register router in the CRM API route setup.
- Update `docs/API Documentation/` for the new endpoints.

- [ ] Add response/request DTOs for grouped map reads and lifecycle updates.
- [ ] Add org-scoped read service methods.
- [ ] Add read, analyze, and update endpoints.
- [ ] Document the API contract.

Verification:

```bash
uv run pytest tests/api/routers/crm -v -k "buying_group"
```

### Task 3: Analyzer Task

Files:

- Add deal-worker task under `src/workers/deal_worker/tasks/`.
- Add analysis service under `src/service/crm/insights/buying_group_analysis_service.py`.
- Reuse existing agent/tool contracts for deal details, stakeholders, meetings, notes, emails,
  and stakeholder insights.

- [ ] Add a Celery task that runs per deal.
- [ ] Add the analysis service that gathers evidence with fresh DB sessions around LLM calls.
- [ ] Add idempotent upsert logic for known, potential, and missing slots.
- [ ] Add tests for conservative evidence handling.

Verification:

```bash
uv run pytest tests/workers/deal_worker -v -k "buying_group"
uv run pytest tests/service/agents -v -k "stakeholder"
```

### Task 4: Insight Bridge

Files:

- Extend `StakeholderInsightType` usage if existing `MISSING_DECISION_MAKER` and
  `COVERAGE_GAP` are sufficient.
- Add service logic to create/update active insight rows from unresolved buying-map gaps.
- Keep insight lifecycle separate from map lifecycle unless the user explicitly resolves or
  dismisses the map slot.

- [ ] Map unresolved buying-group gaps to existing stakeholder insight types where possible.
- [ ] Upsert active insights from missing map rows.
- [ ] Preserve map rows when an insight is acknowledged or dismissed.
- [ ] Add lifecycle tests for map/insight independence.

Verification:

```bash
uv run pytest tests/api/routers/crm/test_stakeholder_insights_handler.py -v
uv run pytest tests/service -v -k "stakeholder_insight or buying_group"
```

### Task 5: Participant Synchronization Rules

Files:

- Update stakeholder participant creation/update paths only where needed to resolve existing
  `MISSING` or `POTENTIAL` map rows when a participant is added.
- Do not change provider participant writers unless the map needs to react to synced contacts.

- [ ] When a stakeholder is added, resolve matching missing/potential map rows through the service.
- [ ] Keep provider participant ingestion unchanged unless a focused test proves it must trigger map refresh.
- [ ] Add regression tests for existing stakeholder and participant write paths.

Verification:

```bash
uv run pytest tests/api/routers/crm/test_stakeholders_handler.py -v
uv run pytest tests/service/agents/test_stakeholder_tools.py -v
```

## Rejected Options

### Add `buying_map` To `CRMParticipantType`

Rejected because it makes a non-person, non-company assessment look like a CRM entity linked to a
deal. It would also break assumptions in services that resolve participants by contact/company/user
identity.

### Store Missing Stakeholders Only In `deal_stakeholder_insights`

Rejected because insights are alerts with acknowledgement, dismissal, resolution, and expiry. A
buying map needs a stable structured state that survives alert lifecycle changes.

### Add Many Buying-Role Columns To `crm_deal_participants`

Rejected for v1 because missing roles cannot live on a participant row. Columns such as
`is_economic_buyer` or `is_champion` also make multi-role and evidence tracking awkward.

## Open Product Questions

1. Should a known stakeholder be allowed to occupy multiple buying-group roles?
   Recommended answer: yes for v1, with one row per `(participant_id, role_key)`.

2. Should users be able to manually create a missing buying-map slot?
   Recommended answer: yes, but only after read/analyze is working.

3. Should resolving a missing slot automatically create a contact/stakeholder?
   Recommended answer: no. Resolution should link to an existing or newly created contact through
   the normal stakeholder/participant write path.

4. Should internal users appear in the buying group?
   Recommended answer: no for customer-side buying roles. Internal users can be shown separately as
   account team coverage if product wants it later.

## Success Criteria

The feature is ready when:

1. `crm_deal_participants` still contains only real roster entities.
2. A deal can return a structured buying-group map with known and missing roles.
3. Missing roles do not require fake participant rows.
4. Agent analysis can update the map idempotently.
5. Existing stakeholder and participant APIs still pass focused regression tests.
6. Stakeholder insights can surface actionable missing-role alerts without becoming the map source
   of truth.
