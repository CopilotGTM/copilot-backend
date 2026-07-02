# Task Orchestration Map

> **Purpose**: This document provides a comprehensive reference map of all Celery tasks, AI agents, and their relationships. It's designed for LLMs and developers to understand how different components interact and trigger each other.

## Quick Reference

### All Celery Task Names

```
# Integration Worker Tasks
integrations.sync_integration_data          # Main sync task for all providers
integrations.sync_calendar_integration      # Calendar single-integration sync
integrations.sync_email_integration         # Email single-integration sync
integrations.refresh_expiring_tokens        # OAuth token refresh
integrations.refresh_single_integration     # Manual token refresh
integrations.sync_teams_channels            # Teams channels and chats sync

# Deal Worker Tasks - Matching
deal.process_deal_meetings_task             # Match meetings to deals (org-wide)
deal.process_single_deal_task               # Match meetings for single deal
deal.process_deal_emails_task               # Match emails to deals (org-wide)
deal.trigger_deal_meeting_processing_all_orgs  # Periodic trigger for all orgs
deal.trigger_deal_email_processing_all_orgs    # Periodic trigger for all orgs

# Deal Worker Tasks - Stakeholders
stakeholder.sync_from_meeting_task          # Sync stakeholders from meeting
stakeholder.analyze_deal_stakeholders_task  # AI analysis for deal
stakeholder.analyze_org_stakeholders_task   # AI analysis for entire org
stakeholder.trigger_analysis_all_orgs       # Periodic trigger for all orgs

# Deal Worker Tasks - AI Jobs
deal_meeting_task.process                   # Process meeting jobs (briefing/summary)
deal_summary_task.process                   # Generate deal summary

# Deal Worker Tasks - Risk/Blocker
risk_blocker.extract_deal_risks_blockers_task  # AI extraction for deal
risk_blocker.trigger_extraction_all_orgs       # Periodic trigger for all orgs

# Knowledge Worker Tasks
knowledge.post_upload                       # Post-upload processing
knowledge.document_extraction               # Heavy document extraction
knowledge.delete_source                     # Delete source and cleanup

# Chat Worker Tasks
chat.process_message                        # Process chat message with agent
```

---

## Task Categories

### 1. Data Sync Tasks (Integration Worker)

These tasks fetch data from external providers via Nango webhooks.

| Task | Function | Trigger |
|------|----------|---------|
| `integrations.sync_integration_data` | Sync data from external providers | Webhook from Nango |
| `integrations.sync_calendar_integration` | Single calendar integration sync | Event/post-connect/manual refresh |
| `integrations.sync_email_integration` | Single email integration sync | Event/post-connect/manual refresh |
| `integrations.sync_teams_channels` | Sync Teams channels and chats | Periodic (3h) or manual |
| `integrations.refresh_expiring_tokens` | Refresh OAuth tokens | Periodic (1h) |

**Categories and Sync Names**:

| Category | Provider | Sync Names |
|----------|----------|------------|
| `calendar` | google, microsoft | events |
| `email` | google, microsoft | emails, messages |
| `crm` | hubspot, salesforce | deals, contacts, companies, users, notes, activities, pipelines |
| `messaging` | microsoft (Teams) | channels, chats, messages |

---

### 2. Deal Matching Tasks (Deal Worker)

These tasks associate external data (meetings, emails) with CRM deals.

| Task | Input | Output | Notes |
|------|-------|--------|-------|
| `deal.process_deal_meetings_task` | org_id | Associations created | Processes all deals in org |
| `deal.process_single_deal_task` | org_id, deal_id | Associations created | Single deal processing |
| `deal.process_deal_emails_task` | org_id | Associations created | Processes all emails in org |

**Matching Algorithm**:

- Team member participation (internal users on deal)
- Contact/stakeholder participation (external contacts on deal)
- Domain matching (email domains matching deal contacts)

---

### 3. Stakeholder Tasks (Deal Worker)

Stakeholders are external people involved in deals (customers, partners).

| Task | Purpose | Uses AI |
|------|---------|---------|
| `stakeholder.sync_from_meeting_task` | Sync attendees as stakeholders | No |
| `stakeholder.analyze_deal_stakeholders_task` | AI analysis for deal | Yes |
| `stakeholder.analyze_org_stakeholders_task` | AI analysis for org | Yes |
| `stakeholder.trigger_analysis_all_orgs` | Periodic trigger | No |

**Stakeholder Analysis Output**:

- Stance: Champion, Supporter, Neutral, Blocker, Unknown
- Decision Power: Final Decision, Influencer, Recommender, End User
- Temperature: Hot, Warm, Cold
- Champion Potential: Score 0.0 - 1.0
- Motivations & Pain Points: Text insights

---

### 5. AI Job Tasks (Deal Worker)

These tasks use AI agents to generate content.

| Task | Job Types | Agent Used |
|------|-----------|------------|
| `deal_meeting_task.process` | MEETING_BRIEFING, MEETING_SUMMARY, STAKEHOLDER_ANALYSIS | BriefingAgent, DealSummaryAgent |
| `deal_summary_task.process` | Deal summary generation | DealSummaryAgent |
| `risk_blocker.extract_deal_risks_blockers_task` | Risk/blocker extraction | RiskBlockerExtractionAgent |

---

## AI Agents Reference

### BriefingAgent

**Location**: `src/agents/workflows/briefing/agent.py`

**Purpose**: Generate pre-meeting preparation briefing

**Input**: `BriefingInput` - deal_id, event_id, current_datetime

**Output**: `BriefingData` - structured briefing content

**Tools Used**:

- Meeting context fetching
- Deal information retrieval
- Stakeholder lookup
- Prior meeting history

**Model**: GPT-5 (Azure OpenAI)

---

### DealSummaryAgent

**Location**: `src/agents/workflows/deal_summary/agent.py`

**Purpose**: Generate comprehensive deal summary from all sources

**Input**: `DealSummaryInput` - deal_id

**Output**: `DealSummaryData` - structured summary

**Tools Used**:

- Stakeholder information
- Meeting transcripts
- Email summaries
- Deal context

**Model**: GPT-5 (Azure OpenAI)

---

### StakeholderAnalysisAgent

**Location**: `src/agents/workflows/stakeholder_analysis/agent.py`

**Purpose**: Analyze stakeholder insights from meeting transcripts

**Input**: Context string with stakeholders and transcripts

**Output**: `DealStakeholdersAnalysisData` - analysis for all stakeholders

**Model**: GPT-5-mini (Azure OpenAI)

**Notes**: Analyzes ALL stakeholders in single LLM call for efficiency

---

### RiskBlockerExtractionAgent

**Location**: `src/agents/workflows/risk_blocker_extraction/agent.py`

**Purpose**: Extract risks and blockers from deal communications

**Input**: `RiskBlockerExtractionInput` - deal_id

**Output**: `RiskBlockerExtractionResult` - identified risks/blockers

**Tools Used**:

- Meeting transcripts
- Email content
- Existing risks/blockers (for deduplication)

**Model**: GPT-5 (Azure OpenAI)

---

## Trigger Chains

### Chain 1: Calendar Sync → Meeting Processing

```
Webhook: Calendar event created/updated
    ↓
integrations.sync_integration_data (calendar/events)
    ↓
[After initial sync] deal.process_deal_meetings_task
    ↓
DealMeetingMatcher.process_deal()
    ↓
├── Create DealMeetingAssociation
├── Create TimelineEvent (INLINE)
└── [Enqueue] stakeholder.sync_from_meeting_task
```

### Chain 1B: Calendar Integration Sync → Meeting Processing

```
Event/post-connect/manual dispatch
    ↓
integrations.sync_calendar_integration
    ↓
fetch provider events (Google syncToken / Outlook deltaLink)
    ↓
upsert_calendar_incremental()
    ↓
update KnowledgeIntegration.last_synced_at + connection_metadata cursors
    ↓
deal.match_calendar_to_deals_org
    ↓
stakeholder.sync_from_meeting_task (for newly matched meetings)
```

### Chain 2: Email Sync → Email Processing

```
Webhook: Email received
    ↓
integrations.sync_integration_data (email/emails)
    ↓
[After initial sync] deal.process_deal_emails_task
    ↓
DealEmailMatcher.process_deal_emails()
    ↓
├── Create DealEmailAssociation
├── Create TimelineEvent (INLINE)
└── Extract participants
```

### Chain 3: CRM Sync → Deal Enrichment

```
Webhook: CRM data updated
    ↓
integrations.sync_integration_data (crm/contacts)
    ↓
_enrich_participants_after_crm_sync()
    ↓
├── deal.process_deal_meetings_task
└── deal.process_deal_emails_task
```

### Chain 4: User Requests Briefing

```
API: POST /deals/{deal_id}/meetings/{event_id}/briefing
    ↓
Create DealJob (MEETING_BRIEFING)
    ↓
deal_meeting_task.process
    ↓
handle_meeting_briefing()
    ↓
BriefingAgent.generate()
    ↓
Return BriefingData
```

### Chain 5: User Requests Deal Summary

```
API: POST /deals/{deal_id}/summary
    ↓
Create DealJob (DEAL_SUMMARY)
    ↓
deal_summary_task.process
    ↓
DealSummaryService.generate_summary()
    ↓
├── Load stakeholders
├── Fetch all transcripts
├── Build context
└── DealSummaryAgent.generate()
    ↓
Return DealSummaryData
```

---

## Periodic Task Schedule

### Integration Worker (Every Hour)

```
:00 - integrations.refresh_expiring_tokens
```

### Integration Worker (Every 3 Hours)

```
:00 - integrations.sync_teams_channels
```

### Deal Worker (Every 6 Hours)

```
:00 - deal.trigger_deal_meeting_processing_all_orgs
:00 - deal.trigger_deal_email_processing_all_orgs
:00 - stakeholder.trigger_analysis_all_orgs
```

### Deal Worker (Daily)

```
05:25 UTC - risk_blocker.trigger_extraction_all_orgs
```

---

## Queue Routing

### Integration Worker Queues

| Queue | Tasks |
|-------|-------|
| `integrations` | Most sync tasks, token refresh |
| `integrations_priority` | Manual refresh, urgent syncs |

### Deal Worker Queues

| Queue | Tasks |
|-------|-------|
| `deal` | Matching tasks, timeline sync, triggers |
| `deal_jobs` | AI jobs (briefing, summary, analysis) |

### Knowledge Worker Queues

| Queue | Tasks |
|-------|-------|
| `knowledge` | Light extraction tasks |
| `knowledge_heavy` | Heavy document extraction (PDF/DOC) |

### Chat Worker Queues

| Queue | Tasks |
|-------|-------|
| `chat` | Standard message processing |
| `chat_priority` | Priority message processing |

---

## Error Handling

### Retry Configuration

| Task Type | Max Retries | Backoff |
|-----------|-------------|---------|
| Sync tasks | 3 | 60s exponential |
| Matching tasks | 3 | 60s exponential |
| AI jobs | 3 | 60s exponential |
| Document extraction | 3 | 60s exponential |

### Non-Retryable Errors

- `DealNotFoundError` - Deal doesn't exist
- `NoMeetingsError` - No meetings for deal
- `NoTranscriptsError` - No transcripts available
- `NonRetryableJobError` - Permanent job failure

---

## Environment Variables

```bash
# Redis (for task queue)
REDIS_URL=redis://localhost:6379/0

# Celery Configuration
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/0

# Worker Concurrency
DEAL_WORKER_CONCURRENCY=4
INTEGRATIONS_WORKER_CONCURRENCY=8
KNOWLEDGE_WORKER_CONCURRENCY=2

# Time Limits
TASK_TIME_LIMIT=300  # 5 minutes
TASK_SOFT_TIME_LIMIT=240  # 4 minutes
```

---

## Monitoring

### Flower Dashboard

Access at: `http://localhost:5555`

### Key Metrics

- Task success/failure rates
- Queue lengths
- Worker status
- Task execution times

### Logging

All tasks use correlation IDs for tracing:

```python
from src.infra.observability.context import correlation_headers

task.apply_async(args=[...], headers=correlation_headers())
```

---

## Future Enhancements

1. **Auto-Briefing Generation**: Schedule briefings 24h before meetings
2. **Real-time Stakeholder Updates**: Trigger analysis after transcript sync
3. **Event-driven Architecture**: Move from periodic to webhook-driven triggers
4. **Task Chaining**: Use Celery chains for sequential workflows
5. **Priority Queuing**: Implement priority-based task routing

<!-- Minor: whitespace/documentation update committed on feature branch -->
