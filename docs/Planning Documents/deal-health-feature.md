# Deal Health Feature - Design Document

## Overview

This document outlines the design for adding **Deal Health** as an AI-generated field to the existing Deal Summary workflow. The feature classifies each deal into a health category with an explanation, enabling sales teams to quickly assess deal status and prioritize their attention.

## Problem Statement

Pre-sales and sales teams need quick visibility into deal health status to:

1. **Prioritize attention** - Focus on deals that need immediate intervention
2. **Identify patterns** - Understand what's causing deals to stall or be at risk
3. **Take action** - Know the specific reasons behind a deal's health status
4. **Track progression** - Monitor how deal health changes over time

### Current State

| Component | Current Implementation |
|-----------|----------------------|
| **Deal Summary Agent** | `src/agents/workflows/deal_summary/` - Generates narrative summary |
| **Deal Summary Service** | `src/workers/deal_worker/services/deal_summary_service.py` - Orchestrates summary generation |
| **Deal Summary Tasks** | `src/workers/deal_worker/tasks/deal_summary_tasks.py` - Celery task processing |
| **CRMDeal Model** | `src/domain/models/organisation_tables/sync/crm/deals.py` - `deal_summary_data` JSONB column |
| **Deal List API** | `GET /users/crm/deals` - Returns `DealListItem` without summary/health |
| **Deal Detail API** | `GET /users/crm/deals/{deal_id}` - Returns `DealDetail` with `deal_summary_data` |
| **Basic Health** | Simple activity-based health in `deals_handler.py` (line ~135): "HEALTHY", "STALLED", or status-based |

### Gap Analysis

| Capability | Current State | Gap |
|------------|---------------|-----|
| Deal Health Classification | Basic (HEALTHY/STALLED based on activity) | Need AI-powered multi-category classification |
| Health Reasoning | None | Need explanation for the health status |
| Health in Deal List | Not included | Need to surface in list for quick scanning |
| Health from AI Analysis | Not generated | Need to be part of AI summary output |
| Persistence | Only in context (calculated) | Need to persist in `deal_summary_data` |

---

## Deal Health Categories

Based on the requirements, deal health should be classified into the following categories:

| # | Category | Core Definition | Primary Signals (Observable) | Underlying Failure Pattern | Recoverable? |
|---|----------|-----------------|------------------------------|---------------------------|--------------|
| 1 | **HEALTHY** | Deal progressing with validated value, active ownership, and forward momentum | Regular meetings scheduled, success criteria defined, validation milestones progressing, consistent stakeholder engagement | None. Normal execution | Yes |
| 2 | **AT_RISK** | Weak negative signals emerging without a single blocking issue | Slower response times, vague next steps, declining energy, repeated explanations | Loss of urgency or clarity beginning to form | Yes |
| 3 | **STALLED** | Deal has stopped advancing without an explicit rejection | No upcoming meetings, long silence, deferred decisions, paused validation | Decision ownership unclear or deprioritized | Sometimes |
| 4 | **CHAMPION_WEAK** | No effective internal advocate driving the deal forward | Champion absent, influence reduced, new stakeholders resetting context | Internal ownership failure | Sometimes |
| 5 | **BLOCKED** | Deal is stopped by a known, explicit external dependency | Security review pending, legal/procurement delays, missing requirement, budget approval outstanding | Process or constraint dependency | Yes |
| 6 | **MISALIGNED** | Stakeholders do not share a common definition of success | Conflicting priorities, inconsistent objections, demo direction keeps changing | Stakeholder alignment failure | Yes |
| 7 | **UNVALIDATED** | Interest exists but value has not been proven in the customer's context | No documented success criteria, generic demo, POC skipped or shallow | Assumed value instead of proven value | Yes |
| 8 | **COLD** | Engagement has dropped sharply with no active dialogue | Unanswered emails, postponed meetings, absence of urgency language | Deprioritization or internal stall | Rarely |
| 9 | **LATE_STAGE_RISK** | Deal appears close but exhibits fragility that threatens closure | Late stakeholder entry, last-minute requirements, pricing scrutiny, late security/legal | Late discovery of hidden constraints | Sometimes |
| 10 | **LIKELY_LOST** | Deal trajectory strongly indicates loss despite no formal rejection | Competitor language, timeline pushed indefinitely, evasive communication | Buyer decision made elsewhere | No |
| 11 | **ARTIFICIALLY_HEALTHY** | Deal appears healthy on surface metrics but lacks structural buying readiness | High activity with shallow depth, positive language without concrete commitments, demo praise without operational detail | False confidence / unchallenged assumptions | Yes |

> **Note:** `deal_health_reason` is captured by the AI but **not displayed in the UI** for the current release.

### Health Determination Factors

The AI agent should consider the following when determining deal health:

1. **Engagement Level**
   - Frequency and recency of meetings
   - Response times in email communications
   - Stakeholder participation in discussions

2. **Deal Progression**
   - Movement through sales stages
   - Time spent in current stage vs. typical
   - Close date changes (slipping vs. advancing)

3. **Stakeholder Signals**
   - Champion engagement strength
   - Decision-maker involvement
   - Blocker owner responsiveness

4. **Content Analysis**
   - Sentiment in communications
   - Technical concerns raised
   - Competitive mentions
   - Budget/timing discussions

5. **Risk Indicators**
   - Identified blockers and their severity
   - Unresolved concerns from meetings
   - Missing key stakeholders

---

## Architecture Design

### Approach: Extend Existing Deal Summary Agent

The Deal Health feature will be integrated into the existing Deal Summary workflow rather than creating a separate workflow. This approach:

- **Reuses existing tools** - Same data gathering tools (meetings, emails, stakeholders)
- **Single AI call** - One agent run produces both summary and health assessment
- **Consistent data** - Health is based on the same analysis as the summary
- **Efficient processing** - No duplicate data gathering or token usage

### Data Flow

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                         Deal Summary Generation Flow                          │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  1. API Trigger                                                              │
│     POST /users/crm/deals/{deal_id}/deal-summary/generate                    │
│                    │                                                         │
│                    ▼                                                         │
│  2. Celery Task                                                              │
│     process_deal_summary_job()                                               │
│                    │                                                         │
│                    ▼                                                         │
│  3. DealSummaryService                                                       │
│     - Load deal context                                                      │
│     - Build minimal input                                                    │
│     - Call DealSummaryAgent                                                  │
│                    │                                                         │
│                    ▼                                                         │
│  4. DealSummaryAgent (UPDATED)                                               │
│     - Gather stakeholder data                                                │
│     - Gather meeting transcripts                                             │
│     - Gather email communications                                            │
│     - Generate summary + HEALTH ASSESSMENT                                   │
│                    │                                                         │
│                    ▼                                                         │
│  5. DealSummaryData (UPDATED MODEL)                                          │
│     {                                                                        │
│       "deal_summary": "...",                                                 │
│       "deal_health": "ON_TRACK",           ← NEW                            │
│       "deal_health_reason": "..."          ← NEW                            │
│     }                                                                        │
│                    │                                                         │
│                    ▼                                                         │
│  6. Save to CRMDeal.deal_summary_data                                        │
│     {                                                                        │
│       "schema_version": "1.1",                                               │
│       "generated_at": "...",                                                 │
│       "data": {                                                              │
│         "deal_summary": "...",                                               │
│         "deal_health": "ON_TRACK",                                           │
│         "deal_health_reason": "..."                                          │
│       }                                                                      │
│     }                                                                        │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## Implementation Plan

### Phase 1: Update Agent Output Model

**File**: `src/agents/workflows/deal_summary/models.py`

```python
from enum import Enum

class DealHealthStatus(str, Enum):
    """AI-assessed deal health classification."""
    
    ON_TRACK = "ON_TRACK"           # Deal progressing well
    NEEDS_ATTENTION = "NEEDS_ATTENTION"  # Minor concerns
    AT_RISK = "AT_RISK"             # Significant concerns
    CRITICAL = "CRITICAL"           # Major blockers
    STALLED = "STALLED"             # No activity


class DealSummaryData(BaseModel):
    """Generated deal summary data with health assessment."""

    deal_summary: str = Field(
        ...,
        description="Comprehensive narrative summary of the deal",
    )
    
    deal_health: DealHealthStatus = Field(
        ...,
        description="AI-assessed health status of the deal",
    )
    
    deal_health_reason: str = Field(
        ...,
        description="Explanation for the health classification (2-3 sentences)",
    )
```

### Phase 2: Update Agent Prompt

**File**: `src/agents/workflows/deal_summary/prompts.py`

Add to the system prompt:

```python
DEAL_SUMMARY_SYSTEM_PROMPT = dedent(
    """
    You are a Sales Intelligence AI generating a concise deal briefing...
    
    # Output Requirements
    Generate a **100-200 word** summary with these sections:
    - **Business Context**: What is the customer trying to solve?
    - **Technical Scope**: Specific product requirements and blockers
    - **Deal Health**: Current momentum and recommended next steps
    
    # Deal Health Assessment (NEW)
    Additionally, assess the overall deal health:
    
    ## Health Categories:
    - **ON_TRACK**: Strong engagement, clear progression, positive signals
    - **NEEDS_ATTENTION**: Minor delays, reduced engagement, or small concerns
    - **AT_RISK**: Significant issues - budget concerns, timeline slips, champion weakness
    - **CRITICAL**: Major blockers - lost champion, competitive threat, executive pushback
    - **STALLED**: No activity in 14+ days, unresponsive stakeholders
    
    ## Assessment Criteria:
    1. Engagement frequency and recency (meetings, emails in last 14 days)
    2. Stakeholder participation (decision-makers involved?)
    3. Stage progression (moving forward or stuck?)
    4. Communication sentiment (positive, neutral, negative?)
    5. Open blockers or unresolved concerns
    
    Provide a 2-3 sentence `deal_health_reason` explaining the assessment.
    """
)
```

### Phase 3: Update Schema Version

**File**: `src/workers/deal_worker/services/deal_summary_service.py`

```python
# Increment schema version for new fields
SCHEMA_VERSION = "1.1"
```

### Phase 4: Update API Response Models

**File**: `src/api/schemas/crm_models.py`

#### 4.1 Update `DealListItem` - Add Health Fields for List API

```python
class DealListItem(BaseModel):
    """Individual deal item in a list response."""
    
    # ... existing fields ...
    
    # NEW: Deal Health (surfaced from deal_summary_data for list view)
    deal_health: str | None = Field(
        None,
        description="AI-assessed health: ON_TRACK, NEEDS_ATTENTION, AT_RISK, CRITICAL, STALLED"
    )
    deal_health_reason: str | None = Field(
        None,
        description="Brief explanation for the health status"
    )
```

#### 4.2 `DealDetail` Already Has `deal_summary_data`

The `DealDetail` model already includes `deal_summary_data` which will contain the new health fields. No changes needed to the response model, but we should document the new structure.

### Phase 5: Update API Handlers

**File**: `src/api/routers/crm/deals_handler.py`

#### 5.1 Update List Deals Response to Include Health

```python
@deals_router.get("/deals", ...)
async def list_deals(...):
    # ... existing code ...
    
    items = [
        {
            # ... existing fields ...
            "deal_id": deal.deal_id,
            "name": deal.name,
            # ... other fields ...
            
            # NEW: Extract health from deal_summary_data
            "deal_health": _extract_deal_health(deal.deal_summary_data),
            "deal_health_reason": _extract_deal_health_reason(deal.deal_summary_data),
        }
        for deal in deals
    ]


def _extract_deal_health(deal_summary_data: dict | None) -> str | None:
    """Extract deal_health from deal_summary_data if available."""
    if not deal_summary_data:
        return None
    data = deal_summary_data.get("data", {})
    return data.get("deal_health")


def _extract_deal_health_reason(deal_summary_data: dict | None) -> str | None:
    """Extract deal_health_reason from deal_summary_data if available."""
    if not deal_summary_data:
        return None
    data = deal_summary_data.get("data", {})
    return data.get("deal_health_reason")
```

### Phase 6: Update Database Query for Efficiency

**File**: `src/service/crm/deals_access_service.py`

Currently, `list_deals()` doesn't load `deal_summary_data`. We need to update it to include this field for extracting health:

```python
async def list_deals(...):
    # Base query now needs deal_summary_data for health extraction
    stmt = select(CRMDeal).where(...)
    
    # No change needed - SQLAlchemy loads all columns by default
    # The deal_summary_data is already on the model
```

**Note**: If `deal_summary_data` is large and impacts performance, we could create a computed/generated column for just the health status, or use a hybrid property. For MVP, loading the full column is acceptable.

---

## Files to Modify

### Summary of Changes

| File | Change Type | Description |
|------|-------------|-------------|
| `src/agents/workflows/deal_summary/models.py` | **Modify** | Add `DealHealthStatus` enum, add fields to `DealSummaryData` |
| `src/agents/workflows/deal_summary/prompts.py` | **Modify** | Update system prompt with health assessment criteria |
| `src/workers/deal_worker/services/deal_summary_service.py` | **Modify** | Update `SCHEMA_VERSION` to "1.1" |
| `src/api/schemas/crm_models.py` | **Modify** | Add `deal_health`, `deal_health_reason` to `DealListItem` |
| `src/api/routers/crm/deals_handler.py` | **Modify** | Add helper functions, update list deals response |
| `docs/API Documentation/deals/crm-deals-api.md` | **Modify** | Document new response fields |

### No Database Migration Required

The `deal_summary_data` is already a JSONB column. Adding new fields to the JSON structure doesn't require a migration. The schema_version field helps track the structure:

- `"1.0"` - Original: `{ deal_summary: string }`
- `"1.1"` - Updated: `{ deal_summary: string, deal_health: string, deal_health_reason: string }`

---

## API Response Examples

### List Deals Response (Updated)

```json
{
  "success": true,
  "message": "Deals retrieved successfully",
  "data": {
    "items": [
      {
        "deal_id": "uuid-1",
        "name": "Acme Corp - Enterprise License",
        "amount": 150000,
        "deal_status": "open",
        "stage_name": "Proposal",
        "owner_name": "John Smith",
        "deal_health": "ON_TRACK",
        "deal_health_reason": "Strong engagement with 3 meetings in the past 2 weeks. Decision-maker actively involved and technical evaluation progressing well."
      },
      {
        "deal_id": "uuid-2",
        "name": "TechStart - Pilot Program",
        "amount": 25000,
        "deal_status": "open",
        "stage_name": "Discovery",
        "owner_name": "Jane Doe",
        "deal_health": "NEEDS_ATTENTION",
        "deal_health_reason": "Last meeting was 10 days ago. Champion is responsive but decision-maker hasn't been involved in recent discussions."
      },
      {
        "deal_id": "uuid-3",
        "name": "BigBank - Integration Project",
        "amount": 500000,
        "deal_status": "open",
        "stage_name": "Negotiation",
        "owner_name": "Mike Johnson",
        "deal_health": "AT_RISK",
        "deal_health_reason": "Security concerns raised in last meeting remain unresolved. Competitor mentioned by procurement. Close date pushed back twice."
      }
    ],
    "cursor": "next-page-cursor"
  }
}
```

### Deal Detail Response (Already Includes Summary Data)

```json
{
  "success": true,
  "data": {
    "deal_id": "uuid-1",
    "name": "Acme Corp - Enterprise License",
    "deal_summary_status": "COMPLETED",
    "deal_summary_data": {
      "schema_version": "1.1",
      "generated_at": "2024-12-24T10:00:00Z",
      "data": {
        "deal_summary": "Acme Corp is evaluating our enterprise solution for their digital transformation initiative...",
        "deal_health": "ON_TRACK",
        "deal_health_reason": "Strong engagement with 3 meetings in the past 2 weeks. Decision-maker actively involved and technical evaluation progressing well."
      }
    },
    "context": {
      "health": "ON_TRACK"
    }
  }
}
```

---

## Backward Compatibility

### Handling Existing Deals

Deals with existing `deal_summary_data` (schema_version 1.0) will have:
- `deal_health`: `null` in list response
- `deal_health_reason`: `null` in list response

When the deal summary is regenerated, it will be updated to schema_version 1.1 with health fields populated.

### Client Handling

Frontend should handle `null` values gracefully:
- Show "Not Assessed" or a neutral indicator when `deal_health` is null
- Allow triggering summary regeneration to get health assessment

---

## Testing Strategy

### Unit Tests

1. **Model Tests** (`tests/agents/workflows/deal_summary/test_models.py`)
   - Test `DealHealthStatus` enum values
   - Test `DealSummaryData` with all fields

2. **Prompt Tests**
   - Verify prompt includes health assessment instructions

3. **Handler Tests** (`tests/api/routers/crm/test_deals_handler.py`)
   - Test `_extract_deal_health()` with valid data
   - Test `_extract_deal_health()` with null/missing data
   - Test list response includes health fields

### Integration Tests

1. **End-to-End Summary Generation**
   - Trigger summary generation
   - Verify response includes deal_health
   - Verify deal_health_reason is populated

2. **List Deals with Health**
   - Create deal with summary data
   - Verify list response includes health fields

---

## Future Enhancements

1. **Health History Tracking** - Store historical health assessments for trend analysis
2. **Health-Based Filtering** - Filter deal list by health status
3. **Health Alerts** - Notify when deal health degrades (e.g., ON_TRACK → AT_RISK)
4. **Health Dashboard** - Aggregate health metrics across portfolio
5. **Custom Health Rules** - Allow orgs to customize health criteria

---

## Implementation Checklist

- [ ] Update `DealSummaryData` model with health fields
- [ ] Create `DealHealthStatus` enum
- [ ] Update `DEAL_SUMMARY_SYSTEM_PROMPT` with health assessment criteria
- [ ] Update `build_deal_summary_user_prompt` to request health output
- [ ] Update `SCHEMA_VERSION` to "1.1"
- [ ] Update `DealListItem` schema with health fields
- [ ] Add `_extract_deal_health()` helper function
- [ ] Add `_extract_deal_health_reason()` helper function
- [ ] Update `list_deals` response to include health
- [ ] Update API documentation
- [ ] Write unit tests
- [ ] Write integration tests
- [ ] Test backward compatibility with existing deals
