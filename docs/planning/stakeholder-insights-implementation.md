# Stakeholder AI Insights Generation - Implementation Plan

## Overview

This plan implements AI-generated insights for stakeholders that appear in the "AI Insights" panel on the Stakeholders page. Insights include engagement gaps, sentiment decline detection, champion opportunities, and unaddressed concerns.

**Key Design Decisions:**

1. **Single `actions` JSONB column** - Contains array of `{action_type, label, parameters}` objects
2. **Integrated into StakeholderAnalysisService** - Insights generated during stakeholder analysis, comparing previous vs current state
3. **Two separate columns for stakeholder extended data:**
   - `stakeholder_data` - General extended data for future extensibility
   - `stakeholder_engagement_history` - Dedicated time-series tracking for engagement history

---

## Phase 1: Schema & Data Model

### 1.1 Create DealStakeholderInsight Model

**File:** `src/domain/models/organisation_tables/sync/stakeholder_insight_models.py`

```python
# Enums
class StakeholderInsightType(str, Enum):
    ENGAGEMENT_GAP = "ENGAGEMENT_GAP"
    SENTIMENT_DECLINING = "SENTIMENT_DECLINING"
    CHAMPION_OPPORTUNITY = "CHAMPION_OPPORTUNITY"
    UNADDRESSED_CONCERN = "UNADDRESSED_CONCERN"
    MISSING_DECISION_MAKER = "MISSING_DECISION_MAKER"
    COVERAGE_GAP = "COVERAGE_GAP"
    STAKEHOLDER_RISK = "STAKEHOLDER_RISK"

class StakeholderInsightStatus(str, Enum):
    ACTIVE = "ACTIVE"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"
    EXPIRED = "EXPIRED"

class StakeholderInsightPriority(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

class InsightActionType(str, Enum):
    SCHEDULE_CALL = "SCHEDULE_CALL"
    SEND_EMAIL = "SEND_EMAIL"
    VIEW_DETAILS = "VIEW_DETAILS"
    VIEW_PROFILE = "VIEW_PROFILE"
    GENERATE_BRIEFING = "GENERATE_BRIEFING"

# Model
class DealStakeholderInsight(SQLModel, table=True):
    __tablename__ = "deal_stakeholder_insights"

    insight_id: UUID (PK)
    organisation_id: UUID (indexed)
    deal_id: UUID (FK to crm_deals, indexed)
    stakeholder_id: UUID | None (FK to deal_stakeholders, indexed)

    insight_type: StakeholderInsightType
    status: StakeholderInsightStatus (default: ACTIVE)
    priority: StakeholderInsightPriority (default: MEDIUM)

    title: str (max 500 chars)
    description: str | None

    # WHY the insight was generated
    ai_reasoning: str (required)
    ai_confidence: float (0.0 - 1.0)

    # Actions as single JSONB array
    # Format: [{"action_type": "SCHEDULE_CALL", "label": "Schedule Call", "parameters": {}}]
    actions: list[dict] (JSONB, default: [])

    # Source attribution
    source_type: str | None (e.g., "analysis_run", "risk_blocker")
    source_record_id: str | None
    related_risk_blocker_id: UUID | None

    # Stakeholder context (denormalized for API efficiency)
    stakeholder_name: str | None
    stakeholder_title: str | None
    stakeholder_email: str | None

    # Metrics (evidence)
    metric_value: float | None (e.g., 21 days)
    metric_unit: str | None (e.g., "days")
    threshold_value: float | None (e.g., 14 days threshold)
    trend_direction: str | None ("declining", "improving", "stable")

    # Timestamps
    identified_at: datetime
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime

    # Extended data (audit trail, comparison data)
    insight_data: dict (JSONB, default: {})
```

### 1.2 Add Columns to DealStakeholder

**File:** `src/domain/models/organisation_tables/sync/crm/stakeholders.py`

Add two new columns:

```python
# General extended data for future extensibility
stakeholder_data: dict[str, Any] | None = Field(
    default=None,
    sa_column=Column(JSONB, nullable=True),
    description="General extended data for future extensibility."
)

# Dedicated engagement history tracking
stakeholder_engagement_history: list[dict[str, Any]] | None = Field(
    default=None,
    sa_column=Column(JSONB, nullable=True),
    description="Time-series engagement history for tracking changes."
)
```

Engagement History Structure:

```json
[
  {
    "recorded_at": "2025-01-06T10:00:00Z",
    "stance": "CHAMPION",
    "temperature": "HOT",
    "engagement_score": 85.0,
    "champion_potential": 90.0,
    "decision_power": "DECISION_MAKER"
  }
]
```

### 1.3 Create Migration

**File:** `migrations/org_migrations/versions/3c7f79637776_024_stakeholder_insights.py`

- Create `deal_stakeholder_insights` table
- Add `stakeholder_data` column to `deal_stakeholders`
- Add `stakeholder_engagement_history` column to `deal_stakeholders`
- Add indexes for common query patterns

---

## Phase 2: Insight Generation (Core Logic)

### 2.1 Modify StakeholderAnalysisService

**File:** `src/workers/deal_worker/services/stakeholder_analysis_service.py`

Add new methods:

```python
async def _save_analysis_snapshot(
    self,
    stakeholder: DealStakeholder,
) -> None:
    """Save current values to history before update."""

async def _get_previous_analysis(
    self,
    stakeholder: DealStakeholder,
) -> dict | None:
    """Get most recent history entry for comparison."""

async def _generate_insights_for_deal(
    self,
    deal: CRMDeal,
    stakeholders: list[DealStakeholder],
    analysis_results: dict[UUID, StakeholderAnalysisData | None],
    previous_snapshots: dict[UUID, dict | None],
) -> list[DealStakeholderInsight]:
    """Generate all insights by comparing previous vs current."""
```

### 2.2 Insight Detection Methods

Each returns `DealStakeholderInsight | None`:

| Method | Trigger Condition | Priority |
|--------|-------------------|----------|
| `_detect_engagement_gap()` | DECISION_MAKER + last_contact > 21 days | HIGH |
| `_detect_sentiment_decline()` | engagement_score dropped > 20% OR stance downgrade | MEDIUM |
| `_detect_champion_opportunity()` | champion_potential >= 75 AND stance != CHAMPION | MEDIUM |
| `_detect_missing_decision_maker()` | No DECISION_MAKER in stakeholder list | HIGH |
| `_detect_coverage_gap()` | Missing key departments (IT, Legal, Finance) | LOW |
| `_detect_unaddressed_concern()` | Open DealRiskBlocker with SECURITY category | HIGH |

### 2.3 Modified Analysis Flow

```
analyze_all_stakeholders_for_deal(deal):
    1. Get stakeholders
    2. Get meetings/transcripts
    3. For each stakeholder:
       - previous = _get_previous_analysis(stakeholder)
    4. Run LLM analysis
    5. For each stakeholder:
       - _save_analysis_snapshot(stakeholder)  # BEFORE update
       - update_stakeholder_with_analysis(stakeholder, analysis)
    6. _expire_old_insights(deal_id)
    7. insights = _generate_insights_for_deal(deal, stakeholders, results, previous)
    8. Save new insights to DB
    9. Return results
```

---

## Phase 3: Service Layer

### 3.1 Create StakeholderInsightService

**File:** `src/service/crm/stakeholder_insight_service.py`

Following `risk_blocker_service.py` pattern:

```python
class StakeholderInsightService:
    async def list_for_deal(
        deal_id: UUID,
        status: list[StakeholderInsightStatus] | None = None,
        priority: StakeholderInsightPriority | None = None,
        include_expired: bool = False,
        limit: int = 20,
    ) -> StakeholderInsightListResponse

    async def get_by_id(insight_id: UUID) -> DealStakeholderInsight | None

    async def acknowledge(insight_id: UUID) -> DealStakeholderInsight

    async def dismiss(insight_id: UUID) -> DealStakeholderInsight

    async def resolve(insight_id: UUID, resolution_notes: str | None) -> DealStakeholderInsight

    async def expire_stale_insights(deal_id: UUID) -> int
```

---

## Phase 4: API Layer

### 4.1 Create API Schemas

**File:** `src/api/schemas/stakeholder_insight_models.py`

```python
class InsightAction(BaseModel):
    action_type: InsightActionType
    label: str
    parameters: dict = {}

class StakeholderInsightResponse(BaseModel):
    insight_id: UUID
    insight_type: StakeholderInsightType
    priority: StakeholderInsightPriority
    status: StakeholderInsightStatus
    title: str
    description: str | None
    ai_reasoning: str
    stakeholder_id: UUID | None
    stakeholder_name: str | None
    stakeholder_title: str | None
    actions: list[InsightAction]
    metric_value: float | None
    metric_unit: str | None
    identified_at: datetime

class StakeholderInsightListResponse(BaseModel):
    insights: list[StakeholderInsightResponse]
    total_count: int
    summary: InsightSummary

class InsightSummary(BaseModel):
    critical_count: int
    high_count: int
    medium_count: int
    low_count: int
```

### 4.2 Create API Handler

**File:** `src/api/routers/crm/stakeholder_insights_handler.py`

```python
@router.get("/{deal_id}/stakeholder-insights")
async def get_deal_stakeholder_insights(
    deal_id: UUID,
    status: list[StakeholderInsightStatus] | None = Query(None),
    priority: StakeholderInsightPriority | None = None,
    limit: int = 20,
) -> GenericResponse[StakeholderInsightListResponse]

@router.post("/{deal_id}/stakeholder-insights/{insight_id}/acknowledge")
async def acknowledge_insight(deal_id: UUID, insight_id: UUID)

@router.post("/{deal_id}/stakeholder-insights/{insight_id}/dismiss")
async def dismiss_insight(deal_id: UUID, insight_id: UUID)

@router.post("/{deal_id}/stakeholder-insights/{insight_id}/resolve")
async def resolve_insight(deal_id: UUID, insight_id: UUID, body: ResolveRequest)
```

---

## Phase 5: Agent Tool (Optional)

### 5.1 Create Insight Tool

**File:** `src/agents/tools/crm/list_stakeholder_insights.py`

Following `list_stakeholders.py` pattern - factory + core function.

---

## Files Summary

### New Files to Create (7)

| File | Purpose |
|------|---------|
| `src/domain/models/organisation_tables/sync/stakeholder_insight_models.py` | Data model & enums |
| `src/service/crm/stakeholder_insight_service.py` | Service layer (CRUD, status transitions) |
| `src/api/schemas/stakeholder_insight_models.py` | Pydantic API schemas |
| `src/api/routers/crm/stakeholder_insights_handler.py` | REST API endpoints |
| `src/agents/tools/crm/list_stakeholder_insights.py` | Agent tool |
| `migrations/org_migrations/versions/xxx_add_stakeholder_insights.py` | DB migration |
| `docs/planning/stakeholder-insights-implementation.md` | This plan |

### Files to Modify (5)

| File | Changes |
|------|---------|
| `src/domain/models/organisation_tables/sync/crm/stakeholders.py` | Add `stakeholder_data` JSONB |
| `src/workers/deal_worker/services/stakeholder_analysis_service.py` | Add insight generation logic |
| `src/domain/models/organisation_tables/sync/__init__.py` | Export new models |
| `src/api/routers/crm/__init__.py` | Register insights router |
| `src/agents/tools/registry.py` | Register new tool |

---

## Insight Rules Reference

| Type | Condition | Priority | Actions |
|------|-----------|----------|---------|
| ENGAGEMENT_GAP | decision_power=DECISION_MAKER AND last_contact > 21 days | HIGH | SCHEDULE_CALL, SEND_EMAIL |
| SENTIMENT_DECLINING | engagement_score dropped >20% OR stance downgrade | MEDIUM | VIEW_DETAILS |
| CHAMPION_OPPORTUNITY | champion_potential >= 75 AND stance != CHAMPION | MEDIUM | VIEW_PROFILE, GENERATE_BRIEFING |
| UNADDRESSED_CONCERN | Open risk_blocker with category=SECURITY/BUDGET/etc | HIGH | GENERATE_BRIEFING, VIEW_DETAILS |
| MISSING_DECISION_MAKER | No stakeholder with decision_power=DECISION_MAKER | HIGH | VIEW_DETAILS |
| COVERAGE_GAP | Key department missing (IT, Legal, Finance) | LOW | VIEW_DETAILS |
| STAKEHOLDER_RISK | Champion stance changed to NEUTRAL/DETRACTOR | CRITICAL | VIEW_PROFILE, SCHEDULE_CALL |

---

## History Tracking - Implemented Approach

### Two-Column Solution (IMPLEMENTED)

We chose a hybrid approach with two dedicated columns:

1. **`stakeholder_data`** (JSONB dict)
   - General purpose extended data
   - Future extensibility for misc data
   - Example: `{"custom_field": "value"}`

2. **`stakeholder_engagement_history`** (JSONB array)
   - Dedicated time-series tracking
   - Keeps last 10 snapshots per stakeholder
   - Direct access: `stakeholder.stakeholder_engagement_history`
   - No nested access needed

**Benefits of this approach:**

- Clear separation of concerns
- Better query performance for history lookups
- Self-documenting schema
- Type-safe column access in code
- Easy to add GIN index if needed for analytics

---

## API Response Example

```json
{
  "success": true,
  "data": {
    "insights": [
      {
        "insight_id": "uuid",
        "insight_type": "ENGAGEMENT_GAP",
        "priority": "HIGH",
        "status": "ACTIVE",
        "title": "The decision maker hasn't been engaged in 3 weeks",
        "description": "Arun Patel (VP Technology) hasn't been contacted in 3 weeks. Risk of deal stalling.",
        "ai_reasoning": "Stakeholder has decision_power=DECISION_MAKER and last_contact_at=2024-12-15, which is 22 days ago. This exceeds the 21-day engagement threshold for decision makers.",
        "stakeholder_id": "uuid",
        "stakeholder_name": "Arun Patel",
        "stakeholder_title": "VP Technology",
        "actions": [
          {"action_type": "SCHEDULE_CALL", "label": "Schedule Call", "parameters": {}},
          {"action_type": "SEND_EMAIL", "label": "Drop an Email", "parameters": {}}
        ],
        "metric_value": 22,
        "metric_unit": "days",
        "identified_at": "2025-01-06T10:00:00Z"
      }
    ],
    "total_count": 4,
    "summary": {
      "critical_count": 0,
      "high_count": 2,
      "medium_count": 2,
      "low_count": 0
    }
  }
}
```

---

## Implementation Order

1. **Phase 1:** Create model + migration + add stakeholder columns ✅
2. **Phase 2:** Add insight generation to StakeholderAnalysisService ✅
3. **Phase 3:** Create StakeholderInsightService ✅
4. **Phase 4:** Create API schemas + handler ✅
5. **Phase 5:** Create agent tool (if needed) ✅
6. **Phase 6:** Testing

---

## Final Architecture: Agent-Generated Structured Insights

### Design Principle

**The agent decides everything. The service only persists.**

- Agent outputs fully structured insights (type, priority, title, description, reasoning)
- Service layer does zero manual mapping or hardcoding
- All intelligence comes from the LLM, not from code

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    AGENT LAYER                                  │
│                                                                 │
│  StakeholderAnalysisAgent                                       │
│              ↓                                                  │
│  Outputs:                                                       │
│    1. Stakeholder attributes (stance, decision_power, etc.)     │
│    2. Structured insights (type, priority, title, reasoning)    │
│                                                                 │
│  Agent decides:                                                 │
│    - What insights to generate                                  │
│    - Insight type (ENGAGEMENT_GAP, STAKEHOLDER_RISK, etc.)      │
│    - Priority (CRITICAL, HIGH, MEDIUM, LOW)                     │
│    - Title and description                                      │
│    - Reasoning for the insight                                  │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                    SERVICE LAYER                                │
│                                                                 │
│  _save_insights_from_agent()                                    │
│    - Direct mapping: AgentInsight → DealStakeholderInsight      │
│    - No hardcoding of type, priority, or any other field        │
│    - Service only adds: insight_id, organisation_id, timestamps │
└─────────────────────────────────────────────────────────────────┘
```

---

## Phased Implementation Plan

### Phase 1: Update Agent Models

**File:** `src/agents/workflows/stakeholder_analysis/models.py`

**Changes:**

1. Create `AgentInsight` schema with all required fields
2. Add `insights: list[AgentInsight]` to `DealStakeholdersAnalysisData`
3. Remove `concerns` and `opportunities` from `StakeholderAnalysisData`

**New Schema:**

```python
class AgentInsight(BaseModel):
    """Structured insight generated by the agent."""

    stakeholder_email: str | None = Field(
        None,
        description="Email of related stakeholder, or null for deal-level insights"
    )
    insight_type: str = Field(
        ...,
        description="Type: ENGAGEMENT_GAP, SENTIMENT_DECLINING, CHAMPION_OPPORTUNITY, STAKEHOLDER_RISK, MISSING_DECISION_MAKER, COVERAGE_GAP, UNADDRESSED_CONCERN"
    )
    priority: str = Field(
        ...,
        description="Priority: CRITICAL, HIGH, MEDIUM, LOW"
    )
    title: str = Field(
        ...,
        description="Short title for the insight (max 100 chars)"
    )
    description: str = Field(
        ...,
        description="Detailed description of the insight"
    )
    reasoning: str = Field(
        ...,
        description="Why this insight was generated - evidence from transcripts"
    )


class DealStakeholdersAnalysisData(BaseModel):
    """Batch analysis results for all stakeholders in a deal."""

    stakeholders: list[StakeholderAnalysisData]
    insights: list[AgentInsight] = Field(
        default_factory=list,
        description="AI-generated insights for the deal and its stakeholders"
    )
```

---

### Phase 2: Update Agent Prompts

**File:** `src/agents/workflows/stakeholder_analysis/prompts.py`

**Changes:**

1. Add structured insight generation instructions
2. Define insight types and when to use each
3. Define priority levels and criteria
4. Provide examples of good insights

**Key Additions to Prompt:**

```
## INSIGHT GENERATION

After analyzing stakeholders, generate actionable insights for the sales team.

### Insight Types (use exactly these values):
- ENGAGEMENT_GAP: Decision maker or key stakeholder not engaged recently
- SENTIMENT_DECLINING: Champion or supporter showing reduced enthusiasm
- CHAMPION_OPPORTUNITY: High-potential stakeholder who could become a champion
- STAKEHOLDER_RISK: Red flags, competitive mentions, budget concerns, blockers
- MISSING_DECISION_MAKER: No clear decision maker identified
- COVERAGE_GAP: Key departments not represented
- UNADDRESSED_CONCERN: Important concern raised but not addressed

### Priority Levels:
- CRITICAL: Immediate action required, deal at risk
- HIGH: Should be addressed this week
- MEDIUM: Important but not urgent
- LOW: Good to know, act when convenient

### What Makes a Good Insight:
- Specific and actionable (not vague)
- Based on evidence from transcripts
- Includes clear reasoning
- Has appropriate priority based on deal impact
```

---

### Phase 3: Update Service Layer

**File:** `src/workers/deal_worker/services/stakeholder_analysis_service.py`

**Changes:**

1. Rename `_generate_insights_from_analysis()` to `_save_insights_from_agent()`
2. Remove all hardcoded values (type, priority, etc.)
3. Direct mapping from `AgentInsight` to `DealStakeholderInsight`
4. Service only adds: `insight_id`, `organisation_id`, `deal_id`, `identified_at`, `status`, `actions`

**New Method:**

```python
def _save_insights_from_agent(
    self,
    deal: CRMDeal,
    stakeholders: list[DealStakeholder],
    agent_insights: list[AgentInsight],
) -> list[DealStakeholderInsight]:
    """Save agent-generated insights to database.

    Direct mapping - no hardcoding. Agent decides type, priority, everything.
    """
    insights = []
    email_to_stakeholder = {s.email.lower(): s for s in stakeholders}

    for agent_insight in agent_insights:
        # Find stakeholder if specified
        stakeholder = None
        if agent_insight.stakeholder_email:
            stakeholder = email_to_stakeholder.get(agent_insight.stakeholder_email.lower())

        insight = DealStakeholderInsight(
            insight_id=uuid4(),
            organisation_id=self.organisation_id,
            deal_id=deal.deal_id,
            stakeholder_id=stakeholder.stakeholder_id if stakeholder else None,

            # All from agent - NO HARDCODING
            insight_type=StakeholderInsightType(agent_insight.insight_type),
            priority=StakeholderInsightPriority(agent_insight.priority),
            title=agent_insight.title,
            description=agent_insight.description,
            ai_reasoning=agent_insight.reasoning,

            # Service adds these
            status=StakeholderInsightStatus.ACTIVE,
            ai_confidence=0.8,
            actions=self._get_default_actions(agent_insight.insight_type),
            source_type="agent",
            identified_at=utcnow(),

            # Stakeholder context if available
            stakeholder_name=stakeholder.name if stakeholder else None,
            stakeholder_title=stakeholder.job_title if stakeholder else None,
            stakeholder_email=stakeholder.email if stakeholder else None,
        )
        insights.append(insight)

    return insights
```

---

### Phase 4: Update Main Analysis Flow

**File:** `src/workers/deal_worker/services/stakeholder_analysis_service.py`

**Changes:**

1. Update `analyze_all_stakeholders_for_deal()` to use new method
2. Pass `batch_result.insights` to `_save_insights_from_agent()`

```python
# In analyze_all_stakeholders_for_deal():

# Generate insights from agent output
if generate_insights:
    await self._expire_old_insights(deal.deal_id)
    insights = self._save_insights_from_agent(
        deal, stakeholders, batch_result.insights
    )
    for insight in insights:
        self.session.add(insight)
```

---

### Phase 5: Verify and Test

1. Verify syntax of all modified files
2. Test agent output schema
3. Verify insights are saved correctly
4. Check API returns correct data

---

## Key Files Summary

| File | Changes |
|------|---------|
| `src/agents/workflows/stakeholder_analysis/models.py` | Add `AgentInsight`, update `DealStakeholdersAnalysisData`, remove concerns/opportunities |
| `src/agents/workflows/stakeholder_analysis/prompts.py` | Add structured insight generation instructions |
| `src/workers/deal_worker/services/stakeholder_analysis_service.py` | Replace `_generate_insights_from_analysis()` with `_save_insights_from_agent()` |

---

## Validation Checklist

- [ ] Agent model has `AgentInsight` schema
- [ ] Agent model has `insights: list[AgentInsight]` in batch result
- [ ] Prompt instructs agent on insight types and priorities
- [ ] Service does NOT hardcode type, priority, or any insight field
- [ ] Service only adds: insight_id, organisation_id, deal_id, status, timestamps, actions
- [ ] All intelligence comes from agent output

---

### History Tracking

- `stakeholder_engagement_history` column tracks changes over time
- Snapshots saved only when values change (not on every run)
- Agent has access to historical data via tools if needed

---

## Execution Flow Map

### Complete Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           TRIGGER LAYER                                     │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  1. Celery Beat (Scheduled)                                                 │
│     └─► trigger_stakeholder_analysis_all_orgs()                             │
│         File: src/workers/deal_worker/tasks/stakeholder_analysis_tasks.py   │
│                                                                             │
│  2. Manual/On-Demand                                                        │
│     └─► analyze_deal_stakeholders_task(org_id, deal_id)                     │
│         File: src/workers/deal_worker/tasks/stakeholder_analysis_tasks.py   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           SERVICE LAYER                                     │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  StakeholderAnalysisService.analyze_all_stakeholders_for_deal()             │
│  File: src/workers/deal_worker/services/stakeholder_analysis_service.py     │
│                                                                             │
│  Steps:                                                                     │
│    1. Get stakeholders for deal                                             │
│    2. Get meetings with transcripts                                         │
│    3. Build batch analysis context                                          │
│    4. ──► Call LLM Agent ◄──                                                │
│    5. Save analysis snapshot to history (if changed)                        │
│    6. Update stakeholder records                                            │
│    7. Expire old insights                                                   │
│    8. ──► Generate new insights from agent observations ◄──                 │
│    9. Save insights to DB                                                   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    AGENT + INSIGHT GENERATION                               │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  StakeholderAnalysisAgent.generate(context)                                 │
│                                                                             │
│  Returns per stakeholder:                                                   │
│    - stance, decision_power, temperature                                    │
│    - champion_potential, motivation, pain_points                            │
│    - concerns → mapped to STAKEHOLDER_RISK insights                         │
│    - opportunities → mapped to CHAMPION_OPPORTUNITY insights                │
│                                                                             │
│  _generate_insights_from_analysis() - Simple mapping:                       │
│    - Loop through stakeholders                                              │
│    - Convert concerns[] to DealStakeholderInsight records                   │
│    - Convert opportunities[] to DealStakeholderInsight records              │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           STORAGE LAYER                                     │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  DealStakeholderInsight records saved to: deal_stakeholder_insights table   │
│  Model: src/domain/models/.../stakeholder_insight_models.py                 │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                                    │
                                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           API LAYER (Pull)                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  GET /deals/{deal_id}/stakeholder-insights                                  │
│  Handler: src/api/routers/crm/stakeholder_insights_handler.py               │
│  Service: src/service/crm/stakeholder_insight_service.py                    │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Line-by-Line Flow

| Step | Action | File & Line |
|------|--------|-------------|
| 1 | **TRIGGER**: Celery task runs (scheduled or on-demand) | `stakeholder_analysis_tasks.py:50` or `:95` |
| 2 | **SERVICE**: StakeholderAnalysisService instantiated | `stakeholder_analysis_service.py:77` |
| 3 | **MAIN METHOD**: `analyze_all_stakeholders_for_deal()` called | `stakeholder_analysis_service.py:345` |
| 4 | **GET PREVIOUS**: Fetch previous snapshots for comparison | `stakeholder_analysis_service.py:370-372` |
| 5 | **GET MEETINGS**: Fetch meetings with transcripts | `stakeholder_analysis_service.py:375-400` |
| 6 | **BUILD CONTEXT**: Create prompt context for LLM | `stakeholder_analysis_service.py:403` |
| 7 | **LLM CALL**: `StakeholderAnalysisAgent.generate()` | `agents/workflows/stakeholder_analysis/agent.py` |
| 8 | **SAVE SNAPSHOT**: Save current state to engagement history | `stakeholder_analysis_service.py:426` |
| 9 | **UPDATE STAKEHOLDER**: Apply new analysis to stakeholder | `stakeholder_analysis_service.py:428` |
| 10 | **EXPIRE OLD**: Mark old insights as EXPIRED | `stakeholder_analysis_service.py:440` |
| 11 | **GENERATE INSIGHTS**: Run all detection methods | `stakeholder_analysis_service.py:441-442` |
| 12 | **SAVE INSIGHTS**: Add insights to session | `stakeholder_analysis_service.py:444-445` |
| 13 | **API PULL**: Handler retrieves for frontend display | `stakeholder_insights_handler.py` |

### Key Files Reference

| Layer | File | Purpose |
|-------|------|---------|
| **Trigger** | `src/workers/deal_worker/tasks/stakeholder_analysis_tasks.py` | Celery task entry points |
| **Trigger** | `src/workers/deal_worker/services/deal_job_handlers.py` | Deal sync job handler |
| **Service** | `src/workers/deal_worker/services/stakeholder_analysis_service.py` | Main orchestration + insight generation |
| **Agent** | `src/agents/workflows/stakeholder_analysis/agent.py` | LLM agent wrapper |
| **Agent** | `src/agents/workflows/stakeholder_analysis/models.py` | Structured output schema |
| **Agent** | `src/agents/workflows/stakeholder_analysis/prompts.py` | System prompt |
| **Model** | `src/domain/models/.../stakeholder_insight_models.py` | DealStakeholderInsight model |
| **API** | `src/api/routers/crm/stakeholder_insights_handler.py` | REST endpoints |
| **API** | `src/service/crm/stakeholder_insight_service.py` | API service layer |

### Important Notes

1. **Insights are generated during stakeholder analysis** - Not a separate process
2. **No additional LLM calls** - Agent observations extracted in same call as stakeholder attributes
3. **Handler only pulls** - API handler retrieves pre-generated insights from database
4. **Triggered by Celery** - Either scheduled (Celery Beat) or on-demand via task
