# Task: Stakeholder AI Insights Generation

## Status: TODO

## Problem Statement

The Stakeholders page (see `docs/Task List/image.png`) shows an "AI Insights" panel on the right side with actionable recommendations like:

1. **"The decision maker hasn't been engaged in 3 weeks"** - Engagement gap alert
2. **"Champion sentiment declining"** - Sentiment trend warning
3. **"Potential secondary champion"** - Opportunity identification
4. **"Security concerns unaddressed"** - Unaddressed risk/blocker alert

Currently, these AI Insights **do not exist** in the system. The existing stakeholder analysis (`StakeholderAnalysisAgent`) generates per-stakeholder attributes (stance, decision_power, champion_potential), but NOT deal-level insights/recommendations.

### Current State vs Required State

| What Exists | What's Missing |
|-------------|----------------|
| Per-stakeholder analysis (stance, decision_power, etc.) | Deal-level insights/recommendations |
| Risk/Blocker extraction from conversations | Insights linking risks to stakeholders |
| Engagement tracking (last_contact_at, engagement_score) | Engagement gap detection and alerts |
| Champion potential score | Champion opportunity identification |

## Proposed Solution

### 1. Create Stakeholder Insight Data Model

Create a new model to store deal-level stakeholder insights:

**File:** `src/domain/models/organisation_tables/sync/stakeholder_insight_models.py`

```python
class StakeholderInsightType(str, Enum):
    """Types of stakeholder insights."""
    ENGAGEMENT_GAP = "ENGAGEMENT_GAP"           # Key person not contacted recently
    SENTIMENT_DECLINING = "SENTIMENT_DECLINING" # Champion/supporter sentiment dropping
    CHAMPION_OPPORTUNITY = "CHAMPION_OPPORTUNITY"  # Potential new champion identified
    UNADDRESSED_CONCERN = "UNADDRESSED_CONCERN"    # Risk/blocker not followed up
    STAKEHOLDER_RISK = "STAKEHOLDER_RISK"          # Champion leaving, role change, etc.
    MISSING_DECISION_MAKER = "MISSING_DECISION_MAKER"  # No decision maker identified
    COVERAGE_GAP = "COVERAGE_GAP"                  # Key department not covered

class StakeholderInsightPriority(str, Enum):
    """Priority level of the insight."""
    CRITICAL = "CRITICAL"   # Red - immediate action needed
    HIGH = "HIGH"           # Orange - attention required
    MEDIUM = "MEDIUM"       # Yellow - worth reviewing
    LOW = "LOW"             # Blue - informational

class StakeholderInsightStatus(str, Enum):
    """Lifecycle status of the insight."""
    ACTIVE = "ACTIVE"           # Currently relevant
    ACKNOWLEDGED = "ACKNOWLEDGED"  # User saw it
    RESOLVED = "RESOLVED"       # Issue addressed
    DISMISSED = "DISMISSED"     # User dismissed as not relevant
    EXPIRED = "EXPIRED"         # No longer relevant (stale)

class DealStakeholderInsight(SQLModel, table=True):
    """AI-generated insights about deal stakeholders."""

    __tablename__ = "deal_stakeholder_insights"

    # Identity
    insight_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(index=True)
    deal_id: UUID = Field(foreign_key="crm_deals.deal_id", index=True)

    # Optional link to specific stakeholder (some insights are deal-level)
    stakeholder_id: UUID | None = Field(
        default=None,
        foreign_key="deal_stakeholders.stakeholder_id"
    )

    # Classification
    insight_type: StakeholderInsightType
    priority: StakeholderInsightPriority
    status: StakeholderInsightStatus = Field(default=StakeholderInsightStatus.ACTIVE)

    # Content
    title: str                    # "The decision maker hasn't been engaged in 3 weeks"
    description: str              # "Arun Patel (VP Technology) hasn't been contacted..."
    suggested_action: str | None  # "Schedule Call", "View Details", etc.
    action_url: str | None        # Deep link for the action

    # Context (for insight generation)
    stakeholder_name: str | None      # Name of related stakeholder
    stakeholder_role: str | None      # Job title
    stakeholder_email: str | None     # For linking

    # Metrics (evidence for the insight)
    metric_value: float | None        # e.g., 21 (days since contact)
    metric_unit: str | None           # e.g., "days", "percent"
    threshold_value: float | None     # e.g., 14 (days threshold)
    trend_direction: str | None       # "declining", "improving", "stable"

    # Source attribution
    source_type: str | None           # "engagement_analysis", "risk_blocker", etc.
    source_record_id: str | None      # Link to source (risk_blocker_id, etc.)

    # AI metadata
    ai_confidence: float | None       # 0.0 - 1.0
    ai_reasoning: str | None          # Why this insight was generated

    # Timestamps
    identified_at: datetime           # When the condition was detected
    expires_at: datetime | None       # When insight becomes stale
    created_at: datetime
    updated_at: datetime
```

### 2. Create Stakeholder Insights Agent

Create a new agent workflow that generates these insights:

**Location:** `src/agents/workflows/stakeholder_insights/`

```
stakeholder_insights/
├── __init__.py
├── agent.py           # StakeholderInsightsAgent
├── models.py          # Input/Output models
├── prompts.py         # System prompts
└── insight_rules.py   # Rule-based insight detection
```

#### Agent Responsibilities

The agent should analyze:

1. **Engagement Gaps**
   - Input: All stakeholders with `decision_power` and `last_contact_at`
   - Rule: If DECISION_MAKER and last_contact > 14 days, generate insight
   - Output: ENGAGEMENT_GAP insight

2. **Sentiment Trends**
   - Input: Historical engagement_score changes (requires tracking history)
   - Rule: If engagement_score dropped > 20% since last analysis
   - Output: SENTIMENT_DECLINING insight

3. **Champion Opportunities**
   - Input: Stakeholders with high engagement but not marked as CHAMPION
   - Rule: If champion_potential > 70% and stance != CHAMPION
   - Output: CHAMPION_OPPORTUNITY insight

4. **Unaddressed Concerns**
   - Input: DealRiskBlocker records with status=OPEN and category=SECURITY/etc.
   - Rule: If risk open > 7 days with no follow-up documented
   - Output: UNADDRESSED_CONCERN insight

5. **Coverage Gaps**
   - Input: All stakeholders by department
   - Rule: If key departments (IT, Finance, Legal) have no stakeholder
   - Output: COVERAGE_GAP insight

### 3. Create Tool to Fetch Existing Insights

**File:** `src/agents/tools/crm/get_stakeholder_insights.py`

```python
async def get_deal_stakeholder_insights(
    deal_id: str,
    context: AgentContext,
    include_resolved: bool = False,
    limit: int = 10,
) -> list[dict]:
    """
    Fetch existing stakeholder insights for a deal.

    Used by:
    - AI agents for context when generating new insights (deduplication)
    - API endpoint for serving insights to frontend

    Returns:
        List of insight dictionaries with:
        - insight_id, insight_type, priority, status
        - title, description, suggested_action
        - stakeholder_name, stakeholder_role
        - metric_value, metric_unit
        - identified_at, expires_at
    """
```

**File:** `src/agents/tools/crm/list_stakeholder_insights.py`

```python
async def list_stakeholder_insights_by_type(
    deal_id: str,
    context: AgentContext,
    insight_types: list[str] | None = None,
) -> list[dict]:
    """
    List insights filtered by type for targeted analysis.

    Useful for checking if specific insight types already exist
    before generating duplicates.
    """
```

### 4. Create Insights Generation Service

**File:** `src/workers/deal_worker/services/stakeholder_insights_service.py`

```python
class StakeholderInsightsService:
    """Service for generating and managing stakeholder insights."""

    async def generate_insights_for_deal(
        self,
        deal_id: UUID
    ) -> list[DealStakeholderInsight]:
        """
        Generate all insights for a deal.

        Steps:
        1. Load all stakeholders for deal
        2. Load existing risks/blockers
        3. Load existing active insights (for deduplication)
        4. Run rule-based detection for each insight type
        5. Run AI agent for complex pattern detection
        6. Create new insight records
        7. Expire stale insights
        """

    async def detect_engagement_gaps(
        self,
        stakeholders: list[DealStakeholder],
        threshold_days: int = 14,
    ) -> list[DealStakeholderInsight]:
        """Detect decision makers not contacted recently."""

    async def detect_sentiment_changes(
        self,
        stakeholders: list[DealStakeholder],
        threshold_percent: float = 20.0,
    ) -> list[DealStakeholderInsight]:
        """Detect declining engagement in champions."""

    async def detect_champion_opportunities(
        self,
        stakeholders: list[DealStakeholder],
        potential_threshold: float = 70.0,
    ) -> list[DealStakeholderInsight]:
        """Identify potential new champions."""

    async def detect_unaddressed_concerns(
        self,
        deal_id: UUID,
        staleness_days: int = 7,
    ) -> list[DealStakeholderInsight]:
        """Find risks/blockers without follow-up."""
```

### 5. Create API Endpoint

**File:** `src/api/routers/crm/stakeholder_insights_handler.py`

```python
@router.get("/deals/{deal_id}/stakeholder-insights")
async def get_deal_stakeholder_insights(
    deal_id: UUID,
    status: StakeholderInsightStatus | None = None,
    priority: StakeholderInsightPriority | None = None,
    limit: int = 20,
) -> list[StakeholderInsightResponse]:
    """
    Get AI-generated stakeholder insights for a deal.

    Response format matches UI card structure:
    {
        "insight_id": "...",
        "insight_type": "ENGAGEMENT_GAP",
        "priority": "HIGH",
        "title": "The decision maker hasn't been engaged in 3 weeks",
        "description": "Arun Patel (VP Technology) hasn't been contacted...",
        "suggested_action": "Schedule Call",
        "stakeholder_name": "Arun Patel",
        "stakeholder_role": "VP Technology",
        "metric_value": 21,
        "metric_unit": "days",
        "identified_at": "2025-01-01T00:00:00Z"
    }
    """

@router.post("/deals/{deal_id}/stakeholder-insights/{insight_id}/acknowledge")
async def acknowledge_insight(deal_id: UUID, insight_id: UUID):
    """Mark an insight as acknowledged."""

@router.post("/deals/{deal_id}/stakeholder-insights/{insight_id}/dismiss")
async def dismiss_insight(deal_id: UUID, insight_id: UUID):
    """Dismiss an insight as not relevant."""

@router.post("/deals/{deal_id}/stakeholder-insights/{insight_id}/resolve")
async def resolve_insight(deal_id: UUID, insight_id: UUID):
    """Mark an insight as resolved."""
```

### 6. Create Celery Tasks

**File:** `src/workers/deal_worker/tasks/stakeholder_insights_tasks.py`

```python
@app.task(name="stakeholder.generate_insights_task")
def generate_stakeholder_insights_task(
    organisation_id: str,
    deal_id: str,
) -> dict:
    """Generate stakeholder insights for a deal."""

@app.task(name="stakeholder.generate_insights_all_deals_task")
def generate_insights_all_deals_task(
    organisation_id: str,
) -> dict:
    """Generate insights for all active deals in an organisation."""

@app.task(name="stakeholder.expire_stale_insights_task")
def expire_stale_insights_task() -> dict:
    """Periodic task to expire old insights."""
```

### 7. Add Engagement History Tracking (For Sentiment Trends)

To detect "sentiment declining", we need historical engagement data:

**Option A:** Add history table

```python
class StakeholderEngagementHistory(SQLModel, table=True):
    """Track engagement score changes over time."""

    history_id: UUID
    stakeholder_id: UUID
    engagement_score: float
    temperature: StakeholderTemperature
    recorded_at: datetime
```

**Option B:** Store in stakeholder_data JSONB

```python
# In DealStakeholder.stakeholder_data:
{
    "engagement_history": [
        {"score": 5.0, "temperature": "HOT", "recorded_at": "2025-01-01"},
        {"score": 3.5, "temperature": "WARM", "recorded_at": "2025-01-15"},
    ]
}
```

## Implementation Steps

### Phase 1: Data Model & Basic Infrastructure

1. Create `stakeholder_insight_models.py` with all enums and model
2. Create database migration for `deal_stakeholder_insights` table
3. Add exports to `__init__.py` files

### Phase 2: Tool for Fetching Insights

1. Create `get_stakeholder_insights.py` tool
2. Create `list_stakeholder_insights.py` tool
3. Register tools in agent tools registry

### Phase 3: Insight Generation Service

1. Create `stakeholder_insights_service.py`
2. Implement rule-based detection methods
3. Integrate with existing stakeholder data

### Phase 4: Agent Integration

1. Create `stakeholder_insights/` workflow directory
2. Implement `StakeholderInsightsAgent`
3. Add AI-powered pattern detection

### Phase 5: API & Tasks

1. Create `stakeholder_insights_handler.py` router
2. Create Celery tasks
3. Add to task orchestration (trigger after stakeholder analysis)

### Phase 6: History Tracking

1. Choose approach (new table vs JSONB)
2. Update stakeholder sync to record history
3. Enable sentiment trend detection

## Files to Create

| File | Purpose |
|------|---------|
| `src/domain/models/organisation_tables/sync/stakeholder_insight_models.py` | Data model for insights |
| `src/agents/tools/crm/get_stakeholder_insights.py` | Tool to fetch insights |
| `src/agents/tools/crm/list_stakeholder_insights.py` | Tool to list/filter insights |
| `src/agents/workflows/stakeholder_insights/agent.py` | AI agent for insight generation |
| `src/agents/workflows/stakeholder_insights/models.py` | Agent input/output models |
| `src/agents/workflows/stakeholder_insights/prompts.py` | Agent prompts |
| `src/workers/deal_worker/services/stakeholder_insights_service.py` | Business logic service |
| `src/workers/deal_worker/tasks/stakeholder_insights_tasks.py` | Celery tasks |
| `src/api/routers/crm/stakeholder_insights_handler.py` | REST API endpoints |
| `src/api/schemas/stakeholder_insight_models.py` | API response schemas |

## Files to Modify

| File | Changes |
|------|---------|
| `src/domain/models/organisation_tables/sync/__init__.py` | Export new models |
| `src/workers/deal_worker/services/stakeholder_service.py` | Record engagement history |
| `src/workers/deal_worker/tasks/stakeholder_analysis_tasks.py` | Trigger insight generation after analysis |
| `src/api/routers/crm/__init__.py` | Register new router |
| `src/agents/tools/__init__.py` | Register new tools |

## Reference Files (For Patterns)

| File | Why Reference It |
|------|------------------|
| `src/domain/models/organisation_tables/sync/risk_blocker_models.py` | Similar model structure with status lifecycle |
| `src/workers/deal_worker/services/risk_blocker_extraction_service.py` | Similar service pattern |
| `src/agents/workflows/stakeholder_analysis/agent.py` | Existing stakeholder agent to extend |
| `src/agents/tools/crm/list_stakeholders.py` | Tool pattern to follow |

## Insight Types & Detection Rules

| Insight Type | Priority | Detection Rule |
|--------------|----------|----------------|
| ENGAGEMENT_GAP | HIGH/CRITICAL | decision_power=DECISION_MAKER AND days_since_contact > 14 |
| SENTIMENT_DECLINING | HIGH | engagement_score dropped > 20% AND stance IN (CHAMPION, SUPPORTER) |
| CHAMPION_OPPORTUNITY | MEDIUM | champion_potential > 70% AND stance NOT IN (CHAMPION) |
| UNADDRESSED_CONCERN | HIGH | DealRiskBlocker.status=OPEN AND days_since_created > 7 |
| STAKEHOLDER_RISK | CRITICAL | Champion marked as leaving OR role change detected |
| MISSING_DECISION_MAKER | MEDIUM | No stakeholder with decision_power=DECISION_MAKER |
| COVERAGE_GAP | LOW | Key department (IT/Finance/Legal) has no stakeholder |

## Success Criteria

- [ ] Insights appear in API response matching UI card format
- [ ] Each insight type is generated based on defined rules
- [ ] Duplicate insights are not created
- [ ] Old insights expire automatically
- [ ] Users can acknowledge/dismiss/resolve insights
- [ ] Tool available for AI agents to fetch existing insights
- [ ] Insights regenerate when stakeholder data changes

## Testing Considerations

1. Test each insight type detection rule individually
2. Test deduplication (same insight not created twice)
3. Test expiration of stale insights
4. Test status transitions (ACTIVE -> ACKNOWLEDGED -> RESOLVED)
5. Test API filtering by status/priority
6. Test with deals that have no stakeholders (edge case)

---

Once task is completed, move to review folder.
