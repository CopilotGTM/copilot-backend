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
3. Exactly one open deal satisfies both conditions.

An existing association is retained during changed-source hydration. Deal-scoped replay
revalidates it against the current participant rule, so removing a contact removes stale
email and meeting links. Multiple existing or rule-matched deals remain
ambiguous; the activity stays visible through its contact/account links and is not guessed
onto a deal. Cancelled meetings deactivate an existing link and are not newly associated.

No created-date or rolling-window rule is used. A deal created after earlier meetings can
recover that history through the same deal-scoped replay.

## Runtime flow

```text
provider sync commits changed IDs
  -> bounded hydration task
  -> resolve/create contacts
  -> project contact/account activity
  -> choose one unambiguous deal
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
