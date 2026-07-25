# Communication-to-deal association

## Decision

Email and calendar sync do not launch separate deal mappers. After provider rows are
committed, both dispatch `deal.hydrate_contact_communications`. That worker resolves
contacts, projects contact/account activity, and applies the deal rule in the same
tenant transaction.

## Association rule

A communication is associated automatically only when all of these are true:

1. A participant resolves to an approved contact participant on an open deal.
2. The same communication contains the deal owner or an active internal user participant.

Emails are associated with every open deal satisfying both conditions. Calendar events
are associated only when exactly one open deal satisfies them; multiple matches remain
ambiguous because one meeting must not be guessed onto a deal.

Existing association rows are retry/history state, not matching evidence. Every hydration
or replay evaluates the current participant rule first. A stale link to another or deleted
deal therefore cannot suppress a newly valid email/deal link, select a calendar deal, or
launch attachment/insight follow-up work. Deal-scoped replay also revalidates links on the
requested deal, so removing a contact removes that deal's stale email and meeting links.
Cancelled meetings deactivate existing links and are not newly associated.

No created-date or rolling-window rule is used. A deal created after earlier meetings can
recover that history through the same deal-scoped replay.

## Runtime flow

```text
provider sync commits changed IDs
  -> bounded hydration task
  -> resolve/create contacts
  -> project contact/account activity
  -> choose every eligible email deal or one unambiguous calendar deal
  -> insert retry-safe compatibility association
  -> project the deal-owned activity card
  -> update approved participant last-contact time
  -> discover co-participants once
  -> replay history for newly approved contacts, with discovery disabled
```

Only contacts on an existing customer domain can be discovered, preventing a copied vendor
or adviser from becoming a stakeholder. The first five communication-discovered
participants per deal are auto-approved; later ones are pending. Pending participants
cannot anchor matching. The replay is one hop, so
an inferred participant can recover their prior communications but cannot recursively
pull an unlimited contact graph into the deal.

## Deal and migration replay

Participant changes, lead conversion, CRM/CSV import, and the scheduled initial-backfill
coordinator dispatch the hydration worker with bounded `deal_ids`. The worker loads only
approved contacts for those deals and reuses the same association rule. This covers deals
created after mailbox integration and migrations without an organisation-wide
email/calendar mapper.

Creating a deal with inline stakeholders starts one visible, deal-scoped stakeholder job.
That job processes the whole deal and owns the communication replay; it is not repeated
once per stakeholder. Primary-contact-only creation, a primary-contact update, or failure
to create the stakeholder job starts one visible `deal_communication_hydration` job
through the same deal job service, so historical communication is still recovered.

## Progress visibility

Hydration emits client progress only when its caller explicitly sets `emit_events=True`
and supplies a durable `DealJobs.job_id`. A job ID alone is not permission to communicate:
scheduled backfill also uses durable internal jobs for recovery. User-triggered deal and
stakeholder operations opt in and return the job ID so the client can subscribe to the
existing deal-job SSE endpoint. Integration sync, CRM import, scheduled backfill, and other
bulk/background callers keep the default `emit_events=False`; they run the identical
hydration logic without creating Redis progress streams.

For a stakeholder-triggered replay, the same job remains active across stakeholder sync
and hydration. Stakeholder sync hands the job to hydration instead of finalizing it early.
Hydration completes the job after association writes commit and downstream research,
attachment, and insight dispatch has been attempted. An attachment publish failure retries
the hydration job; research and insight dispatch failures are logged independently. The
downstream tasks themselves are not part of this job's completion boundary and may continue
after it completes.

## Operational properties

- Source association uniqueness is the retry boundary (`ON CONFLICT DO NOTHING`).
- Organisation predicates remain on every source, deal, participant, and association query.
- Batches are capped at 100 source, contact, or deal IDs.
- Participant discovery is serialized per deal with a transaction advisory lock.
- Worker output reports created, deactivated, ambiguous, and auto-approved associations.
- Research, attachment extraction, and email-driven insight processing dispatch only after
  the association transaction commits.
- The split email/calendar matcher tasks and services have been removed; hydration is the
  only communication-to-deal mapping entrypoint.
