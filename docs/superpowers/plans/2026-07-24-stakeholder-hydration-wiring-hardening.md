# Stakeholder-to-communication hydration hardening

## Goal

Finish the consolidation of stakeholder, email, and calendar deal mapping without
reintroducing separate mapper services or visible jobs.

After this work:

- one user-visible `stakeholder_sync` job represents the complete stakeholder add flow;
- that job completes only after deal communication hydration commits;
- every membership change that can affect matching triggers one deal-scoped replay;
- duplicate writes do not enqueue duplicate replay;
- research that adds a previously missing email can recover historical communication;
- old email/calendar task names remain retired.

## Non-goals

- Do not restore `deal.match_email_to_deals` or `deal.match_calendar_to_deals`.
- Do not add another table, queue, service, or DealJob type.
- Do not add a created-date or rolling-window filter.
- Do not change the matching rule in `CommunicationDealAssociationService`.
- Do not change attachment extraction beyond the already-approved attachment plan.
- Do not trigger replay for stakeholder profile/role edits that cannot affect membership.

## Required business behavior

The association rule remains:

1. The communication contains an approved stakeholder on the deal (stored as a
   `CONTACT` deal participant).
2. The communication contains the deal owner or an active internal deal
   participant (stored as a `USER` deal participant).
3. Email may map to every deal satisfying the rule.
4. Calendar events require one unambiguous deal.

Deal replay uses all retained active communication for the deal's approved contacts.
There is no deal-created-date or 30/90-day boundary.

## Resolved prerequisite: participant activity-link role

Live verification found hydration failing before association because it writes the
intentional `PARTICIPANT` link role while the database check constraint omitted that
value. Migration `081` adds `PARTICIPANT` to the allowed roles.

`STAKEHOLDER` is not an activity-link object or role. Stakeholder membership remains in
`crm_deal_participants`; activity links target the canonical `CONTACT`, while
`STAKEHOLDER_ADDED`, `STAKEHOLDER_REMOVED`, and `STAKEHOLDER_UPDATED` remain activity
source types.

## Target flow

```text
stakeholder/contact membership committed
  -> stakeholder.sync_deal_stakeholders
  -> sync canonical participant/contact state
  -> deal.hydrate_contact_communications(deal_id, visible_job_id?)
  -> reconcile associations invalidated by participant removal
  -> replay retained emails and calendar events
  -> commit associations and activity projections
  -> dispatch required post-commit follow-ups
  -> complete the one visible stakeholder_sync job
```

Source-driven email/calendar sync continues to call hydration directly with changed
source IDs and no visible stakeholder job.

## Task 1: Make the visible job own hydration completion

### Problem

`stakeholder.sync_deal_stakeholders` currently marks its visible job complete after it
publishes hydration. The hydration task has no visible job ID. The UI can therefore show
complete while email/calendar association is still queued or later fails.

### Changes

- Add an optional `visible_job_id` to:
  - `dispatch_contact_communication_hydration_async`
  - `deal.hydrate_contact_communications`
- Pass the stakeholder job ID when stakeholder sync dispatches deal replay.
- When a visible job ID is passed:
  - stakeholder sync must leave the job `in_progress` after hydration is accepted;
  - hydration becomes the terminal owner of that job;
  - hydration publishes a `mapping_communications` progress stage;
  - hydration marks the job completed only after its transaction commits and required
    post-commit publication succeeds;
  - exhausted hydration retries mark the job failed and publish the existing SSE error
    and finalize frames.
- If stakeholder sync is skipped, does not require rematching, or runs with
  `trigger_rematching=False`, stakeholder sync remains the terminal owner.
- Hydration invoked by Gmail, calendar, imports, contact creation, or background replay
  without `visible_job_id` keeps its current non-SSE behavior.

### Files

- `src/workers/deal_worker/utils/contact_communication_hydration_dispatch.py`
- `src/workers/deal_worker/tasks/deal_mappers/stakeholder_tasks.py`
- `src/workers/deal_worker/tasks/deal_mappers/contact_communication_hydration_tasks.py`
- `src/workers/deal_worker/tasks/deal_mappers/visible_job_tracking.py`
- focused tests for those files

### Acceptance checks

- A stakeholder job does not publish `complete` merely because hydration was queued.
- Successful hydration publishes one terminal completion for the same stakeholder job ID.
- Exhausted hydration failure marks that same job failed.
- Source-driven hydration without a job ID creates no visible job or SSE stream.
- A zero-match hydration result still completes successfully.

## Task 2: Replay after stakeholder removal

### Problem

The canonical participant delete endpoint triggers stakeholder sync, but the legacy
stakeholder delete endpoint only soft-deletes the row. Existing email/meeting associations
can remain active until a later scheduled replay.

### Changes

- After the legacy stakeholder removal transaction commits, publish
  `stakeholder.sync_deal_stakeholders` with:
  - `change_type=PARTICIPANT_REMOVED`
  - `trigger_rematching=True`
  - the affected organisation and deal IDs.
- Preserve successful HTTP removal if publication fails; log the failure because the
  scheduled organisation sync remains the recovery path.
- Do not dispatch replay from stakeholder PATCH. Its editable fields do not include email,
  `contact_id`, approval status, or membership, so it cannot change the association rule.

### Files

- `src/api/routers/crm/stakeholders_handler.py`
- `tests/api/routers/crm/test_stakeholders_handler.py`

### Acceptance checks

- Removing through either participant API surface queues one deal-scoped replay.
- The replay removes associations that no longer satisfy the contact/internal-participant
  rule.
- Profile-only PATCH does not queue mapping work.

## Task 3: Recover mapping when research fills an empty email

### Problem

Provider research can fill `CRMContact.email` when it was previously empty. A linked
participant may still have no `related_email`, and no stakeholder sync/hydration is
currently triggered from research completion.

### Changes

- In the contact research workflow, compare the contact's normalized primary email before
  and after provider persistence.
- Only when the email changes from empty to non-empty:
  - load distinct active deals containing that contact through
    `CRMDealParticipant.related_local_id`;
  - publish one `stakeholder.sync_deal_stakeholders` task per affected deal with
    `change_type=PARTICIPANT_UPDATED` and `trigger_rematching=True`.
- Stakeholder sync remains responsible for copying canonical contact identity to the
  participant before hydration. Do not dispatch hydration directly from research.
- Do not dispatch for cached research, summary-only changes, unchanged email, deleted
  contacts, or contacts that are not active deal participants.
- Research completion must remain successful if replay publication fails; log affected
  deal IDs for operational retry.

### Files

- `src/workers/deal_worker/services/deal_insight_services/contact_research_service.py`
- focused contact research workflow tests

### Acceptance checks

- Empty email becoming populated queues each affected deal exactly once.
- Existing or unchanged email queues nothing.
- Multiple participant rows for the same deal still produce one task.
- The subsequent stakeholder sync updates participant identity before hydration runs.

## Task 4: Do not replay an unchanged stakeholder

### Problem

Manual stakeholder create is an upsert. API and agent callers currently infer that
processing is needed from `stakeholder.email`; submitting an already-active stakeholder
can therefore enqueue another complete replay.

### Changes

- Use the existing `StakeholderWriteResult` contract as the write boundary.
- Define `created=True` for:
  - a newly inserted participant;
  - reactivation of a soft-deleted participant.
- Define `created=False` for reuse/update of an already-active participant.
- Preserve the existing stakeholder detail DTO in API/agent responses.
- Queue stakeholder processing only when:
  - `created=True`; and
  - the resolved stakeholder has an email-backed contact identity.
- Keep contact-research cooldown behavior independent from mapping dispatch.

### Files

- `src/service/crm/participants/contracts.py`
- `src/service/crm/participants/stakeholder_write_service.py`
- `src/api/routers/crm/stakeholders_handler.py`
- `src/agents/tools/crm/add_stakeholder.py`
- direct service, API, and agent tests

### Acceptance checks

- New and reactivated email-backed stakeholders queue processing.
- Re-submitting an active stakeholder returns the existing row without replay.
- Name-only stakeholders remain valid placeholders but queue no mapping.
- LinkedIn/contact resolution that produces an email queues normally.

## Task 5: Remove duplicate replay during deal creation

### Problem

Deal creation with inline contacts currently starts stakeholder processing for each added
participant and also publishes one direct `deal_created` hydration. This can replay the
same deal multiple times.

### Changes

- Keep the single existing deal-scoped `deal_created` hydration after all inline
  participants commit.
- Do not create per-participant stakeholder-processing jobs from the deal-create route.
- Keep contact research dispatch for each newly added contact.
- Do not change standalone stakeholder-add behavior.

### Files

- `src/api/routers/crm/deals_handler.py`
- `tests/api/routers/crm/test_deals_handler.py`

### Acceptance checks

- Creating a deal with any number of inline contacts publishes one deal hydration.
- The route creates no redundant stakeholder processing chain.
- Each newly added contact still gets the existing research dispatch.
- Deal creation without contacts publishes no unnecessary replay.

## Task 6: Align recovery, documentation, and deployment

### Changes

- Keep legacy `DEAL_EMAIL_REMATCH` and `DEAL_CALENDAR_REMATCH` enum values and stale-job
  cleanup support for historical rows.
- Do not recover or republish those retired task names.
- Update API and technical documentation from three stakeholder jobs to one
  `stakeholder_sync` job whose completion includes hydration.
- Update stale comments that still say email/calendar matchers remain.
- Before deploying workers:
  - confirm no active producer references the retired task names;
  - inspect/drain old queued mapper tasks or accept their retirement explicitly;
  - deploy producers and workers in a sequence that does not publish names unavailable to
    the active worker version.

### Files

- `docs/API Documentation/deals/stakeholders-api.md`
- `docs/API Documentation/deals/crm-deals-api.md`
- `docs/Technical Documentation/deal/stakeholder-job-visibility.md`
- `src/workers/deal_worker/tasks/deal_mappers/__init__.py`
- related contract tests

### Acceptance checks

- Documentation exposes one visible job and accurate SSE completion semantics.
- Repository search finds no active producer for:
  - `deal.match_email_to_deals`
  - `deal.match_calendar_to_deals`
- Historical job rows remain readable and eligible for stale cleanup.

## Test order

Implement with focused regression tests in this order:

1. visible job remains active until hydration completes;
2. hydration failure marks the visible job failed;
3. stakeholder removal queues replay;
4. newly researched email queues distinct affected deals;
5. duplicate stakeholder create does not queue replay;
6. deal creation publishes one replay;
7. existing association and hydration suites remain green.

Suggested final checks:

```powershell
uv run pytest tests/service/crm/test_stakeholder_job_visibility_service.py `
  tests/workers/deal_worker/tasks/deal_mappers/test_stakeholder_tasks.py `
  tests/workers/deal_worker/tasks/deal_mappers/test_contact_communication_hydration_tasks.py `
  tests/api/routers/crm/test_stakeholders_handler.py `
  tests/api/routers/crm/test_deals_handler.py `
  tests/workers/deal_worker/services/test_contact_research_service.py -q

uv run ruff check src tests
uv run ruff format --check src tests
git diff --check
```

If the contact research test file has a different current name, use the existing focused
workflow test module rather than creating a duplicate test surface.

## Completion definition

The work is complete when one stakeholder add produces at most one replay, its visible job
truthfully covers hydration through terminal success/failure, all membership-changing
entry points converge on the same deal-scoped path, and no retired matcher task is
published.
