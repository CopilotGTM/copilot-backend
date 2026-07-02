# Proposed Agent Tools

This document outlines new tools to be implemented for chat agents. Tools are categorized by scope and purpose.

## Current Tool Status

### Implemented Tools

| Tool Name | Scope | Description |
|-----------|-------|-------------|
| `LIST_DEAL_MEETINGS` | Deal | List meetings associated with a deal |
| `GET_MEETING_DETAILS` | Org | Get meeting details including **summary**, prep, followup |
| `GET_MEETING_TRANSCRIPT` | Org | Get meeting transcript text |
| `GET_MEETING_DEAL_CONTEXT` | Org | Get deal context for a meeting |
| `LIST_DEAL_STAKEHOLDERS` | Deal | List stakeholders for a deal, optionally filtered to one person |
| `LIST_DEAL_EMAILS` | Deal | List emails associated with a deal |
| `GET_EMAIL_DETAILS` | Org | Get full email content |
| `LIST_DEAL_NOTES` | Deal | List CRM notes for a deal |
| `GET_EXISTING_RISKS_BLOCKERS` | Deal | Get risks/blockers for a deal |
| `SEARCH_INTERNAL_KNOWLEDGE` | Org | Search knowledge base |
| `SEARCH_INTERNET` | Global | Web search |

### Note: Meeting Summary Already Available

The `GET_MEETING_DETAILS` tool already includes meeting summary via `include_summary=True` (default). It returns:
- `summary.status`: Generation status (PENDING, IN_PROGRESS, COMPLETED, FAILED)
- `summary.data`: The full meeting summary JSON when completed

**No separate `GET_MEETING_SUMMARY` tool is needed.**

---

## Proposed New Tools

### Category 1: Organization-Level Deal Tools (ORG-SCOPED)

These tools query across all deals in an organization. Essential for chat agents to help users find and explore deals.

#### 1.1 `SEARCH_DEALS` (High Priority)

**Purpose**: Quick lookup to find deals by name. Returns minimal data (deal_id, name, company_name) for fast searching. Use this first when user mentions a deal by name, then use `GET_DEAL_DETAILS` for full info.

**Parameters**:
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | `str` | Required | Search term - matches deal name or company name (case-insensitive, partial match) |
| `limit` | `int` | `10` | Max results (1-20) |

**Returns**:
```python
[
    {
        "deal_id": "uuid-1234-5678",
        "name": "Enterprise Deal - Acme Corp",
        "company_name": "Acme Corporation"
    },
    {
        "deal_id": "uuid-2345-6789",
        "name": "Acme Expansion Q2",
        "company_name": "Acme Corporation"
    },
    ...
]
```

**Example Use Cases**:
- "Get me the Acme deal" → Search for "Acme", return matching deals
- "Show me the Microsoft enterprise deal" → Search for "Microsoft enterprise"
- "Find the deal with TechCorp" → Search for "TechCorp"

**Usage Pattern**:
```
User: "What's the status of the Acme deal?"

Agent:
1. Call SEARCH_DEALS(query="Acme") → Get deal_id
2. Call GET_DEAL_DETAILS(deal_id=...) → Get full details
3. Respond with deal status
```

---

#### 1.2 `LIST_DEALS` (High Priority)

**Purpose**: List deals with flexible filtering and sorting. Use for browsing/filtering deals.

**Parameters**:
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str \| None` | `None` | Filter by deal name (partial match, case-insensitive) |
| `status` | `str \| None` | `None` | Filter: "open", "closed", "won", "lost" |
| `stage_name` | `str \| None` | `None` | Filter by stage name |
| `min_amount` | `float \| None` | `None` | Filter: deal amount >= value |
| `max_amount` | `float \| None` | `None` | Filter: deal amount <= value |
| `owner_email` | `str \| None` | `None` | Filter by deal owner |
| `company_name` | `str \| None` | `None` | Filter by company (partial match) |
| `created_after` | `datetime \| None` | `None` | Filter: created after date |
| `created_before` | `datetime \| None` | `None` | Filter: created before date |
| `close_date_after` | `datetime \| None` | `None` | Filter: expected close after |
| `close_date_before` | `datetime \| None` | `None` | Filter: expected close before |
| `sort_by` | `str` | `"updated_at"` | Sort field: "amount", "created_at", "close_date", "updated_at" |
| `sort_order` | `str` | `"desc"` | "asc" or "desc" |
| `limit` | `int` | `20` | Max results (1-100) |
| `offset` | `int` | `0` | Pagination offset |

**Returns**:
```python
[
    {
        "deal_id": "uuid",
        "name": "Enterprise Deal - Acme Corp",
        "company_name": "Acme Corporation",
        "amount": 150000.00,
        "currency": "USD",
        "stage_name": "Negotiation",
        "stage_label": "Contract Review",
        "is_closed": False,
        "is_won": None,
        "probability": 60,
        "owner_name": "John Smith",
        "owner_email": "john@company.com",
        "expected_close_date": "2024-03-15",
        "created_at": "2024-01-10T...",
        "updated_at": "2024-02-01T...",
        "stakeholder_count": 5,
        "meeting_count": 12,
        "last_activity_at": "2024-02-01T..."
    },
    ...
]
```

**Example Use Cases**:
- "Show me all open deals worth more than $100K"
- "List deals closing this quarter"
- "What deals does Sarah own?"
- "Show me deals with Acme"
- "Find deals named 'Enterprise'" → Use `name` filter

---

#### 1.4 `GET_DEAL_DETAILS` (High Priority)

**Purpose**: Get comprehensive details for a single deal. Use after `SEARCH_DEALS` to get full information.

**Parameters**:
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `deal_id` | `str` | Required | Deal UUID |
| `include_timeline` | `bool` | `False` | Include recent activity timeline |
| `include_stakeholders` | `bool` | `True` | Include stakeholder summary |
| `include_risks` | `bool` | `True` | Include active risks/blockers |

**Returns**:
```python
{
    "deal_id": "uuid",
    "name": "Enterprise Deal - Acme Corp",
    "description": "Multi-year enterprise agreement...",
    "company_name": "Acme Corporation",
    "amount": 150000.00,
    "currency": "USD",
    "stage": {
        "name": "Negotiation",
        "label": "Contract Review",
        "probability": 60,
        "is_closed": False
    },
    "owner": {
        "name": "John Smith",
        "email": "john@company.com"
    },
    "dates": {
        "created_at": "2024-01-10T...",
        "updated_at": "2024-02-01T...",
        "expected_close_date": "2024-03-15",
        "last_activity_at": "2024-02-01T..."
    },
    "metrics": {
        "stakeholder_count": 5,
        "meeting_count": 12,
        "email_count": 45,
        "note_count": 8
    },
    "stakeholders": [...],  # if include_stakeholders
    "risks": [...],         # if include_risks
    "timeline": [...]       # if include_timeline
}
```

---

#### 1.5 `GET_DEAL_PIPELINE_SUMMARY` (Medium Priority)

**Purpose**: Get aggregated pipeline metrics for dashboard/overview queries.

**Parameters**:
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `group_by` | `str` | `"stage"` | Group by: "stage", "owner", "month" |
| `include_closed` | `bool` | `False` | Include closed deals in metrics |

**Returns**:
```python
{
    "total_deals": 45,
    "total_value": 2500000.00,
    "currency": "USD",
    "by_stage": [
        {
            "stage_name": "Discovery",
            "deal_count": 12,
            "total_value": 450000.00,
            "avg_value": 37500.00
        },
        ...
    ],
    "by_owner": [...],  # if group_by="owner"
    "summary": {
        "avg_deal_size": 55555.55,
        "median_deal_size": 45000.00,
        "deals_closing_this_month": 5,
        "value_closing_this_month": 350000.00
    }
}
```

**Example Use Cases**:
- "What's our current pipeline value?"
- "How many deals are in each stage?"
- "What's closing this month?"

---

### Category 2: Meeting Intelligence Tools (ORG-SCOPED)

#### 2.1 `GET_MEETING_SUMMARY` (Low Priority - Already Available)

**Status**: **NOT NEEDED** - Already available via `GET_MEETING_DETAILS` with `include_summary=True`.

The existing tool returns:
```python
{
    "event_id": "...",
    "title": "...",
    "summary": {
        "status": "COMPLETED",
        "data": {
            "executive_summary": "...",
            "key_topics": [...],
            "action_items": [...],
            "decisions": [...],
            ...
        }
    }
}
```

---

#### 2.2 `LIST_MEETINGS_BY_DATE` (Medium Priority)

**Purpose**: List meetings across all deals within a date range. Useful for "what meetings happened this week" queries.

**Parameters**:
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `start_date` | `date` | Required | Start of date range |
| `end_date` | `date` | Required | End of date range |
| `has_transcript` | `bool \| None` | `None` | Filter by transcript availability |
| `has_summary` | `bool \| None` | `None` | Filter by summary availability |
| `include_internal` | `bool` | `False` | Include internal meetings (no external attendees) |
| `limit` | `int` | `50` | Max results |

**Returns**:
```python
[
    {
        "event_id": "uuid",
        "title": "Quarterly Review - Acme",
        "start_at": "2024-02-01T10:00:00Z",
        "end_at": "2024-02-01T11:00:00Z",
        "deal": {
            "deal_id": "uuid",
            "name": "Acme Enterprise Deal",
            "company_name": "Acme Corp"
        },
        "attendee_count": 5,
        "has_transcript": True,
        "has_summary": True,
        "organiser_name": "John Smith"
    },
    ...
]
```

**Example Use Cases**:
- "What customer meetings happened this week?"
- "Show me meetings with transcripts from last month"
- "List all meetings that have summaries"

---

### Category 3: Calendar Tools (ORG-SCOPED)

#### 3.1 `GET_UPCOMING_MEETINGS` (High Priority)

**Purpose**: List upcoming meetings for preparation and awareness.

**Parameters**:
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `days_ahead` | `int` | `7` | Look ahead N days |
| `deal_id` | `str \| None` | `None` | Filter to specific deal |
| `include_internal` | `bool` | `False` | Include internal meetings |
| `has_prep` | `bool \| None` | `None` | Filter by prep availability |
| `limit` | `int` | `20` | Max results |

**Returns**:
```python
[
    {
        "event_id": "uuid",
        "title": "Demo Call - Acme",
        "start_at": "2024-02-05T14:00:00Z",
        "duration_minutes": 60,
        "deal": {
            "deal_id": "uuid",
            "name": "Acme Enterprise",
            "company_name": "Acme Corp",
            "amount": 150000.00
        },
        "attendees": [
            {"name": "Jane Doe", "email": "jane@acme.com", "is_external": True},
            ...
        ],
        "has_prep": True,
        "prep_summary": "Key discussion points: pricing, timeline..."
    },
    ...
]
```

**Example Use Cases**:
- "What meetings do I have this week?"
- "Show me upcoming calls with prep available"
- "What customer meetings are scheduled for tomorrow?"

---

### Category 4: Activity & Timeline Tools

#### 4.1 `GET_RECENT_ACTIVITIES` (Medium Priority)

**Purpose**: Get recent activities across all deals for an overview.

**Parameters**:
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `activity_types` | `list[str] \| None` | `None` | Filter: "meeting", "email", "note", "stage_change" |
| `days_back` | `int` | `7` | Look back N days |
| `deal_id` | `str \| None` | `None` | Filter to specific deal |
| `limit` | `int` | `50` | Max results |

**Returns**:
```python
[
    {
        "activity_type": "meeting",
        "timestamp": "2024-02-01T10:00:00Z",
        "title": "Discovery Call completed",
        "deal": {"deal_id": "...", "name": "Acme Deal"},
        "summary": "Discussed pricing and timeline...",
        "actor": "John Smith"
    },
    {
        "activity_type": "stage_change",
        "timestamp": "2024-02-01T15:30:00Z",
        "title": "Stage changed: Discovery → Proposal",
        "deal": {"deal_id": "...", "name": "Acme Deal"},
        "actor": "System"
    },
    ...
]
```

---

### Category 5: Contact/Stakeholder Tools (ORG-SCOPED)

#### 5.1 `SEARCH_CONTACTS` (Medium Priority)

**Purpose**: Search for contacts across all deals.

**Parameters**:
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | `str` | Required | Search by name, email, company |
| `company_name` | `str \| None` | `None` | Filter by company |
| `limit` | `int` | `20` | Max results |

**Returns**:
```python
[
    {
        "contact_id": "uuid",
        "email": "jane@acme.com",
        "name": "Jane Doe",
        "job_title": "VP of Engineering",
        "company_name": "Acme Corp",
        "deals": [
            {"deal_id": "...", "name": "Acme Enterprise", "role": "Decision Maker"}
        ],
        "last_contact_at": "2024-02-01T..."
    },
    ...
]
```

---

## Implementation Priority

### Phase 1 (High Priority)
1. **`SEARCH_DEALS`** - Quick deal lookup by name (most common user query)
2. `LIST_DEALS` - Deal discovery with filters
3. `GET_DEAL_DETAILS` - Deal deep-dive
4. `GET_UPCOMING_MEETINGS` - Meeting preparation

### Phase 2 (Medium Priority)
5. `GET_DEAL_PIPELINE_SUMMARY` - Dashboard queries
6. `LIST_MEETINGS_BY_DATE` - Historical review
7. `SEARCH_CONTACTS` - Contact discovery
8. `GET_RECENT_ACTIVITIES` - Activity overview

### Phase 3 (Lower Priority)
- Additional filters and aggregations
- Export capabilities
- Comparison tools

---

## Recommended Tool Usage Flow

```
User: "Tell me about the Acme deal"

┌─────────────────────────────────────────────────────────────┐
│ Step 1: SEARCH_DEALS(query="Acme")                          │
│         → Returns: [{deal_id: "abc-123", name: "Acme..."}]  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ Step 2: GET_DEAL_DETAILS(deal_id="abc-123")                 │
│         → Returns: Full deal info with stakeholders, risks  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ Step 3: Respond to user with deal information               │
└─────────────────────────────────────────────────────────────┘
```

This two-step approach is efficient because:
- `SEARCH_DEALS` is fast (minimal data, indexed search)
- `GET_DEAL_DETAILS` is called only when deal_id is known
- Avoids loading full details for multiple deals when only one is needed

---

## Security Pattern

All new tools MUST follow the established security pattern:

```python
async def list_deals(
    *,
    context: AgentContext,  # Contains immutable organisation_id
    # other parameters...
) -> list[dict[str, Any]]:
    """
    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Prevents cross-org data access
    """
    # SECURITY: organisation_id comes from context, not parameters
    organisation_id = context.organisation_id

    if not organisation_id:
        logger.warning("list_deals: missing organisation_id in context")
        return []

    # Acquire fresh session for database operations
    async with context.acquire_session() as session:
        # Query with organisation_id filter
        stmt = select(CRMDeal).where(
            c.organisation_id == organisation_id,
            # other filters...
        )
        # ...
```

**Key Points**:
- `organisation_id` ALWAYS from context (immutable)
- `deal_id` from context for deal-scoped tools, from parameter for org-scoped tools
- Use `context.acquire_session()` for database access
- Never use `db_session_lock()` - tools acquire their own sessions
