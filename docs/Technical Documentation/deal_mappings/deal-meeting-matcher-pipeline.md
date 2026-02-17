# Deal Mapping Pipeline: Stakeholders → Meetings → Timeline

> **Purpose**: Definitive reference for how deals get connected to meetings. The pipeline starts with stakeholder mapping (who are the customer contacts on this deal?) and then uses those stakeholders to find and validate meeting associations. This is core architecture.

## Design Principles

1. **Stakeholders first, meetings second** — You can't match meetings without knowing who the customer contacts are. Stakeholder mapping always runs before meeting matching.
2. **Self-healing** — Every run re-validates data integrity. Bad data from any source (AI, import bugs, manual errors) gets corrected automatically. The system converges to a correct state over successive runs.
3. **Multiple input sources, single truth** — Stakeholders come from CRM sync, calendar discovery, email discovery, AI agents, and manual user input. The pipeline treats them all the same and validates all of them.

---

## Full Pipeline Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     DEAL MAPPING PIPELINE                                │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │  PHASE 1: STAKEHOLDER MAPPING                                     │  │
│  │  ─────────────────────────────                                    │  │
│  │                                                                   │  │
│  │  CRM Sync → CRMDealParticipant (CONTACT type)                    │  │
│  │       │                                                           │  │
│  │       ▼                                                           │  │
│  │  StakeholderSyncService                                           │  │
│  │       ├── Create DealStakeholder (source=CRM, APPROVED)           │  │
│  │       ├── Enrich from CRMContact (job_title, name)                │  │
│  │       ├── Soft-delete removed participants                        │  │
│  │       ├── Reject internal-domain stakeholders (→ REJECTED)        │  │
│  │       └── IF new stakeholders created → trigger Phase 2           │  │
│  │                                                                   │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                              │                                          │
│                              ▼                                          │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │  PHASE 2: MEETING MATCHING                                        │  │
│  │  ─────────────────────────                                        │  │
│  │                                                                   │  │
│  │  Stage 0: Cancel Handling                                         │  │
│  │  Stage 1: Data Collection (deal participants, domains, emails)    │  │
│  │  Stage 2: Contact Collection (build clean contact_emails set)     │  │
│  │  Stage 3: Two-Pass Matching (organizer=1.0, attendee=0.8)        │  │
│  │  Stage 4: Association Validation (remove orphaned associations)   │  │
│  │                                                                   │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                              │                                          │
│                              ▼                                          │
│                    Deal Timeline (clean)                                 │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Phase 1: Stakeholder Mapping

Stakeholder mapping is the foundation. It answers: **"Who are the customer contacts on this deal?"** Without this, meeting matching has no contact emails to match against.

### What Triggers It

| Trigger | Task | Scope |
|---------|------|-------|
| CRM sync completes | `sync_deal_stakeholders_task` | Per deal (dispatched for each synced deal) |
| Hourly schedule | `sync_all_deals_stakeholders_all_orgs_task` | All orgs → all deals |
| Manual re-sync | `sync_all_deals_stakeholders_org_task` | All deals in one org |

### How CRM Participants Become Stakeholders

```
CRM API (HubSpot / Salesforce)
  │
  ▼
CRMDealParticipant table
  │
  ├── participant_type = USER     → Team member (owner, collaborator)
  │                                  NOT a stakeholder — used for team_emails
  │
  ├── participant_type = CONTACT  → Customer contact
  │     │                            THIS becomes a DealStakeholder
  │     │
  │     ▼
  │   StakeholderSyncService._ensure_stakeholder()
  │     │
  │     ├── Does stakeholder already exist for this deal + email?
  │     │     ├── YES → Update name, enrich job_title from CRMContact
  │     │     └── NO  → Create new DealStakeholder:
  │     │                 email = participant.related_email
  │     │                 name = participant.related_name
  │     │                 source = CRM
  │     │                 approval_status = APPROVED (CRM is trusted)
  │     │                 source_reference_id = participant.participant_id
  │     │
  │     └── Enrich from CRMContact (if email matches):
  │           stakeholder.contact_id = contact.contact_id
  │           stakeholder.job_title = contact.job_title
  │
  └── participant_type = COMPANY  → Skipped (not a person)
```

### Stakeholder Sources

Stakeholders don't only come from CRM. The DealStakeholder table is fed by multiple sources:

```
┌──────────────────────┬───────────────────────┬──────────────────────────┐
│ Source                │ Created By            │ Approval Logic           │
├──────────────────────┼───────────────────────┼──────────────────────────┤
│ CRM                  │ StakeholderSyncService │ Always APPROVED          │
│ CALENDAR_DISCOVERY   │ Meeting matcher        │ First 5 auto-APPROVED,   │
│                      │ (Phase 2, Stage 4)     │ rest PENDING             │
│ EMAIL_DISCOVERY      │ Email matcher          │ First 5 auto-APPROVED,   │
│                      │                        │ rest PENDING             │
│ MANUAL               │ User API / AI agents   │ APPROVED                 │
└──────────────────────┴───────────────────────┴──────────────────────────┘
```

Note the feedback loop: Phase 2 (meeting matching) can **discover** new stakeholders from calendar attendees, which then feed back into future matching runs. Same for email matching.

### Soft-Delete Logic

When a CRM sync runs and a participant is no longer in the CRM:

```
Existing stakeholder (source=CRM) NOT in current participant list
  │
  ├── IF user has set status to REMOVED → leave it (user intent preserved)
  └── ELSE → soft-delete (is_deleted = true)
```

User-managed fields (stored in `stakeholder_data.manual_overrides`) are never overwritten by CRM sync.

### Internal Stakeholder Rejection

After syncing CRM participants, the service rejects any stakeholder whose email domain matches the org's internal domains. This self-cleans internal employees that were accidentally added as stakeholders (by AI agents, manual error, etc.).

```
_reject_internal_stakeholders(session, deal_id)
  │
  ├── internal_domains = _collect_internal_domains(session)
  │     ├── Tier 1: Organisation.domain (admin-configured, authoritative)
  │     └── Tier 2 (fallback): Derived from USER participant emails
  │     (Cached for lifetime of service instance)
  │
  ├── IF no internal_domains → skip
  │
  ├── Query: stakeholders WHERE deal_id = X
  │                        AND is_deleted = false
  │                        AND approval_status != REJECTED
  │
  └── For each stakeholder:
        IF email domain IN internal_domains:
          SET approval_status = REJECTED
```

**Why in Phase 1 (not Phase 2)**: Stakeholder rejection is a stakeholder concern — it validates the data integrity of the DealStakeholder table. The meeting matcher should receive already-clean stakeholder data.

**Why REJECTED not deleted**: Preserves audit trail, visible in UI, reversible (admin can re-approve). `_collect_contact_emails()` already filters `approval_status == APPROVED`, so REJECTED stakeholders are automatically excluded from matching.

**Internal domain detection (two tiers):**

| Tier | Source | Reliability |
|------|--------|-------------|
| Tier 1 | `Organisation.domain` (admin-configured) | High — explicit, authoritative |
| Tier 2 | Derived from USER participant emails | Medium — works for common case but may include personal emails |

**Admin action**: Configure `Organisation.domain` in org settings for reliable detection.

### What Happens After Stakeholder Sync

```
sync_deal_stakeholders_task(org_id, deal_id)
  │
  ├── Run StakeholderSyncService.sync_for_deal(deal_id)
  │     └── Returns: new_count (how many stakeholders were created)
  │
  └── IF new_count > 0:
        ├── Dispatch match_calendar_to_deals_task(org_id, deal_id)  ← Phase 2
        └── Dispatch match_email_to_deals_task(org_id, deal_id)
```

Meeting matching is only triggered when new stakeholders appear. This avoids unnecessary re-matching when nothing changed.

---

## Phase 2: Meeting Matching

With stakeholders mapped, the matcher can answer: **"Which calendar events involve both a team member and a customer contact from this deal?"**

### What Triggers It

| Trigger | Task | Scope |
|---------|------|-------|
| New stakeholders created (Phase 1) | `match_calendar_to_deals_task` | Single deal |
| Calendar sync completes | `match_calendar_to_deals_org_task` | All deals in org |
| Hourly schedule | `match_calendar_to_deals_all_orgs_task` | All orgs → all deals |

### Pipeline Stages (per deal)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     MEETING MATCHER (per deal)                           │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  Stage 0: Cancel Handling                                               │
│  ────────────────────────                                               │
│  Mark associations as CANCELLED where calendar event was cancelled       │
│                                                                         │
│  Stage 1: Data Collection                                               │
│  ────────────────────────                                               │
│  Fetch deal + participants → customer_domains, team_emails              │
│                                                                         │
│  Stage 2: Contact Collection                                            │
│  ────────────────────────────                                           │
│  Build clean contact_emails (APPROVED only, minus team, minus internal) │
│  (Internal stakeholders already REJECTED by Phase 1)                    │
│                                                                         │
│  Stage 3: Two-Pass Matching                                             │
│  ──────────────────────────                                             │
│  PASS 1: Team=ORGANIZER + Contact=ATTENDEE → score 1.0                  │
│  PASS 2: Team=ATTENDEE  + Contact=ATTENDEE → score 0.8                  │
│  Each match → association + timeline event + stakeholder discovery       │
│                                                                         │
│  ╔═════════════════════════════════════════════════════════════════════╗ │
│  ║  Stage 4: Association Validation                                   ║ │
│  ║  Remove ACTIVE associations where no attendee matches contacts     ║ │
│  ╚═════════════════════════════════════════════════════════════════════╝ │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘

  Stage 4 (boxed) = validation layer. Runs every cycle.
```

---

## Stage-by-Stage Detail

### Stage 0: Cancel Handling

```
_handle_cancelled_associations(session, deal_id?)
  │
  ├── Query: associations WHERE event.status = 'cancelled'
  │                        AND association.status != CANCELLED
  │
  └── For each: SET association.status = CANCELLED
```

If a meeting was cancelled in the calendar provider, we mark the association as CANCELLED so it drops from the deal timeline.

### Stage 1: Data Collection

```
deal = fetch(deal_id, with participants eager-loaded)

customer_domains = _collect_customer_domains(deal)
  └── Extract domains from CONTACT-type participants
      e.g., {"acme.com", "bigcorp.io"}
      Used for: stakeholder DISCOVERY in Stage 4

team_emails = _collect_team_emails(deal)
  └── Extract emails from USER-type participants
      Fallback: deal.owner_email if no USER participants
      e.g., {"owner@copilotgtm.com", "ae@copilotgtm.com"}
      Used for: identifying "our side" in matching
```

**Key distinction — three email sets used in this pipeline:**

| Set | Built from | Purpose |
|-----|-----------|---------|
| `customer_domains` | CRM CONTACT participants | Stakeholder discovery (Stage 4) |
| `team_emails` | CRM USER participants | Matching — "our side" of the meeting |
| `contact_emails` | DealStakeholder (APPROVED) | Matching — "their side" of the meeting |

### Stage 2: Contact Collection

```
contact_emails = _collect_contact_emails(session, deal)
  └── SELECT email FROM deal_stakeholders
      WHERE deal_id = X AND is_deleted = false AND approval_status = APPROVED
      → Internal stakeholders already REJECTED by Phase 1

contact_emails -= team_emails
  └── Prevent self-matching (team member can't be both sides)

contact_emails -= { emails with internal domains }
  └── Double safety for edge cases
```

If `contact_emails` is empty after all filters → no matching possible → return 0.

### Stage 3: Two-Pass Matching

```
PASS 1: Organizer Match (score = 1.0, highest confidence)
  │
  ├── Query: calendar events WHERE
  │     organiser_email IN team_emails
  │     AND ANY attendee.email IN contact_emails
  │     AND event.status != 'cancelled'
  │
  └── For each matched event:
        ├── Create CalendarEventDealAssociation (INSERT ON CONFLICT DO NOTHING)
        ├── Create timeline event
        ├── Discover new stakeholders from attendees (see below)
        └── Update stakeholder last_contact_at

PASS 2: Attendee Match (score = 0.8, lower confidence)
  │
  ├── Query: calendar events WHERE
  │     ANY attendee.email IN team_emails
  │     AND organiser NOT in team_emails (handled in Pass 1)
  │     AND ANY attendee.email IN contact_emails
  │     AND event.status != 'cancelled'
  │     AND event NOT already matched in Pass 1
  │
  └── Same logic as Pass 1
```

**Duplicate prevention**: `INSERT ... ON CONFLICT DO NOTHING` on the `(event_id, deal_id)` unique constraint. If association already exists from a previous run, the insert is silently skipped. Stakeholder `last_contact_at` is still updated.

#### Stakeholder Discovery (within Stage 3)

When a meeting is matched, the matcher checks: "Are there attendees from the customer's company who aren't yet stakeholders?"

```
_discover_stakeholders_from_event(session, deal, event, customer_domains, team_emails)
  │
  ├── For each attendee in event:
  │     ├── Skip if email in team_emails
  │     ├── Skip if email domain NOT in customer_domains
  │     ├── Skip if already a stakeholder for this deal
  │     │
  │     └── Create DealStakeholder:
  │           source = CALENDAR_DISCOVERY
  │           approval_status = APPROVED (if < 5 discovered) or PENDING (6th+)
  │
  └── 5-LIMIT RULE:
        First 5 CALENDAR_DISCOVERY stakeholders per deal → auto-APPROVED
        Beyond 5 → PENDING (user must approve before they affect matching)
```

This is the **feedback loop**: matched meetings can discover new stakeholders, which become available for future matching runs.

### Stage 4: Association Validation

```
_cleanup_orphaned_associations(session, deal, contact_emails)
  │
  ├── Query: associations WHERE deal_id = X AND status = ACTIVE
  │     (eager-load event → attendees via selectinload)
  │
  └── For each association:
        event_participant_emails = organizer email + all attendee emails
        overlap = event_participant_emails & contact_emails

        IF overlap is EMPTY:
          SET association.status = REMOVED
```

**Why this is needed**: When Phase 1 rejects a stakeholder, associations that existed only because of that stakeholder become orphaned. Stage 4 finds and removes them.

**Example — orphaned:**
```
Event participants: {saksham@copilotgtm.com, raman@copilotgtm.com}
contact_emails:     {buyer@acme.com, cfo@acme.com}
overlap:            {} → EMPTY → REMOVED
```

**Example — valid (stays):**
```
Event participants: {owner@ourco.com, buyer@acme.com}
contact_emails:     {buyer@acme.com, cfo@acme.com}
overlap:            {buyer@acme.com} → NOT empty → stays ACTIVE
```

**Performance**: 2 SQL queries (associations + eager-loaded events/attendees), then Python set intersections. No N+1.

---

## Complete Data Flow

```
  CRM API (HubSpot/Salesforce)
       │
       ▼
  CRMDealParticipant
       │
       ├── type=USER ──────────────────────────────► team_emails
       │                                                  │
       └── type=CONTACT                                   │
             │                                            │
             ▼                                            │
  ┌─────────────────────────┐                             │
  │ PHASE 1:                │                             │
  │ StakeholderSyncService  │                             │
  │                         │                             │
  │ CRM participant         │                             │
  │    → DealStakeholder    │                             │
  │      (source=CRM,       │                             │
  │       APPROVED)         │                             │
  │                         │                             │
  │ ╔═══════════════════╗   │                             │
  │ ║ Reject internal   ║   │                             │
  │ ║ domain emails     ║   │                             │
  │ ║ → REJECTED        ║   │                             │
  │ ╚═══════════════════╝   │                             │
  └───────────┬─────────────┘                             │
              │                                           │
              ▼                                           │
  DealStakeholder table ◄── Calendar Discovery            │
       │                ◄── Email Discovery                │
       │                ◄── AI Agents / User               │
       │                                                   │
       ▼                                                   │
  contact_emails (APPROVED only,                           │
                  minus team,                              │
                  minus internal)                          │
       │                                                   │
       │                    ┌──────────────────────────────┘
       │                    │
       ▼                    ▼
  ┌─────────────────────────────────┐
  │ Stage 3: Match                  │
  │                                 │
  │ Calendar Event WHERE:           │
  │   team member + contact         │
  │   both present                  │
  │                                 │
  │ → CalendarEventDealAssociation  │
  │ → Timeline Event                │
  │ → Discover new stakeholders ────┼──► feeds back into DealStakeholder
  └──────────────┬──────────────────┘
                 │
       ╔═════════╧══════════╗
       ║ Stage 4: Validate  ║
       ║ Orphaned?          ║
       ║ → REMOVED          ║
       ╚═════════╤══════════╝
                 │
                 ▼
          Deal Timeline (clean)
```

---

## Status Model

### DealStakeholder.approval_status

| Status | In matching? | Set by |
|--------|-------------|--------|
| `APPROVED` | Yes | CRM sync, calendar discovery (first 5), user, AI |
| `PENDING` | No | Calendar discovery (6th+), email discovery (6th+) |
| `REJECTED` | No | Phase 1 validation (internal domain), user rejection |
| `REMOVED` | No | User removed from deal |

### CalendarEventDealAssociation.status

| Status | In timeline? | Set by |
|--------|-------------|--------|
| `ACTIVE` | Yes | Stage 3 matching |
| `REMOVED` | No | Stage 4 validation (orphaned), user removal |
| `CANCELLED` | No | Stage 0 (calendar event cancelled) |

### DealStakeholder.source

| Source | Created by | Trust level |
|--------|-----------|-------------|
| `CRM` | StakeholderSyncService | High — direct from CRM API |
| `CALENDAR_DISCOVERY` | Meeting matcher (Stage 3) | Medium — inferred from attendance |
| `EMAIL_DISCOVERY` | Email matcher | Medium — inferred from email headers |
| `MANUAL` | User API / AI agents | Variable — depends on who added it |

---

## Self-Healing Scenarios

The validation layer (Phase 1 rejection + Phase 2 Stage 4) handles these automatically, every run:

| Scenario | Where | Action |
|----------|-------|--------|
| Internal employee added as stakeholder (any source) | Phase 1 | `approval_status → REJECTED` |
| Meeting matched only because of internal stakeholder | Phase 2, Stage 4 | `association.status → REMOVED` |
| Internal email re-added by future bug | Phase 1 | Caught again on next run |
| Calendar event cancelled after matching | Phase 2, Stage 0 | `association.status → CANCELLED` |
| CRM contact removed → stakeholder soft-deleted | Phase 2, Stage 4 | Orphaned associations removed |

---

## Known Limitations and Future Enhancements

| Limitation | Impact | Future Enhancement |
|------------|--------|-------------------|
| **Org domain not configured** | Falls back to Tier 2 (USER email derivation). Less reliable for personal-email orgs. | Prompt admins to configure domain on org setup. |
| **Re-activating REMOVED associations** | If a new contact makes a previously-orphaned meeting valid, the association stays REMOVED. `INSERT ... ON CONFLICT DO NOTHING` can't update existing rows. | Add `ON CONFLICT DO UPDATE` to reactivate REMOVED → ACTIVE. |
| **No `created_by` on stakeholders** | Can't distinguish AI-created vs user-created. Both use `source=MANUAL`, `approval_status=APPROVED`. | Schema migration to add `created_by` field. |
| **External stakeholders from false meetings** | If AI analyzed a falsely-matched meeting and added `john@random.com`, that won't be caught by internal-domain check. | Rare. Could add source-meeting validation in future. |

---

## File Reference

### Phase 1: Stakeholder Mapping

| File | Role |
|------|------|
| `src/workers/deal_worker/services/deal_mapper_services/stakeholder_sync_service.py` | CRM → DealStakeholder sync + internal domain rejection |
| `src/workers/deal_worker/tasks/deal_mappers/stakeholder_tasks.py` | Three-tier task orchestration |
| `src/service/integrations/crm_deal_sync_service.py` | CRM sync → dispatches stakeholder sync |

### Phase 2: Meeting Matching

| File | Role |
|------|------|
| `src/workers/deal_worker/services/deal_mapper_services/deal_meeting_matcher.py` | All matching + validation logic |
| `src/workers/deal_worker/tasks/deal_mappers/calendar_matching_tasks.py` | Task orchestration |

### Tests

| File | Covers |
|------|--------|
| `tests/workers/deal_worker/services/test_stakeholder_sync_service.py` | Phase 1: sync + internal domain rejection |
| `tests/workers/deal_worker/services/test_deal_meeting_matcher_cleanup.py` | Phase 2, Stage 4: orphaned association cleanup |
| `tests/workers/deal_worker/services/test_stakeholder_last_contact_updates.py` | Stakeholder last_contact_at updates |
| `tests/service/agents/test_stakeholder_tools.py` | AI stakeholder tool guards |

### Key Methods (Phase 1 — StakeholderSyncService)

| Method | Purpose |
|--------|---------|
| `_sync_deal_internal()` | Sync CRM participants → DealStakeholder + rejection |
| `_ensure_stakeholder()` | Create or update a stakeholder from CRM participant |
| `_reject_internal_stakeholders()` | Reject stakeholders with internal-domain emails |
| `_collect_internal_domains()` | Determine org's internal domains (cached) |
| `_is_internal_email()` | Check if email belongs to internal domain |

### Key Methods (Phase 2 — DealMeetingMatcher)

| Method | Stage | Purpose |
|--------|-------|---------|
| `_handle_cancelled_associations()` | 0 | Mark cancelled-event associations |
| `_collect_customer_domains()` | 1 | Customer domains from CRM contacts |
| `_collect_team_emails()` | 1 | Team emails from USER participants |
| `_collect_contact_emails()` | 2 | Approved stakeholder emails |
| `_collect_internal_domains()` | 2 | Inline safety filter (same logic, separate cache) |
| `_is_internal_email()` | 2 | Check if email is internal |
| `_match_events_as_organizer()` | 3 (Pass 1) | Match where team is organizer |
| `_match_events_as_attendee()` | 3 (Pass 2) | Match where team is attendee |
| `_create_associations()` | 3 | Create association + timeline + discover |
| `_discover_stakeholders_from_event()` | 3 | New stakeholders from attendees |
| `_cleanup_orphaned_associations()` | 4 | Remove orphaned associations |
