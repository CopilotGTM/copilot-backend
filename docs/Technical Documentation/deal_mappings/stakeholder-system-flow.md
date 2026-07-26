# Stakeholder System Flow - Complete Architecture

> **Current task flow:** This document contains historical split email/calendar
> matcher diagrams. For the verified manual stakeholder-add task graph, including
> current hydration, research, attachments, and insight parallelism, use
> [`stakeholder-add-task-level-flow.md`](stakeholder-add-task-level-flow.md).

> **Historical scope**: This document preserves the earlier comprehensive
> stakeholder architecture and task inventory for reference.

## System Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    STAKEHOLDER SYSTEM ARCHITECTURE                           │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │                      EXTERNAL DATA SOURCES                          │   │
│   │                                                                     │   │
│   │  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐     │   │
│   │  │   CRM    │    │ Calendar │    │  Email   │    │ Meetings │     │   │
│   │  │ HubSpot/ │    │  Google/ │    │  Google/ │    │ Transcripts│    │   │
│   │  │Salesforce│    │Microsoft │    │Microsoft │    │           │     │   │
│   │  └────┬─────┘    └────┬─────┘    └────┬─────┘    └────┬─────┘     │   │
│   └───────┼──────────────┼──────────────┼──────────────┼─────────────┘   │
│           │              │              │              │                  │
│           ▼              ▼              ▼              ▼                  │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │                    INTEGRATION WORKER                               │   │
│   │                                                                     │   │
│   │  integrations.sync_integration_data                                 │   │
│   │  - Syncs CRM deals, contacts, participants                          │   │
│   │  - Syncs calendar events with attendees                             │   │
│   │  - Syncs emails with participants                                   │   │
│   └───────────────────────────┬─────────────────────────────────────────┘   │
│                               │                                              │
│                               ▼                                              │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │                       DEAL WORKER                                   │   │
│   │                                                                     │   │
│   │  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐           │   │
│   │  │ Stakeholder   │  │   Calendar    │  │    Email      │           │   │
│   │  │    Sync       │  │   Matching    │  │   Matching    │           │   │
│   │  └───────┬───────┘  └───────┬───────┘  └───────┬───────┘           │   │
│   │          │                  │                  │                    │   │
│   │          └──────────────────┼──────────────────┘                    │   │
│   │                             │                                       │   │
│   │                             ▼                                       │   │
│   │  ┌─────────────────────────────────────────────────────────────┐   │   │
│   │  │               STAKEHOLDER ANALYSIS (AI)                     │   │   │
│   │  │                                                             │   │   │
│   │  │  - Stance analysis (Champion, Blocker, etc.)                │   │   │
│   │  │  - Decision power classification                            │   │   │
│   │  │  - Temperature calculation                                  │   │   │
│   │  │  - Insight generation                                       │   │   │
│   │  └─────────────────────────────────────────────────────────────┘   │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│                               │                                              │
│                               ▼                                              │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │                    DEAL STAKEHOLDER TABLE                           │   │
│   │                                                                     │   │
│   │  - email, name, job_title                                           │   │
│   │  - source (CRM, CALENDAR_DISCOVERY, EMAIL_DISCOVERY, MANUAL)        │   │
│   │  - stance, decision_power, temperature                              │   │
│   │  - engagement_score, last_contact_at                                │   │
│   │  - insights (AI-generated)                                          │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Task Orchestration Map

### All Stakeholder-Related Celery Tasks

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    CELERY TASK INVENTORY                                     │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   STAKEHOLDER SYNC TASKS                                                     │
│   ══════════════════════                                                    │
│   stakeholder.sync_all_deals_stakeholders_all_orgs  → Tier 1 coordinator    │
│   stakeholder.sync_all_deals_stakeholders_org       → Tier 2 org-level      │
│   stakeholder.sync_deal_stakeholders                → Tier 3 deal-level     │
│                                                                              │
│   CALENDAR MATCHING TASKS                                                    │
│   ═══════════════════════                                                   │
│   deal.match_calendar_to_deals_all_orgs             → Tier 1 coordinator    │
│   deal.match_calendar_to_deals_org                  → Tier 2 org-level      │
│   deal.match_calendar_to_deals                      → Tier 3 deal-level     │
│                                                                              │
│   EMAIL MATCHING TASKS                                                       │
│   ════════════════════                                                      │
│   deal.match_email_to_deals_all_orgs                → Tier 1 coordinator    │
│   deal.match_email_to_deals_org                     → Tier 2 org-level      │
│   deal.match_email_to_deals                         → Tier 3 deal-level     │
│                                                                              │
│   ANALYSIS TASKS                                                             │
│   ══════════════                                                            │
│   stakeholder.trigger_analysis_all_orgs             → Tier 1 coordinator    │
│   stakeholder.analyze_org_stakeholders              → Tier 2 org-level      │
│   stakeholder.analyze_deal_stakeholders             → Tier 3 deal-level     │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Complete Data Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    COMPLETE STAKEHOLDER DATA FLOW                            │
└─────────────────────────────────────────────────────────────────────────────┘

PHASE 1: DATA INGESTION
═══════════════════════════════════════════════════════════════════════════════

  ┌────────────────────────────────────────────────────────────────────────┐
  │                     INTEGRATION WORKER                                  │
  │                                                                        │
  │   Webhook from Nango                                                   │
  │          │                                                             │
  │          ▼                                                             │
  │   integrations.sync_integration_data                                   │
  │          │                                                             │
  │          ├──▶ CRM Sync (deals, contacts, companies, users)             │
  │          │         │                                                   │
  │          │         └──▶ Creates: CRMDeal, CRMContact,                  │
  │          │                       CRMDealParticipant                    │
  │          │                                                             │
  │          ├──▶ Calendar Sync (events)                                   │
  │          │         │                                                   │
  │          │         └──▶ Creates: CalendarEvent, CalendarAttendee       │
  │          │                                                             │
  │          └──▶ Email Sync (messages)                                    │
  │                    │                                                   │
  │                    └──▶ Creates: Email                                 │
  │                                                                        │
  └────────────────────────────────────────────────────────────────────────┘
                                     │
                                     │ After CRM sync
                                     ▼
PHASE 2: STAKEHOLDER SYNC (CRM Source)
═══════════════════════════════════════════════════════════════════════════════

  ┌────────────────────────────────────────────────────────────────────────┐
  │ CRMDealSyncService._dispatch_stakeholder_sync()                        │
  │                                                                        │
  │ Triggers: sync_deal_stakeholders_task(deal_id, trigger_rematching=True)│
  └────────────────────────────┬───────────────────────────────────────────┘
                               │
                               ▼
  ┌────────────────────────────────────────────────────────────────────────┐
  │                    STAKEHOLDER SYNC SERVICE                            │
  │                                                                        │
  │   sync_deal_stakeholders_task(organisation_id, deal_id)                │
  │          │                                                             │
  │          ▼                                                             │
  │   StakeholderSyncService.sync_for_deal(deal_id)                        │
  │          │                                                             │
  │          ├──▶ Load CRMDeal with participants                           │
  │          │                                                             │
  │          ├──▶ Filter: participant_type == CONTACT                      │
  │          │                                                             │
  │          ├──▶ For each CONTACT:                                        │
  │          │         │                                                   │
  │          │         ├──▶ Normalize email (lowercase)                    │
  │          │         │                                                   │
  │          │         ├──▶ Create/Update DealStakeholder                  │
  │          │         │         source = CRM                              │
  │          │         │         approval_status = APPROVED                │
  │          │         │         source_reference_id = participant_id      │
  │          │         │                                                   │
  │          │         └──▶ Enrich job_title from CRMContact               │
  │          │                                                             │
  │          ├──▶ Soft-delete removed stakeholders                         │
  │          │                                                             │
  │          └──▶ Return StakeholderSyncResult                             │
  │                    │                                                   │
  │                    └──▶ requires_rematching: bool (true if created)    │
  │                                                                        │
  └────────────────────────────┬───────────────────────────────────────────┘
                               │
                               │ IF requires_rematching = True
                               │
              ┌────────────────┴────────────────┐
              │                                  │
              ▼                                  ▼
PHASE 3: CALENDAR MATCHING              PHASE 4: EMAIL MATCHING
(Parallel Execution)                    (Parallel Execution)
═══════════════════════════════════════════════════════════════════════════════

  ┌────────────────────────────┐      ┌────────────────────────────┐
  │ match_calendar_to_deals    │      │ match_email_to_deals       │
  │ _task(org_id, deal_id)     │      │ _task(org_id, deal_id)     │
  │                            │      │                            │
  │ DealMeetingMatcher         │      │ DealEmailMatcher           │
  │ .process_deal()            │      │ .process_deal()            │
  │                            │      │                            │
  │ 1. Build lookup maps:      │      │ 1. Build stakeholder maps: │
  │    - team_emails           │      │    - team_email_to_deals   │
  │    - contact_emails        │      │    - contact_email_to_deals│
  │    - customer_domains      │      │    - company_domains       │
  │                            │      │                            │
  │ 2. Two-pass matching:      │      │ 2. Score calculation:      │
  │    Pass 1: Organizer (1.0) │      │    - Direct: 1.0           │
  │    Pass 2: Attendee (0.8)  │      │    - CC: 0.7               │
  │                            │      │                            │
  │ 3. Create associations:    │      │ 3. Create associations:    │
  │    CalendarEventDeal       │      │    DealEmailAssociation    │
  │    Association             │      │                            │
  │                            │      │                            │
  │ 4. Discover stakeholders:  │      │ 4. Discover stakeholders:  │
  │    - Domain matching       │      │    - Domain matching       │
  │    - Exclude team          │      │    - Exclude team/contacts │
  │    - 5-limit rule          │      │    - 5-limit rule          │
  │                            │      │    - Skip calendar invites │
  │                            │      │                            │
  │ 5. Create timeline events  │      │ 5. Create timeline events  │
  │                            │      │                            │
  └────────────────────────────┘      └────────────────────────────┘
              │                                  │
              │                                  │
              └──────────────┬───────────────────┘
                             │
                             ▼
  ┌────────────────────────────────────────────────────────────────────────┐
  │                    DEAL STAKEHOLDER TABLE                              │
  │                                                                        │
  │   Updated with stakeholders from all sources:                          │
  │   - CRM (source=CRM, approval_status=APPROVED)                         │
  │   - Calendar (source=CALENDAR_DISCOVERY)                               │
  │   - Email (source=EMAIL_DISCOVERY)                                     │
  │                                                                        │
  └────────────────────────────────────────────────────────────────────────┘
                             │
                             │ Scheduled (nightly) or on-demand
                             ▼
PHASE 5: STAKEHOLDER ANALYSIS (AI)
═══════════════════════════════════════════════════════════════════════════════

  ┌────────────────────────────────────────────────────────────────────────┐
  │                    STAKEHOLDER ANALYSIS SERVICE                        │
  │                                                                        │
  │   analyze_deal_stakeholders_task(organisation_id, deal_id)             │
  │          │                                                             │
  │          ▼                                                             │
  │   StakeholderAnalysisService.analyze_deal(deal_id)                     │
  │          │                                                             │
  │          ├──▶ Load APPROVED stakeholders                               │
  │          │                                                             │
  │          ├──▶ Load meeting transcripts (from confirmed meetings)       │
  │          │                                                             │
  │          ├──▶ Build context for AI agent                               │
  │          │                                                             │
  │          ├──▶ Call StakeholderAnalysisAgent                            │
  │          │         │                                                   │
  │          │         └──▶ Returns per-stakeholder analysis:              │
  │          │               - stance (CHAMPION, SUPPORTER, NEUTRAL, etc.) │
  │          │               - decision_power (DECISION_MAKER, INFLUENCER) │
  │          │               - temperature (HOT, WARM, COLD)               │
  │          │               - champion_potential (0.0-1.0)                │
  │          │               - motivation, pain_points, keywords           │
  │          │                                                             │
  │          ├──▶ Update DealStakeholder records                           │
  │          │                                                             │
  │          ├──▶ Track engagement history (JSONB snapshot)                │
  │          │                                                             │
  │          └──▶ Generate insights:                                       │
  │                    - ENGAGEMENT_GAP                                    │
  │                    - SENTIMENT_DECLINING                               │
  │                    - CHAMPION_OPPORTUNITY                              │
  │                    - UNADDRESSED_CONCERN                               │
  │                    - MISSING_DECISION_MAKER                            │
  │                    - COVERAGE_GAP                                      │
  │                    - STAKEHOLDER_RISK                                  │
  │                                                                        │
  └────────────────────────────────────────────────────────────────────────┘
```

---

## Scheduling & Triggers

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    TASK SCHEDULING & TRIGGERS                                │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   PERIODIC SCHEDULE (Celery Beat)                                            │
│   ═══════════════════════════════                                           │
│                                                                              │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │ HOURLY (:00)                                                        │   │
│   │                                                                     │   │
│   │ stakeholder.sync_all_deals_stakeholders_all_orgs                    │   │
│   │     │                                                               │   │
│   │     └──▶ Syncs CRM stakeholders for all orgs                        │   │
│   │          Triggers calendar + email matching if new stakeholders     │   │
│   │                                                                     │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │ NIGHTLY (02:00 UTC)                                                 │   │
│   │                                                                     │   │
│   │ stakeholder.trigger_analysis_all_orgs                               │   │
│   │     │                                                               │   │
│   │     └──▶ AI analysis for all deals with transcripts                 │   │
│   │          Generates stance, decision_power, insights                 │   │
│   │                                                                     │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│   ─────────────────────────────────────────────────────────────────────────  │
│                                                                              │
│   EVENT-DRIVEN TRIGGERS                                                      │
│   ═════════════════════                                                     │
│                                                                              │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │ AFTER CRM SYNC                                                      │   │
│   │                                                                     │   │
│   │ integrations.sync_integration_data (crm/deals)                      │   │
│   │     │                                                               │   │
│   │     └──▶ CRMDealSyncService._dispatch_stakeholder_sync()            │   │
│   │              │                                                      │   │
│   │              └──▶ sync_deal_stakeholders_task(deal_id)              │   │
│   │                                                                     │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │ AFTER STAKEHOLDER SYNC (if new stakeholders created)                │   │
│   │                                                                     │   │
│   │ sync_deal_stakeholders_task                                         │   │
│   │     │                                                               │   │
│   │     └──▶ IF requires_rematching = True:                             │   │
│   │              │                                                      │   │
│   │              ├──▶ match_calendar_to_deals_task(deal_id)             │   │
│   │              │                                                      │   │
│   │              └──▶ match_email_to_deals_task(deal_id)                │   │
│   │                                                                     │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │ AFTER MEETING CONFIRMATION (via API)                                │   │
│   │                                                                     │   │
│   │ POST /deals/{deal_id}/meetings/{event_id}/confirm                   │   │
│   │     │                                                               │   │
│   │     └──▶ stakeholder.sync_from_meeting_task(deal_id, event_id)      │   │
│   │                                                                     │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Service Dependencies

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    SERVICE DEPENDENCY GRAPH                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │                   CRMDealSyncService                                │   │
│   │                                                                     │   │
│   │   Entry point after CRM webhook                                     │   │
│   │   Calls: _dispatch_stakeholder_sync()                               │   │
│   └────────────────────────────────┬────────────────────────────────────┘   │
│                                    │                                        │
│                                    ▼                                        │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │                  StakeholderSyncService                             │   │
│   │                                                                     │   │
│   │   Methods:                                                          │   │
│   │   - sync_for_deal(deal_id) → StakeholderSyncResult                  │   │
│   │   - sync_for_organisation(org_id) → list[StakeholderSyncResult]     │   │
│   │   - _ensure_stakeholder() → DealStakeholder                         │   │
│   │   - _enrich_from_contact() → None                                   │   │
│   │                                                                     │   │
│   │   Dependencies:                                                     │   │
│   │   - CRMDeal, CRMDealParticipant, CRMContact                         │   │
│   │   - DealStakeholder                                                 │   │
│   └────────────────────────────────┬────────────────────────────────────┘   │
│                                    │                                        │
│              ┌─────────────────────┴─────────────────────┐                  │
│              │                                           │                  │
│              ▼                                           ▼                  │
│   ┌─────────────────────────────┐     ┌─────────────────────────────┐      │
│   │    DealMeetingMatcher       │     │     DealEmailMatcher        │      │
│   │                             │     │                             │      │
│   │   Methods:                  │     │   Methods:                  │      │
│   │   - process_organisation()  │     │   - process_organisation()  │      │
│   │   - process_deal()          │     │   - process_deal()          │      │
│   │   - _find_and_link_meetings │     │   - _build_stakeholder_maps │      │
│   │   - _discover_stakeholders  │     │   - _calculate_match_score  │      │
│   │                             │     │   - _discover_stakeholders  │      │
│   │   Dependencies:             │     │                             │      │
│   │   - CalendarEvent           │     │   Dependencies:             │      │
│   │   - CalendarAttendee        │     │   - Email                   │      │
│   │   - CalendarEventDeal       │     │   - DealEmailAssociation    │      │
│   │     Association             │     │   - DealStakeholder         │      │
│   │   - DealStakeholder         │     │                             │      │
│   └─────────────────────────────┘     └─────────────────────────────┘      │
│              │                                           │                  │
│              └─────────────────────┬─────────────────────┘                  │
│                                    │                                        │
│                                    ▼                                        │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │                 StakeholderAnalysisService                          │   │
│   │                                                                     │   │
│   │   Methods:                                                          │   │
│   │   - analyze_deal(deal_id) → AnalysisResult                          │   │
│   │   - analyze_organisation(org_id) → list[AnalysisResult]             │   │
│   │   - _build_context() → str                                          │   │
│   │   - _update_stakeholder() → None                                    │   │
│   │   - _generate_insights() → list[DealStakeholderInsight]             │   │
│   │   - _track_engagement_history() → None                              │   │
│   │                                                                     │   │
│   │   Dependencies:                                                     │   │
│   │   - DealStakeholder                                                 │   │
│   │   - CalendarEventDealAssociation (for transcripts)                  │   │
│   │   - DealStakeholderInsight                                          │   │
│   │   - StakeholderAnalysisAgent (AI)                                   │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Data Model Relationships

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    DATA MODEL RELATIONSHIPS                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   ┌─────────────────┐                                                        │
│   │    CRMDeal      │                                                        │
│   │                 │                                                        │
│   │ - deal_id (PK)  │                                                        │
│   │ - owner_email   │                                                        │
│   │ - primary_      │                                                        │
│   │   contact_email │                                                        │
│   └────────┬────────┘                                                        │
│            │                                                                 │
│            │ 1:N                                                             │
│            ▼                                                                 │
│   ┌─────────────────────┐        ┌─────────────────────┐                    │
│   │ CRMDealParticipant  │        │    CRMContact       │                    │
│   │                     │        │                     │                    │
│   │ - participant_id    │──────▶│ - contact_id (PK)   │                    │
│   │ - deal_id (FK)      │  N:1  │ - email             │                    │
│   │ - participant_type  │        │ - name              │                    │
│   │   (USER/CONTACT/    │        │ - job_title ◀───────┼──── Enrichment     │
│   │    COMPANY)         │        │                     │                    │
│   │ - related_email     │        └─────────────────────┘                    │
│   │ - related_name      │                                                    │
│   │ - relationship_role │                                                    │
│   └─────────────────────┘                                                    │
│            │                                                                 │
│            │ source_reference_id                                             │
│            ▼                                                                 │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │                      DealStakeholder                                │   │
│   │                                                                     │   │
│   │ - stakeholder_id (PK)                                               │   │
│   │ - deal_id (FK)                                                      │   │
│   │ - email (unique per deal)                                           │   │
│   │ - name, job_title                                                   │   │
│   │                                                                     │   │
│   │ - source: CRM | CALENDAR_DISCOVERY | EMAIL_DISCOVERY | MANUAL       │   │
│   │ - source_reference_id (links back to CRMDealParticipant)            │   │
│   │ - approval_status: APPROVED | PENDING | REJECTED                    │   │
│   │                                                                     │   │
│   │ - stance: CHAMPION | SUPPORTER | NEUTRAL | DETRACTOR | BLOCKER      │   │
│   │ - decision_power: DECISION_MAKER | INFLUENCER | EVALUATOR | END_USER│   │
│   │ - temperature: HOT | WARM | COLD | UNKNOWN                          │   │
│   │ - engagement_score: float (0-100)                                   │   │
│   │ - last_contact_at: datetime                                         │   │
│   │                                                                     │   │
│   │ - stakeholder_engagement_history: JSONB (10 snapshots)              │   │
│   │ - is_deleted: bool (soft-delete)                                    │   │
│   └──────────────────────────────────┬──────────────────────────────────┘   │
│                                      │                                       │
│                                      │ 1:N                                   │
│                                      ▼                                       │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │                   DealStakeholderInsight                            │   │
│   │                                                                     │   │
│   │ - insight_id (PK)                                                   │   │
│   │ - deal_id (FK)                                                      │   │
│   │ - stakeholder_id (FK, optional)                                     │   │
│   │                                                                     │   │
│   │ - type: ENGAGEMENT_GAP | SENTIMENT_DECLINING | CHAMPION_OPPORTUNITY │   │
│   │         | UNADDRESSED_CONCERN | MISSING_DECISION_MAKER              │   │
│   │         | COVERAGE_GAP | STAKEHOLDER_RISK                           │   │
│   │                                                                     │   │
│   │ - priority: CRITICAL | HIGH | MEDIUM | LOW                          │   │
│   │ - status: ACTIVE | ACKNOWLEDGED | RESOLVED | EXPIRED                │   │
│   │                                                                     │   │
│   │ - title, description, recommendation                                │   │
│   │ - ai_confidence: float                                              │   │
│   │ - ai_reasoning: str                                                 │   │
│   │                                                                     │   │
│   │ - expires_at: datetime                                              │   │
│   │ - acknowledged_at, acknowledged_by                                  │   │
│   │ - resolved_at, resolved_by, resolution_notes                        │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Key Configuration

### Task Time Limits

| Task Tier | Soft Limit | Hard Limit | Max Retries | Backoff Base |
|-----------|------------|------------|-------------|--------------|
| Tier 1 (All Orgs) | 5 min | 6 min | 0 | N/A |
| Tier 2 (Org Level) | 10 min | 11 min | 2 | 60s |
| Tier 3 (Deal Level) | 2 min | 2.5 min | 3 | 30s |

### Queue Routing

| Task Pattern | Queue |
|--------------|-------|
| `stakeholder.*` | `deal` |
| `deal.match_*` | `deal` |
| `stakeholder.analyze_*` | `deal_jobs` |

### Important Constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `MIN_MATCH_SCORE` | 0.7 | Minimum score for email/calendar associations |
| `MAX_CALENDAR_DISCOVERY_AUTO_APPROVED` | 5 | Auto-approve first 5 calendar stakeholders |
| `MAX_EMAIL_DISCOVERY_AUTO_APPROVED` | 5 | Auto-approve first 5 email stakeholders |
| `EMAIL_BATCH_SIZE` | 500 | Emails processed per org run |
| `MAX_ENGAGEMENT_HISTORY_SNAPSHOTS` | 10 | Engagement history entries per stakeholder |

---

## File Reference

### Core Services

| Service | File Path |
|---------|-----------|
| StakeholderSyncService | `src/workers/deal_worker/services/stakeholder_sync_service.py` |
| DealMeetingMatcher | `src/workers/deal_worker/services/deal_meeting_matcher.py` |
| DealEmailMatcher | `src/workers/deal_worker/services/deal_email_matcher.py` |
| StakeholderAnalysisService | `src/workers/deal_worker/services/stakeholder_analysis_service.py` |

### Task Files

| Task Module | File Path |
|-------------|-----------|
| Stakeholder Tasks | `src/workers/deal_worker/tasks/deal_mappers/stakeholder_tasks.py` |
| Calendar Matching Tasks | `src/workers/deal_worker/tasks/deal_mappers/calendar_matching_tasks.py` |
| Email Matching Tasks | `src/workers/deal_worker/tasks/deal_mappers/email_matching_tasks.py` |
| Scheduled Tasks | `src/workers/deal_worker/tasks/scheduled_tasks.py` |

### Data Models

| Model | File Path |
|-------|-----------|
| DealStakeholder | `src/domain/models/organisation_tables/sync/crm/stakeholders.py` |
| DealStakeholderInsight | `src/domain/models/organisation_tables/sync/crm/stakeholders.py` |
| CalendarEventDealAssociation | `src/domain/models/organisation_tables/sync/calendar.py` |
| DealEmailAssociation | `src/domain/models/organisation_tables/sync/email_models.py` |

### Configuration

| Config | File Path |
|--------|-----------|
| DealWorkerConfig | `src/workers/deal_worker/config.py` |
| Beat Schedule | `src/workers/deal_worker/celery_app.py` |

---

## Related Documentation

| Document | Purpose |
|----------|---------|
| `stakeholder-identification.md` | How stakeholders are identified from sources |
| `deal-calendar-mapping.md` | Calendar event matching details |
| `deal-email-mapping-comprehensive.md` | Email matching details |
| `stakeholder-operations.md` | Original stakeholder sync guide |
| `stakeholder-analysis-tasks.md` | AI analysis implementation |
| `stakeholder-change-detection.md` | Change detection algorithm |
| `task-orchestration-map.md` | General task reference |

---

## Summary

The stakeholder system is a comprehensive pipeline that:

1. **Ingests** data from CRM, Calendar, and Email via Integration Worker
2. **Syncs** CRM participants as authoritative stakeholders
3. **Discovers** additional stakeholders from calendar and email participants
4. **Analyzes** stakeholders using AI to determine stance, decision power, and temperature
5. **Generates** actionable insights for sales teams
6. **Tracks** engagement history for trend analysis

All components work together through a three-tier task architecture with proper scheduling, retry mechanisms, and queue routing to ensure reliable processing at scale.
