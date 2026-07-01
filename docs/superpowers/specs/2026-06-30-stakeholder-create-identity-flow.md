# Stakeholder Create Identity Flow

## Purpose

This is the decision document for simplifying stakeholder creation before
rewriting `StakeholderWriteService.create_stakeholder()`.

The create surface has four possible person identifiers:

- `contact_id`
- `email`
- `linkedin_url`
- `name`

The goal is not minimum lines for its own sake. The goal is one boring flow where
each identifier has a clear job, contact/account automation still works, and weak
identity does not pollute durable CRM data.

## Short Recommendation

Keep the backend create command typed. Do not add a raw `identity` string to
`StakeholderWriteService`.

Frontend can keep one input box, but it should classify the input before submit:

- selected CRM contact -> `contact_id`
- email-looking text -> `email`
- LinkedIn profile URL -> `linkedin_url`
- everything else -> `name`

Agent tools may accept a raw `identity` because LLM tool calls are fuzzier, but
they should translate it to the same typed command before calling the service.

Agent tools may also accidentally fill all four typed fields. The service should
not blindly trust that payload. It should resolve strong identifiers
independently and reject contradictory identity claims before writing.

## Agent Multi-Field Payload Rule

Agents can send `contact_id`, `email`, `linkedin_url`, and `name` together. Treat
those as claims, not as a single coherent identity.

Resolution rule:

1. Load `contact_id` if present.
2. Resolve `email` to a contact if present.
3. Resolve `linkedin_url` to a contact if present.
4. If two strong identifiers resolve to different contacts, reject with a 409
   identity conflict.
5. If `contact_id` exists, it is authoritative only when email/LinkedIn do not
   contradict it.
6. If one strong identifier resolves, use that contact and hydrate from it.
7. If no strong identifier resolves and email exists, create/reuse contact from
   email.
8. If only LinkedIn exists, create a LinkedIn-only stakeholder placeholder.
9. If only name exists, create a name-only stakeholder placeholder.
10. Never use `name` to reject or merge identity. Use it only as a display fill
    when the resolved contact lacks a name.

## Sales-Rep View

For a sales rep, email is the best create input because it unlocks the account
and deal automation:

- contact dedupe
- account/domain creation
- email and calendar rematching
- stakeholder sync
- reusable contact research

LinkedIn is useful, but it is not equivalent to email. It can identify a person
and start enrichment, but it does not provide an email domain, so it cannot drive
the account automation path safely.

Name is only a note-level placeholder. It is useful for "I know Jane is involved"
but it is not strong enough to create a durable CRM contact or run matching.

Account and contact are not symmetric here. Account name-only creation can be
reasonable because company name is often the primary business object. Person
name is not strong enough identity for an automatic CRM contact from stakeholder
create. A contact module may later choose to support explicit draft contacts,
but stakeholder create should not create those silently.

## Current Code Evidence

- `CreateStakeholderRequest` and `CreateStakeholderCommand` already allow
  `contact_id`, `email`, `linkedin_url`, or `name`.
- `StakeholderWriteService.create_stakeholder()` currently resolves contact in
  this order: `contact_id`, then email, then LinkedIn.
- Email contact creation goes through
  `ContactWriteService.find_or_create_contact_by_email()`, which is good because
  that path also owns account resolution.
- `ContactCreateCommand` still requires email, so contact creation from
  LinkedIn-only or name-only is not a supported contact write contract.
- Stakeholder research can run with email or LinkedIn, but the structured
  stakeholder research output currently does not include email, so LinkedIn-only
  research cannot create or link a contact after enrichment without a contract
  change.
- The API handler currently dispatches post-create jobs only inside
  `if stakeholder.email`, so a frontend LinkedIn-only create does not dispatch
  research from the API path today. Agent tools already dispatch research for
  email or LinkedIn.

## Pushback On The Tempting Ideas

### "Name-only should create a contact with that name"

I would not do this.

A contact with only a name has no account key, no email key, weak dedupe, and no
reliable automation path. In a CRM, that creates noise faster than value. A
name-only stakeholder should be a deal-local participant placeholder:

- `CRMDealParticipant.related_name = name`
- `related_email = null`
- `related_local_id = null`
- source `MANUAL`
- approval `APPROVED`

The UI should show it as incomplete until email or LinkedIn is added.

This does not contradict name-only account creation. `Acme Corp` is useful as an
incomplete account because later enrichment can add domain, website, LinkedIn,
industry, and account context. `John Smith` is not enough to create a durable
person record from a stakeholder add because it has no account, no contact point,
and high duplicate risk.

### "LinkedIn should be treated as a name"

I would not do this.

LinkedIn is a stronger identifier than name and should be stored as
`linkedin_url`, used for exact contact lookup, used for same-deal dedupe, and
used for research dispatch.

Treating it as a name throws away the only useful structured identifier.

### "LinkedIn-only should immediately create a contact"

Not yet.

The sales automation you care about depends on email/domain. Creating a durable
contact from LinkedIn-only would create a contact with no email, no domain, weak
account association, and likely duplicate risk when the email appears later.

The production-safe path is:

1. search existing contacts by exact LinkedIn URL;
2. if found, link the stakeholder to that contact;
3. if not found, create a LinkedIn-only stakeholder participant;
4. dispatch research from LinkedIn;
5. only if enrichment later returns email, resolve/create the contact through
   the canonical email contact writer and link the participant.

Step 5 is not fully available today because stakeholder research output does not
return email.

### "LinkedIn create should synchronously call Apollo and reject if no email"

I would not make no-email a hard reject.

Apollo-first is useful, but vendor lookup is not identity truth. A missing email
can mean rate limit, provider coverage gap, private profile, temporary vendor
failure, or a profile that exists without a deliverable email. Rejecting the
stakeholder in all of those cases would block valid sales reps.

Better rule:

1. If LinkedIn URL format is invalid, reject before write.
2. If enrichment says the profile does not exist, return a clear invalid-profile
   error.
3. If enrichment finds email, resolve/create contact by email and create the
   stakeholder as contact-backed.
4. If enrichment finds a profile but no email, create a LinkedIn-only incomplete
   stakeholder and show it as incomplete.
5. If enrichment is unavailable or times out, create LinkedIn-only incomplete
   stakeholder and allow retry.

The synchronous version is acceptable only with a short timeout and a fallback
path. It should not make the write service depend on Apollo availability.

### "Create service should directly trigger research"

I would keep dispatch outside the write service.

`create_stakeholder()` should create/reactivate the participant, hydrate from a
resolved contact, and copy cached contact research. The router/tool should
dispatch jobs after commit. That avoids queued jobs pointing at rows that later
roll back.

Contact research should become the canonical research path for contact-backed
stakeholders. The write service still should not enqueue it before commit. After
commit, the router/tool/workflow can enqueue contact research when contact
research is missing or stale.

## Identity Precedence

Use this order in the service:

1. `contact_id`
2. normalized `email`
3. normalized `linkedin_url`
4. normalized `name`

`contact_id` is authoritative when present. If a caller sends `contact_id` plus
manual fragments, the contact should hydrate the durable profile fields. Manual
fragments can still be allowed as optional display hints, but they should not
silently override a real CRM contact during create.

## Desired Flow

### 1. Selected Contact: `contact_id`

Use when the frontend search result or agent has already selected a CRM contact.

Flow:

1. Load active org-owned contact by `contact_id`.
2. If missing, return 404.
3. Hydrate stakeholder identity from contact:
   - email
   - full name
   - job title
   - phone
   - LinkedIn
   - department/external links where available
4. Find existing participant on the same deal by contact ID, email, or LinkedIn.
5. If removed, reactivate the same participant.
6. If active, return conflict.
7. Else create `CRMDealParticipant` linked by `related_local_id`.
8. Copy cached `contact.contact_research` to `stakeholder_research` if present.
9. After commit, dispatch processing jobs if the stakeholder has email.
10. After commit, enqueue contact research if contact research is missing or
    stale.
11. When contact research completes, fan out the contact research result to all
    stakeholder participants with the same `related_local_id`.

### 2. Email

Use when the user enters an email or the agent has a reliable email.

Flow:

1. Normalize email.
2. Reject internal org emails.
3. Find active org contact by email.
4. If found, hydrate from that contact.
5. If not found and the email is a business domain, create/reuse a contact
   through `ContactWriteService.find_or_create_contact_by_email()`.
6. If not found and the email is a personal/consumer domain, create only the
   participant and do not create an account-backed contact automatically.
7. Find existing participant on the same deal by contact ID or email.
8. Reactivate removed participant, conflict active participant, otherwise create
   participant.
9. After commit, dispatch email/calendar/stakeholder-sync processing jobs.
10. After commit, enqueue contact research if contact research is missing or
    stale.
11. When contact research completes, fan out the contact research result to all
    stakeholder participants linked to that contact.

### 3. LinkedIn URL

Use when the user enters a LinkedIn profile URL and no email/contact was selected.

Flow:

1. Normalize LinkedIn URL enough for exact comparison.
2. Find active org contact by exact LinkedIn URL.
3. If found, treat it like selected `contact_id`.
4. If not found, do not create a contact yet.
5. Create/reactivate a participant with `linkedin_url`, no email, and no
   `related_local_id`.
6. Find existing participant on the same deal by LinkedIn URL to prevent obvious
   duplicate rows.
7. After commit, run LinkedIn enrichment as the temporary non-contact path.
8. Do not dispatch email/calendar rematch jobs until email exists.

Future upgrade:

If stakeholder research starts returning email, add a small linkage step after
enrichment:

1. normalize returned email;
2. reject internal email;
3. resolve/create contact through `ContactWriteService.find_or_create_contact_by_email()`;
4. set participant `related_local_id` and `related_email`;
5. enqueue contact research if needed;
6. dispatch the email/calendar/stakeholder-sync processing jobs.

### 4. Name

Use when the user only knows the person's name.

Flow:

1. Normalize name.
2. Do not create a CRM contact.
3. Do not run stakeholder research.
4. Do not dispatch email/calendar rematch jobs.
5. Create a deal-local participant placeholder.
6. Optionally warn about same-deal name matches, but do not hard-conflict on
   name because names are weak identifiers.
7. When email or LinkedIn is later added through PATCH, run the same
   resolve/link/enrich path as above.

## Dedupe Rules

Hard dedupe:

- same deal and same `related_local_id`
- same deal and same normalized email
- same deal and same normalized LinkedIn URL

Soft warning only:

- same deal and same normalized name

Do not cross-deal dedupe stakeholders. The same person can be a stakeholder on
multiple deals.

## Proposed Responsibility Split

`StakeholderWriteService.create_stakeholder()` should own:

- tenant/deal boundary checks
- contact lookup by ID/email/LinkedIn
- create/reactivate participant
- copy cached contact profile/research into participant snapshot
- timeline add event

`ContactWriteService` should own:

- contact creation
- email-based contact dedupe
- account creation/resolution from email domain
- contact mutation/projection events

Routers and agent tools should own:

- post-commit job dispatch
- API/tool response shape
- deciding whether processing jobs are visible to the user
- choosing contact research for contact-backed stakeholders
- choosing temporary LinkedIn enrichment for LinkedIn-only stakeholders

Research worker should own:

- contact research as the canonical reusable research record
- fanout from contact research to every stakeholder participant linked to that contact
- temporary LinkedIn-only enrichment until LinkedIn can resolve/create a contact
- writing profile/research values to contact only when a contact is linked

## Implementation Gaps To Fix Later

1. Contact-backed stakeholders should enqueue contact research, not stakeholder
   research, after commit when contact research is missing or stale.
2. The API docs currently describe LinkedIn research dispatch as automatic, but
   the API handler does not do it for LinkedIn-only stakeholders today.
3. Conflict messages should stop saying "with this email" when the match may be
   by `contact_id` or LinkedIn.
4. LinkedIn normalization is exact-string today. That is acceptable for the first
   pass, but the known ceiling is duplicate URLs with trailing slashes, locale
   paths, or query params.
5. If we want LinkedIn-only to eventually create/link a contact, stakeholder
   research output must include email and the worker needs a post-enrichment
   contact linkage step.
6. Name-only PATCH-to-email/LinkedIn should be treated as identity upgrade, not
   just field edit. It should resolve/link contact and run the right post-update
   jobs.
7. Agent tools can send contradictory `contact_id`, `email`, and `linkedin_url`
   values; create must detect and reject those conflicts.
8. Contact research fanout needs one canonical update path so cached contact
   research and newly completed contact research update all linked participants
   consistently.

## Loopholes To Close

1. **Contradictory agent identity**: `contact_id` belongs to Jane, email belongs
   to Bob, LinkedIn belongs to Priya. Reject; do not pick one silently.
2. **Contact ID plus manual overwrite**: agent sends `contact_id` and a different
   name. Hydrate from contact; use manual name only if the contact has no name.
3. **LinkedIn no-email false negative**: Apollo not returning email is not proof
   the profile is wrong. Do not hard reject unless the profile itself is invalid.
4. **Pre-commit dispatch**: do not enqueue contact research from inside the write
   service before commit, or workers can observe rolled-back rows.
5. **Name-only CRM pollution**: do not create a contact from stakeholder name
   alone. It is a participant placeholder until identity is upgraded.
6. **Personal email account creation**: email can create a stakeholder, but
   personal domains should not automatically create account-backed contact noise
   unless a product rule explicitly allows it.
7. **Research duplication**: contact-backed stakeholders should converge on
   contact research. Stakeholder research should remain only as a temporary path
   for LinkedIn-only or unlinked stakeholders.
8. **Identity upgrade merge**: when a name-only or LinkedIn-only stakeholder later
   gets an email/contact, merge or reactivate the canonical participant instead
   of leaving duplicate deal participants.

## Target Shape For A Simpler Create Function

The service should read like this:

```text
normalize request identity
contact = resolve_contact_claims_or_raise_conflict(contact_id, email, linkedin_url)
hydrate fields from contact if contact exists
reject internal email
contact = ensure_email_contact_if_needed(email, contact)
participant = find_existing_participant(contact_id, email, linkedin_url)
if participant exists: reactivate_or_conflict(participant)
participant = create_participant(contact, email, linkedin_url, name, behavior_fields)
copy_cached_research(contact, participant)
create_timeline_event(participant)
return participant_detail(participant, contact)
```

That is still production logic. The simplification is that identity resolution is
one ordered section, participant creation is one section, and post-commit jobs
stay outside the service.

## Final Position

For the product you described, the clean answer is:

- `contact_id`: link to that contact and hydrate.
- `email`: resolve/create contact, then create/link stakeholder.
- `linkedin_url`: resolve existing contact if possible; otherwise create
  LinkedIn-only stakeholder and enrich it, but do not create a contact until
  email is available.
- `name`: create only a stakeholder placeholder; never create a contact.

This keeps the simple one-box frontend while preserving the backend truth:
contacts are durable CRM people, stakeholders are deal participation, and account
automation starts only when email/domain exists.

Contact-backed stakeholders should move to contact research as the canonical
research source. Stakeholder research should be treated as a temporary bridge for
unlinked LinkedIn-only stakeholders until enrichment can resolve and link a
contact.
