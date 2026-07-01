# Stakeholder Create Runtime Flow

This document explains what happens when a stakeholder is added to a deal.
It is operational on purpose: the goal is to make identity resolution, DB
lookups, upsert behavior, and post-commit jobs easy to reason about.

## Mental Model

There are two durable records involved:

- `crm_contacts`: the canonical person/profile record. Contact research belongs here.
- `crm_deal_participants`: the deal-local stakeholder row. Buying role, stance,
  deal-local snapshots, and soft-delete state belong here.

Create accepts these identity inputs:

- `contact_id`
- `email`
- `linkedin_url`
- `name`

The write path is now an upsert by strong identity. If the same deal already has
a participant matching contact ID, email, or LinkedIn, create updates/reactivates
that participant instead of returning a duplicate conflict.

Name-only creates a deal-local participant placeholder. It does not create a
contact and does not enqueue research.

The `approval_status` column is still written as `"APPROVED"` for compatibility,
but create no longer has an approval workflow. There is no pending/approve/reject
branch in the write path.

Contact-backed create enqueues `contact_research` when cached contact research
is missing. Unlinked email/LinkedIn create still uses temporary
`stakeholder_research` until that path is retired.

## Entry Points

### API

`POST /{deal_id}/stakeholders`

Handler:

`src/api/routers/crm/stakeholders_handler.py:create_stakeholder`

Write service:

`src/service/crm/participants/stakeholder_write_service.py:create_stakeholder`

### Agent Tools

These tools call the same write service, but they own their own job dispatch:

- `src/agents/tools/crm/add_stakeholder.py`
- `src/agents/tools/crm/upsert_stakeholder.py`
- `src/agents/tools/crm/create_stakeholder_if_missing.py`

Because the service resolves `contact_id`, `email`, and `linkedin_url`
independently, agents may overfill identity fields safely. If the fields point
to different contacts, the service rejects the request before any write.

## API Create Flow

### 1. Handler Validates And Calls Write Service

The API handler converts the request into `CreateStakeholderCommand`, then calls:

```text
StakeholderWriteService.create_stakeholder(deal_id, command, context)
```

No job is dispatched before the write service returns and the request session
commits.

### 2. Normalize Identity Fields

Inside `StakeholderWriteService.create_stakeholder`:

```text
email_normalized = normalize_email(request.email)
name = clean_optional_string(request.name)
linkedin_url = clean_optional_string(request.linkedin_url)
```

No DB call.

### 3. Resolve Strong Identity Claims

The service resolves every strong identity supplied by the caller independently.

Conditional DB reads:

| Input present | Method | DB read |
| --- | --- | --- |
| `contact_id` | `_find_contact_by_id` | `crm_contacts` by contact ID, org, active |
| `email` | `_find_contact_by_email` | `crm_contacts` by normalized email, org, active |
| `linkedin_url` | `_find_contact_by_linkedin_url` | `crm_contacts` by normalized LinkedIn URL, org, active |

Outcomes:

- Supplied `contact_id` does not exist in the org: return `404`.
- Multiple supplied fields resolve to different contacts: return `409`.
- One contact is resolved: it becomes canonical for profile fields.
- No contact is resolved: continue with the user-provided identity fields.

When a contact is resolved, contact name, email, title, phone, LinkedIn, and
department hydrate the participant snapshot.

### 4. Reject Internal Email

If an email is known at this point, the service calls:

```text
_is_internal_email(email_normalized)
```

That loads internal domains from the master organisation table:

```text
get_internal_domains(organisation_id)
```

Conditional DB read:

| Condition | DB read |
| --- | --- |
| email exists | `master.organisations.domain` |

If the email belongs to the internal org domain, create returns `403`.

### 5. Email Fallback Contact Creation

This step runs only when there is an email but no resolved contact.

```text
_ensure_contact_for_stakeholder(...)
```

Current behavior:

1. Checks internal email again.
2. Skips durable contact creation for personal domains like Gmail.
3. Calls `ContactWriteService.find_or_create_contact_by_email`.

Conditional DB calls in this fallback:

| Step | DB call |
| --- | --- |
| internal guard repeat | `master.organisations.domain` again |
| contact writer lookup | `crm_contacts` by email again |
| if contact is missing | ensure internal CRM integration |
| if contact is missing | resolve or create account for email domain |
| if contact is missing | insert `crm_contacts` |
| if contact is created/updated | write mutation/activity rows |

Important: this fallback intentionally uses the shared contact writer so contact
creation is centralized. The tradeoff is extra DB calls because the service has
already checked email once.

### 6. Find Existing Deal Participant

Before inserting a participant, the service checks whether the same deal already
has a matching participant by contact ID, email, or LinkedIn.

```text
_find_contact_participant(
    deal_id=deal_id,
    email=email_normalized,
    contact_id=contact_id,
    linkedin_url=linkedin_url,
    include_deleted=True,
)
```

Conditional DB read:

| Condition | DB read |
| --- | --- |
| at least one of contact ID/email/LinkedIn exists | `crm_deal_participants` by deal and identity |
| name-only | no DB read here because there is no dedupe key |

Outcomes:

- Existing active participant: update the same row and return it.
- Existing soft-deleted participant: reactivate the same row and return it.
- No participant: continue to create a new row.

Approval status is not a branch. Existing legacy pending/rejected/removed values
do not block create; the row is made active and `approval_status` is set to
`"APPROVED"` for compatibility.

### 7. Update Existing Participant

If a matching participant exists, create behaves as an upsert target:

```text
_apply_contact_profile_updates(...)
_apply_participant_behavior_updates(...)
existing_participant.is_deleted = False
existing_participant.approval_status = "APPROVED"
```

Behavior:

- Keeps the same `participant_id`.
- Adds `related_local_id` if a contact is now known.
- Updates email/name/profile snapshot from the resolved contact or request.
- Updates deal-local behavior fields such as stance and decision power.
- Copies cached `contact.contact_research` to `stakeholder_research` when present.
- Flushes, refreshes, reloads the linked contact, and returns the DTO.

This branch does not create a new stakeholder-added activity today. It is an
update/reactivation of an existing deal participant, not a fresh insert.

### 8. Create New Deal Participant

For a new participant, the service calls:

```text
_ensure_deal_participant(...)
```

This does more than insert the participant. It also ensures internal CRM setup.

DB calls inside `_ensure_deal_participant`:

| Step | DB call |
| --- | --- |
| ensure internal CRM integration | select `knowledge_integrations` |
| ensure default pipeline | select `crm_pipelines` |
| ensure default stages | select `crm_deal_stages` |
| validate deal belongs to org | select `crm_deals` |
| defensive participant dedupe | select `crm_deal_participants` if identity exists |
| create participant | insert `crm_deal_participants` |

The second participant dedupe is defensive, but in the API create path it
usually duplicates the earlier participant lookup.

The new participant is written with:

```text
participant_source = "CRM" if contact exists else "MANUAL"
approval_status = "APPROVED"
is_deleted = False
```

### 9. Copy Cached Contact Research

If the participant is contact-backed and the contact already has
`contact_research`, the service copies it onto the participant:

```text
participant.stakeholder_research = contact.contact_research
participant.last_researched_at = contact.last_researched_at
```

No DB read. This is written during the final flush.

### 10. Add Activity Event For New Participant

For the new-participant path, the service projects a stakeholder-added activity
in the same transaction:

```text
ActivityFeedProjectionService.upsert_stakeholder_added(...)
```

This writes a `crm_activities` row with `source_type = STAKEHOLDER_ADDED` and
activity links for the deal, contact when available, and actor user when known.

### 11. Flush, Refresh, And Build DTO

The service finishes the write:

```text
_finish_write(participant)
_get_participant_contact(participant)
_participant_detail(participant, contact=contact)
```

DB calls:

| Step | DB call |
| --- | --- |
| flush participant/contact/activity writes | writes pending rows |
| refresh participant | DB read for participant |
| load linked contact | `crm_contacts` by `related_local_id`, if linked |

Then the service returns `StakeholderDetail`.

### 12. Handler Commits

After the service returns:

```text
await service.session.commit()
```

Only after this commit does the handler create visible processing jobs or
enqueue research.

## Post-Commit Jobs

The handler dispatches jobs from the final returned stakeholder state. This is
true for both newly inserted participants and existing participants updated by
the create/upsert path.

### A. Visible Processing Jobs

If the returned stakeholder has an email, the API handler calls:

```text
StakeholderJobVisibilityService.create_stakeholder_processing_jobs(
    deal_id,
    stakeholder.participant_id,
)
```

This creates three `DealJobs` rows in one operation group:

1. email rematch
2. calendar rematch
3. stakeholder sync

DB calls:

| Step | DB call |
| --- | --- |
| add three job rows | insert `deal_jobs` |
| flush | write jobs |
| refresh each job | three job refresh reads |
| commit jobs | commit |

Then it dispatches a Celery chain on the `deal` queue.

### B. Contact Research

If the returned stakeholder has `contact_id` and no cached research, the API
handler calls:

```text
dispatch_contact_research_async(
    organisation_id=...,
    contact_id=stakeholder.contact_id,
    user_id=...,
    trigger="stakeholder_created",
)
```

DB calls in the dispatch helper:

| Step | DB call |
| --- | --- |
| load contact | `session.get(CRMContact, contact_id)` |
| active job dedupe | select active `CONTACT_RESEARCH` jobs |
| create research job | insert `deal_jobs` |
| activity feed projection | upsert activity projection |
| commit | commit |

Then it enqueues `contact_research.process_job` on `deal_jobs`.

### C. Temporary Stakeholder Research

If the returned stakeholder has email or LinkedIn but no contact and no cached
research, the API handler calls:

```text
dispatch_stakeholder_research_async(
    organisation_id=...,
    stakeholder_id=stakeholder.participant_id,
    deal_id=deal_id,
    user_id=...,
)
```

DB calls in the dispatch helper:

| Step | DB call |
| --- | --- |
| load participant | select `crm_deal_participants` |
| load linked contact | select `crm_contacts` if participant is linked |
| recent contact research reuse | select `crm_contacts` by email |
| active job dedupe | select active `STAKEHOLDER_RESEARCH` jobs |
| maybe copy cached research | update participant/contact |
| create research job | insert `deal_jobs` |
| commit | commit |

This path exists only as a bridge for unlinked placeholders.

## Approximate Call Counts By Scenario

These are approximate because flush/refresh, mutation events, account creation,
and activity projection can add extra SQL.

| Scenario | Pre-commit core reads | Writes before commit | Post-commit jobs |
| --- | ---: | ---: | --- |
| name only | about 4 | participant + activity | none |
| selected `contact_id` with email on contact | about 9 | participant + activity | processing jobs + contact research |
| email matches existing contact | about 9-11 | participant + activity, maybe contact update | processing jobs + contact research |
| email creates new contact | 12+ | contact + account side effects + participant + activity | processing jobs + contact research |
| LinkedIn matches existing contact | about 8-10 | participant + activity | contact research if contact has email/domain |
| LinkedIn-only placeholder | about 6-8 | participant + activity | temporary stakeholder research |
| existing active/deleted participant | about 4-8 | participant update/reactivation | based on final email/contact state |

The selected-contact path feels heavy because it can do all of this:

1. contact lookup by ID
2. internal-domain lookup after contact hydration
3. participant dedupe
4. internal integration lookup
5. pipeline lookup
6. stage lookup
7. deal ownership lookup
8. participant dedupe again
9. flush/refresh participant
10. reload contact for DTO
11. create three processing job rows
12. refresh three job rows
13. load contact again in contact research dispatch
14. active research job dedupe
15. insert contact research job and activity feed

If the participant already exists, the service skips the internal setup and
fresh insert path, so the request is materially cheaper.

## Why It Is Complex

The flow is doing four jobs at once:

1. resolve identity safely across `contact_id`, email, and LinkedIn
2. maintain contact ownership of durable profile/research fields
3. maintain deal-local participant ownership of buying-role state
4. create post-commit processing and research jobs

The complexity is mostly from safety and compatibility, not from the business
rule itself.

## Current Duplications To Simplify Later

These are good cleanup targets:

1. Email contact lookup runs twice on email fallback:
   `_find_contact_by_email` and then `ContactWriteService.find_or_create_contact_by_email`.

2. Internal-domain lookup can run twice:
   `create_stakeholder` checks it, then `_ensure_contact_for_stakeholder` checks it again.

3. Participant dedupe can run twice:
   `create_stakeholder` checks before calling `_ensure_deal_participant`, and
   `_ensure_deal_participant` checks again.

4. `_ensure_deal_participant` ensures pipeline and stages even though stakeholder
   participant creation only needs the internal CRM integration and deal ownership.

5. Research dispatch opens a new org session and reloads contact/participant
   after the API request already had those objects. This is clean after commit,
   but it costs extra reads.

## Suggested Simpler Shape

Target shape:

```text
1. Resolve identity once
   -> contact | no contact | conflict

2. Validate guards once
   -> internal email, personal domain

3. Ensure contact only if required
   -> email business domain fallback only

4. Find participant once
   -> existing update/reactivate | new create

5. Create or update participant
   -> no pipeline/stage checks in this path

6. Commit

7. Dispatch jobs by final identity state
   -> email => processing jobs
   -> contact_id => contact_research
   -> unlinked email/linkedin => temporary stakeholder_research
   -> name only => no jobs
```

This would preserve the current product behavior while reducing the common
selected-contact and email paths by several DB calls.
