# Deal Risks & Blockers Feature - Design Document

## Overview

This document outlines the design for a **Deal Risks & Blockers** feature that provides AI-powered identification and tracking of risks and blockers associated with deals. The feature enables pre-sales teams to proactively identify potential issues that could stall or lose deals by analysing meeting transcripts, emails, Slack conversations, and other deal activities.

## Problem Statement

Pre-sales teams need visibility into potential deal blockers and risks to:

1. Proactively address customer concerns before they derail deals
2. Track resolution status of identified blockers
3. Understand patterns across deals (common blockers, resolution strategies)
4. Prioritise deals that need immediate attention

### Current State

- **Meeting Intelligence**: Transcripts are captured and stored (`TranscriptSegment`)
- **Email Associations**: Deal-email links exist (`deal_email_associations`)
- **Slack Integration**: Messaging channels and messages exist (`messaging_channels`, `messaging_messages`)
- **Deal Summary**: AI-generated summaries exist (`CRMDeal.deal_summary_data`)
- **Activities Table**: `CRMActivity` table supports generic activity types with `JSONB` metadata

### Gap Analysis

| Capability | Current State | Gap |
|------------|---------------|-----|
| Risk/Blocker Identification | Manual by AEs | Need AI-powered extraction |
| Risk/Blocker Storage | No dedicated structure | Need schema to store extracted items |
| Source Attribution | N/A | Need linking to source (transcript, email, Slack) |
| Status Tracking | N/A | Need workflow states (Open → In Progress → Resolved) |
| Timeline Integration | N/A | Need to appear in deal timeline |

---

## Architecture Decision: Dedicated Table vs Activities Table

### Option A: Extend `CRMActivity` Table

**Approach**: Add new `activity_type` values (`RISK`, `BLOCKER`) to the existing `CRMActivity` model and use the `properties` JSONB field to store risk/blocker-specific metadata.

**Pros**:

- No new tables; reuse existing infrastructure
- Unified activity querying for deals
- Existing sync patterns can be reused

**Cons**:

- `CRMActivity` is designed for CRM-synced data with sync metadata (`synced_at`, `last_seen_at`, `source_modified_at`)
- Risks/blockers are **internally generated**, not synced from external CRMs
- Schema pollution: Many `CRMActivity` columns are irrelevant for risks/blockers (e.g., `owner_external_id`, `attendees`, `call_outcome`)
- Query complexity: Filtering AI-generated items from CRM-synced items

### Option B: Dedicated `DealRiskBlocker` Table (Recommended)

**Approach**: Create a purpose-built table for AI-identified and manually-added risks/blockers with clean, focused schema.

**Pros**:

- Purpose-built schema for risks/blockers domain
- No sync metadata overhead (these are internally generated)
- Clear separation of concerns: CRM sync vs AI intelligence
- Optimised indexes for risk/blocker queries
- Easier to evolve independently

**Cons**:

- New table and migration
- Separate query paths from activities

### Recommendation: **Option B - Dedicated Table**

**Reasoning**:

1. Risks/blockers are **intelligence outputs**, not synced CRM data
2. The `CRMActivity` table has significant schema overhead designed for sync workflows
3. Risks/blockers have unique lifecycle (AI-suggested → User-reviewed → Resolved) not present in CRM activities
4. Timeline integration is already solved via `DealTimelineEvent` (events can reference any source table)
5. Clean separation enables independent evolution of risk intelligence features

---

## Data Model

### DealRiskBlocker Table

```python
class RiskBlockerType(str, Enum):
    """Classification of the item as risk or blocker."""
    
    RISK = "RISK"       # Potential issue that may impact deal
    BLOCKER = "BLOCKER"  # Active impediment that must be resolved


class RiskBlockerStatus(str, Enum):
    """Lifecycle status of the risk/blocker."""
    
    # AI Workflow States
    AI_SUGGESTED = "AI_SUGGESTED"    # Identified by AI, pending review
    AI_DISMISSED = "AI_DISMISSED"    # Dismissed by AI as low-confidence
    
    # User Workflow States
    OPEN = "OPEN"                    # Confirmed as active issue
    IN_PROGRESS = "IN_PROGRESS"      # Being actively addressed
    RESOLVED = "RESOLVED"            # Issue has been resolved
    DISMISSED = "DISMISSED"          # User dismissed as not relevant
    WONT_FIX = "WONT_FIX"            # Acknowledged but not addressing


class RiskBlockerSeverity(str, Enum):
    """Severity/impact level of the risk or blocker."""
    
    LOW = "LOW"           # Minor impact, can proceed
    MEDIUM = "MEDIUM"     # Moderate impact, needs attention
    HIGH = "HIGH"         # Significant impact, priority attention
    CRITICAL = "CRITICAL"  # Deal-breaking, immediate action required


class RiskBlockerCategory(str, Enum):
    """Category/domain of the risk or blocker.
    
    Categories aligned with common pre-sales deal obstacles.
    """
    
    # Technical Categories
    TECHNICAL = "TECHNICAL"           # Technical requirements/compatibility
    INTEGRATION = "INTEGRATION"       # Integration challenges
    SECURITY = "SECURITY"             # Security/compliance concerns
    PERFORMANCE = "PERFORMANCE"       # Performance/scalability concerns
    
    # Commercial Categories
    BUDGET = "BUDGET"                 # Budget/pricing concerns
    TIMELINE = "TIMELINE"             # Timeline/deadline concerns
    CONTRACT = "CONTRACT"             # Contract/legal terms
    
    # Stakeholder Categories
    CHAMPION_RISK = "CHAMPION_RISK"   # Champion leaving/changing roles
    STAKEHOLDER = "STAKEHOLDER"       # Stakeholder alignment issues
    COMPETITION = "COMPETITION"       # Competitive pressure
    
    # Process Categories
    INTERNAL_PROCESS = "INTERNAL_PROCESS"  # Customer internal process
    RESOURCE = "RESOURCE"             # Resource availability
    
    # General
    OTHER = "OTHER"


class RiskBlockerSource(str, Enum):
    """Source where the risk/blocker was identified."""
    
    # AI Extraction Sources
    MEETING_TRANSCRIPT = "MEETING_TRANSCRIPT"
    EMAIL = "EMAIL"
    SLACK = "SLACK"
    
    # Manual Sources
    MANUAL = "MANUAL"                 # Manually added by user
    CRM_NOTE = "CRM_NOTE"             # Imported from CRM notes
    
    # System Sources
    SYSTEM = "SYSTEM"                 # System-detected (e.g., stale deal)


class DealRiskBlocker(SQLModel, table=True):
    """
    AI-identified or manually tracked risks and blockers for deals.
    
    This table stores potential deal obstacles extracted from meeting transcripts,
    emails, Slack conversations, or manually added by users. Each item goes through
    a lifecycle from AI suggestion to resolution.
    
    Key design principles:
    1. Source attribution: Links to original source (transcript, email, etc.)
    2. Lifecycle tracking: Status workflow from suggestion to resolution
    3. AI confidence: Confidence score for AI-extracted items
    4. Timeline integration: Creates DealTimelineEvent for visibility
    """
    
    __tablename__ = "deal_risks_blockers"
    __table_args__ = (
        # Primary query: All items for a deal by status
        Index("ix_risk_blocker_deal_status", "deal_id", "status"),
        # Filter by type within a deal
        Index("ix_risk_blocker_deal_type", "deal_id", "item_type"),
        # Org-wide queries (dashboards, reports)
        Index("ix_risk_blocker_org_status", "organisation_id", "status"),
        Index("ix_risk_blocker_org_severity", "organisation_id", "severity"),
        # Category analysis
        Index("ix_risk_blocker_category", "organisation_id", "category"),
        # Source tracking
        Index("ix_risk_blocker_source_ref", "source_type", "source_record_id"),
    )
    
    # ─────────────────────────────────────────────────────────────────────────
    # Identity
    # ─────────────────────────────────────────────────────────────────────────
    risk_blocker_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Internal UUID for the risk/blocker record.",
    )
    
    # ─────────────────────────────────────────────────────────────────────────
    # Context
    # ─────────────────────────────────────────────────────────────────────────
    organisation_id: UUID = Field(
        index=True,
        description="Organisation this risk/blocker belongs to.",
    )
    deal_id: UUID = Field(
        foreign_key="crm_deals.deal_id",
        index=True,
        description="The deal this risk/blocker is associated with.",
    )
    
    # ─────────────────────────────────────────────────────────────────────────
    # Classification
    # ─────────────────────────────────────────────────────────────────────────
    item_type: RiskBlockerType = Field(
        description="Whether this is a RISK or BLOCKER.",
    )
    category: RiskBlockerCategory = Field(
        default=RiskBlockerCategory.OTHER,
        description="Domain category of the risk/blocker.",
    )
    severity: RiskBlockerSeverity = Field(
        default=RiskBlockerSeverity.MEDIUM,
        description="Impact severity level.",
    )
    status: RiskBlockerStatus = Field(
        default=RiskBlockerStatus.AI_SUGGESTED,
        description="Current lifecycle status.",
    )
    
    # ─────────────────────────────────────────────────────────────────────────
    # Content
    # ─────────────────────────────────────────────────────────────────────────
    title: str = Field(
        sa_column=Column(String(500), nullable=False),
        description="Short title/summary of the risk or blocker.",
    )
    description: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Detailed description of the issue.",
    )
    quote: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Direct quote from source (transcript/email) if extracted by AI.",
    )
    
    # ─────────────────────────────────────────────────────────────────────────
    # Source Attribution
    # ─────────────────────────────────────────────────────────────────────────
    source_type: RiskBlockerSource = Field(
        default=RiskBlockerSource.MANUAL,
        description="Source where this was identified.",
    )
    source_record_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True),
        description="ID of source record (meeting_id, email_association_id, etc.).",
    )
    source_context: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Human-readable source context (e.g., 'Discovery Call - Oct 7').",
    )
    
    # ─────────────────────────────────────────────────────────────────────────
    # AI Metadata
    # ─────────────────────────────────────────────────────────────────────────
    ai_confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="AI confidence score (0.0 - 1.0) for extracted items.",
    )
    ai_reasoning: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="AI explanation for why this was identified as risk/blocker.",
    )
    extraction_run_id: UUID | None = Field(
        default=None,
        description="ID of the AI extraction job that created this item.",
    )
    
    # ─────────────────────────────────────────────────────────────────────────
    # Assignment
    # ─────────────────────────────────────────────────────────────────────────
    owner_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Name of person responsible for addressing this.",
    )
    owner_email: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Email of person responsible.",
    )
    assigned_team: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True),
        description="Team responsible (e.g., 'engineering', 'legal', 'finance').",
    )
    
    # ─────────────────────────────────────────────────────────────────────────
    # Resolution
    # ─────────────────────────────────────────────────────────────────────────
    resolution_notes: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Notes on how this was resolved.",
    )
    resolved_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="When this was marked as resolved.",
    )
    resolved_by: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Who resolved this issue.",
    )
    
    # ─────────────────────────────────────────────────────────────────────────
    # Provenance & Audit (AI vs User Tracking)
    # ─────────────────────────────────────────────────────────────────────────
    is_ai_generated: bool = Field(
        default=False,
        description="True if originally created by AI extraction, False if manually added.",
    )
    created_by_user_id: UUID | None = Field(
        default=None,
        description="User ID who created this item (null for AI-generated).",
    )
    created_by_user_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Display name of user who created this item.",
    )
    last_modified_by_user_id: UUID | None = Field(
        default=None,
        description="User ID who last modified this item.",
    )
    last_modified_by_user_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Display name of user who last modified this item.",
    )
    user_confirmed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="When a user first confirmed/accepted an AI-suggested item.",
    )
    
    # ─────────────────────────────────────────────────────────────────────────
    # Timestamps
    # ─────────────────────────────────────────────────────────────────────────
    identified_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="When this risk/blocker was first identified.",
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    
    # ─────────────────────────────────────────────────────────────────────────
    # Extended Metadata
    # ─────────────────────────────────────────────────────────────────────────
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        description="Additional metadata (stakeholder mentions, related items, etc.).",
    )
```

### Field Mapping by Source Type

| Source Type | source_record_id | source_context Example |
|-------------|------------------|------------------------|
| `MEETING_TRANSCRIPT` | `calendar_event_deal_associations.association_id` or `meeting_id` | "Discovery Call - Oct 7" |
| `EMAIL` | `deal_email_associations.association_id` | "Re: Technical Requirements" |
| `SLACK` | `messaging_messages.message_id` | "#deal-acme-corp - Discussion" |
| `MANUAL` | `null` | "Added by John (AE)" |
| `CRM_NOTE` | `crm_activity.activity_id` | "HubSpot Note - Oct 5" |

### Metadata Examples

```json
// TECHNICAL blocker from meeting transcript
{
    "mentioned_stakeholders": ["Sarah Chen (CTO)", "Mike Johnson (VP Eng)"],
    "related_requirements": ["SOC2 compliance", "GDPR"],
    "transcript_segment_ids": ["seg-123", "seg-456"],
    "timestamp_in_recording": "00:15:30"
}

// BUDGET risk from email
{
    "mentioned_amount": "$500K",
    "budget_cycle": "Q2 2025",
    "decision_maker": "Finance Director"
}

// COMPETITION risk from Slack
{
    "competitor": "CompetitorCorp",
    "mentioned_features": ["Feature X", "Feature Y"],
    "channel_name": "#deal-acme-corp"
}
```

---

## Provenance & Audit Logic

### Tracking AI vs User Actions

The table tracks **origin** and **current state** separately to answer:
- "Was this created by AI or a user?"
- "Who last touched this item?"
- "When did a user confirm an AI suggestion?"

**Note**: The fields `created_by_*` and `last_modified_by_*` only track first creator and most recent modifier. For full change history (who changed what, when), use the `audit_trail` in the `risk_blocker_data` JSONB field.

### Audit Trail in risk_blocker_data

The `risk_blocker_data` JSONB field stores a complete audit trail of all modifications:

```json
{
    "audit_trail": [
        {
            "action": "created",
            "timestamp": "2025-10-07T14:30:00Z",
            "actor_type": "ai",
            "actor_id": null,
            "actor_name": "AI Extraction",
            "details": {
                "extraction_run_id": "uuid",
                "confidence": 0.87,
                "source": "meeting_transcript"
            }
        },
        {
            "action": "confirmed",
            "timestamp": "2025-10-08T09:15:00Z",
            "actor_type": "user",
            "actor_id": "user-uuid",
            "actor_name": "John Doe",
            "details": {
                "previous_status": "AI_SUGGESTED",
                "new_status": "OPEN"
            }
        },
        {
            "action": "updated",
            "timestamp": "2025-10-10T11:00:00Z",
            "actor_type": "user",
            "actor_id": "user-uuid-2",
            "actor_name": "Sarah Chen",
            "details": {
                "fields_changed": ["severity", "assigned_team"],
                "previous_values": {"severity": "MEDIUM", "assigned_team": null},
                "new_values": {"severity": "HIGH", "assigned_team": "engineering"}
            }
        },
        {
            "action": "updated",
            "timestamp": "2025-10-12T14:20:00Z",
            "actor_type": "user",
            "actor_id": "user-uuid",
            "actor_name": "John Doe",
            "details": {
                "fields_changed": ["owner_name"],
                "previous_values": {"owner_name": null},
                "new_values": {"owner_name": "Mike Johnson"}
            }
        },
        {
            "action": "resolved",
            "timestamp": "2025-10-15T16:45:00Z",
            "actor_type": "user",
            "actor_id": "user-uuid-3",
            "actor_name": "Mike Johnson",
            "details": {
                "previous_status": "IN_PROGRESS",
                "new_status": "RESOLVED",
                "resolution_notes": "Sandbox credentials received"
            }
        }
    ],
    "modification_count": 4,
    "unique_modifiers": ["user-uuid", "user-uuid-2", "user-uuid-3"],
    "mentioned_stakeholders": ["Sarah Chen (CTO)", "Mike Johnson (VP Eng)"]
}
```

### Audit Trail Action Types

| Action | When Used | Key Details Captured |
|--------|-----------|---------------------|
| `created` | Item first created | `actor_type` (ai/user), source info |
| `confirmed` | User accepts AI suggestion | Previous/new status |
| `updated` | Any field modification | Fields changed, old/new values |
| `assigned` | Owner/team assignment | Who was assigned |
| `resolved` | Marked as resolved | Resolution notes |
| `reopened` | Resolved item reopened | Reason for reopening |
| `dismissed` | User dismisses item | Dismissal reason |

### Service Method for Audit Trail

```python
# In RisksBlockersAccessService

def _append_audit_entry(
    self,
    item: DealRiskBlocker,
    action: str,
    user_id: UUID | None,
    user_name: str | None,
    details: dict,
) -> None:
    """Append an entry to the audit trail in risk_blocker_data."""
    
    # Initialize audit_trail if not exists
    if "audit_trail" not in item.risk_blocker_data:
        item.risk_blocker_data["audit_trail"] = []
    if "unique_modifiers" not in item.risk_blocker_data:
        item.risk_blocker_data["unique_modifiers"] = []
    
    # Create audit entry
    entry = {
        "action": action,
        "timestamp": datetime.now(UTC).isoformat(),
        "actor_type": "user" if user_id else "ai",
        "actor_id": str(user_id) if user_id else None,
        "actor_name": user_name or "AI Extraction",
        "details": details,
    }
    
    # Append to trail
    item.risk_blocker_data["audit_trail"].append(entry)
    
    # Update modification count
    item.risk_blocker_data["modification_count"] = len(item.risk_blocker_data["audit_trail"])
    
    # Track unique modifiers
    if user_id and str(user_id) not in item.risk_blocker_data["unique_modifiers"]:
        item.risk_blocker_data["unique_modifiers"].append(str(user_id))


async def update(
    self,
    risk_blocker_id: UUID,
    updates: dict,
    modified_by_user_id: UUID,
) -> DealRiskBlocker | None:
    """Update a risk/blocker with full audit trail."""
    
    item = await self.get_by_id(risk_blocker_id)
    if not item:
        return None
    
    # Get user name for audit
    user_name = await self._get_user_display_name(modified_by_user_id)
    
    # Track what's changing for audit
    fields_changed = []
    previous_values = {}
    new_values = {}
    
    for field, new_value in updates.items():
        old_value = getattr(item, field, None)
        if old_value != new_value:
            fields_changed.append(field)
            previous_values[field] = old_value
            new_values[field] = new_value
            setattr(item, field, new_value)
    
    if fields_changed:
        # Check if this is a confirmation (AI_SUGGESTED → OPEN)
        if "status" in updates and item.status == "AI_SUGGESTED" and updates["status"] == "OPEN":
            action = "confirmed"
            item.user_confirmed_at = datetime.now(UTC)
        else:
            action = "updated"
        
        # Append audit entry
        self._append_audit_entry(
            item,
            action=action,
            user_id=modified_by_user_id,
            user_name=user_name,
            details={
                "fields_changed": fields_changed,
                "previous_values": previous_values,
                "new_values": new_values,
            },
        )
        
        # Update last_modified fields
        item.last_modified_by_user_id = modified_by_user_id
        item.last_modified_by_user_name = user_name
        item.updated_at = datetime.now(UTC)
        
        await self.session.commit()
    
    return item
```

### Computed Properties for UI

```python
# Properties that can be computed from audit_trail for display

@property
def modification_count(self) -> int:
    """Total number of modifications (excluding creation)."""
    trail = self.risk_blocker_data.get("audit_trail", [])
    return len([e for e in trail if e["action"] != "created"])

@property
def unique_modifier_count(self) -> int:
    """Number of unique users who modified this item."""
    return len(self.risk_blocker_data.get("unique_modifiers", []))

@property
def last_modified_action(self) -> str | None:
    """Most recent action type."""
    trail = self.risk_blocker_data.get("audit_trail", [])
    return trail[-1]["action"] if trail else None
```

### State Transitions & Audit Updates

| Action | Field Updates | Audit Entry |
|--------|---------------|-------------|
| **AI creates item** | `is_ai_generated=True`, `status=AI_SUGGESTED`, `created_by_user_id=null` | `action=created`, `actor_type=ai` |
| **User creates item** | `is_ai_generated=False`, `status=OPEN`, `created_by_user_id={user}` | `action=created`, `actor_type=user` |
| **User confirms AI item** | `status=OPEN`, `user_confirmed_at=now()`, `last_modified_by_user_id={user}` | `action=confirmed` |
| **User dismisses AI item** | `status=DISMISSED`, `last_modified_by_user_id={user}` | `action=dismissed` |
| **User updates any field** | `last_modified_by_user_id={user}`, `updated_at=now()` | `action=updated`, `fields_changed=[...]` |
| **User resolves item** | `status=RESOLVED`, `resolved_at=now()`, `resolved_by={name}` | `action=resolved` |
| **User reopens item** | `status=OPEN`, `resolved_at=null` | `action=reopened` |

### API Response with Audit Summary

The list endpoint returns summary stats, detail endpoint returns full trail:

```python
class RiskBlockerDetail(RiskBlockerListItem):
    """Full detail view including audit trail."""
    
    # ... existing fields ...
    
    # Audit summary
    modification_count: int = Field(0, description="Total modifications")
    unique_modifier_count: int = Field(0, description="Unique users who modified")
    audit_trail: list[dict] | None = Field(None, description="Full change history")
```

### Display Logic for UI

```python
# Determine display badge
if item.is_ai_generated and item.status == RiskBlockerStatus.AI_SUGGESTED:
    badge = "AI SUGGESTED"  # Orange badge - needs review
elif item.is_ai_generated and item.user_confirmed_at:
    badge = "AI (Confirmed)"  # Green badge - AI-found, user-approved
elif item.is_ai_generated:
    badge = "AI"  # Blue badge - AI-found, status changed
else:
    badge = "Manual"  # No badge or gray - user-added

# Show modification indicator
if item.modification_count > 0:
    tooltip = f"Modified {item.modification_count} times by {item.unique_modifier_count} users"
```

---

## Deal Overview API Integration

The Risks & Blockers feature integrates with the **Deal Overview** page, following the existing patterns established by Tasks (`tasks_handler.py`) and Deal Summary (`deals_handler.py`).

### Architecture Overview

```
src/api/routers/crm/
├── deals_handler.py          # Existing - Deal detail, summary generation
├── tasks_handler.py          # Existing - GET /deals/{deal_id}/tasks
├── stakeholders_handler.py   # Existing - GET /deals/{deal_id}/stakeholders
├── meetings_handler.py       # Existing - GET /deals/{deal_id}/meetings
└── risks_blockers_handler.py # NEW - Risks & Blockers endpoints
```

### Response Models (crm_models.py additions)

```python
# src/api/schemas/crm_risk_blocker_models.py

class RiskBlockerListItem(BaseModel):
    """Individual risk/blocker item in list response."""
    
    risk_blocker_id: UUID
    deal_id: UUID
    item_type: str  # RISK or BLOCKER
    category: str
    severity: str
    status: str
    title: str
    description: str | None = None
    quote: str | None = None
    
    # Source attribution
    source_type: str
    source_context: str | None = None
    
    # AI metadata (for AI-suggested items)
    ai_confidence: float | None = None
    is_ai_generated: bool = False
    
    # Assignment
    owner_name: str | None = None
    assigned_team: str | None = None
    
    # Audit
    created_by_user_name: str | None = None
    last_modified_by_user_name: str | None = None
    
    # Timestamps
    identified_at: datetime
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None

    model_config = {"from_attributes": True}


class RiskBlockerDetail(RiskBlockerListItem):
    """Full detail view including AI reasoning and metadata."""
    
    ai_reasoning: str | None = None
    resolution_notes: str | None = None
    resolved_by: str | None = None
    user_confirmed_at: datetime | None = None
    metadata: dict[str, Any] | None = None


class RisksBlockersPage(BaseModel):
    """Paginated list of risks/blockers with summary stats."""
    
    items: list[RiskBlockerListItem]
    cursor: str | None = Field(None, description="Cursor for next page")
    

class RisksBlockersSummary(BaseModel):
    """Summary statistics for risks/blockers on a deal."""
    
    total_count: int
    total_risks: int
    total_blockers: int
    by_status: dict[str, int]
    by_severity: dict[str, int]
    ai_suggested_count: int  # Items pending review
    

class CreateRiskBlockerRequest(BaseModel):
    """Request to manually create a risk/blocker."""
    
    item_type: Literal["RISK", "BLOCKER"]
    category: str = "OTHER"
    severity: str = "MEDIUM"
    title: str = Field(..., max_length=500)
    description: str | None = None
    owner_name: str | None = None
    assigned_team: str | None = None


class UpdateRiskBlockerRequest(BaseModel):
    """Request to update a risk/blocker."""
    
    status: str | None = None
    severity: str | None = None
    category: str | None = None
    title: str | None = None
    description: str | None = None
    owner_name: str | None = None
    owner_email: str | None = None
    assigned_team: str | None = None


class ResolveRiskBlockerRequest(BaseModel):
    """Request to resolve a risk/blocker."""
    
    resolution_notes: str = Field(..., min_length=1)
```

---

## API Design

Following the existing patterns from `tasks_handler.py` and `deals_handler.py`:

### Handler Implementation: `risks_blockers_handler.py`

```python
# src/api/routers/crm/risks_blockers_handler.py

"""CRM Deal Risks & Blockers API handler."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_risk_blocker_models import (
    CreateRiskBlockerRequest,
    ResolveRiskBlockerRequest,
    RiskBlockerDetail,
    RisksBlockersPage,
    RisksBlockersSummary,
    UpdateRiskBlockerRequest,
)
from src.api.utils.http_response import Response
from src.service.crm.risks_blockers_access_service import (
    RisksBlockersAccessService,
    RisksBlockersAccessServiceError,
)

risks_blockers_router = APIRouter()


async def get_risks_blockers_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields a RisksBlockersAccessService instance."""
    service = RisksBlockersAccessService(
        org_session, 
        user_ids.organisation_id,
        user_ids.user_id,
    )
    try:
        yield service
    finally:
        await service.close()
```

### API Endpoints

#### 1. List Risks/Blockers for Deal

```
GET /deals/{deal_id}/risks-blockers
```

**Handler:**

```python
@risks_blockers_router.get(
    "/deals/{deal_id}/risks-blockers",
    response_model=GenericResponse[RisksBlockersPage],
    tags=["crm", "risks-blockers"],
    summary="List risks and blockers for a deal",
    description="""
    Retrieve risks and blockers associated with a deal.
    
    Returns both AI-suggested items (pending review) and user-confirmed items.
    Use filters to narrow down results by type, status, or severity.
    """,
)
async def list_deal_risks_blockers(
    deal_id: UUID,
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = Query(None),
    item_types: list[str] | None = Query(None, description="RISK, BLOCKER"),
    statuses: list[str] | None = Query(None, description="AI_SUGGESTED, OPEN, IN_PROGRESS, RESOLVED"),
    severities: list[str] | None = Query(None, description="LOW, MEDIUM, HIGH, CRITICAL"),
    categories: list[str] | None = Query(None),
    include_dismissed: bool = Query(False),
    service: RisksBlockersAccessService = Depends(get_risks_blockers_service),
    _rl_read: None = Depends(rate_limit_user_read),
):
    """List risks and blockers for a specific deal."""
    items, next_cursor = await service.list_by_deal(
        deal_id=deal_id,
        limit=limit,
        cursor=cursor,
        item_types=item_types,
        statuses=statuses,
        severities=severities,
        categories=categories,
        include_dismissed=include_dismissed,
    )
    return Response.success_response(
        message="Risks and blockers retrieved",
        data=RisksBlockersPage(items=items, cursor=next_cursor),
    )
```

**Response:**

```json
{
    "success": true,
    "message": "Risks and blockers retrieved",
    "data": {
        "items": [
            {
                "risk_blocker_id": "uuid",
                "deal_id": "uuid",
                "item_type": "BLOCKER",
                "category": "TECHNICAL",
                "severity": "HIGH",
                "status": "AI_SUGGESTED",
                "title": "Sandbox credentials not received",
                "description": "Still waiting for sandbox access. POC to start in 3 days.",
                "quote": "We're still waiting on the sandbox credentials from IT...",
                "source_type": "MEETING_TRANSCRIPT",
                "source_context": "Discovery Call - Oct 7",
                "ai_confidence": 0.87,
                "is_ai_generated": true,
                "owner_name": null,
                "assigned_team": null,
                "created_by_user_name": null,
                "identified_at": "2025-10-07T14:30:00Z",
                "created_at": "2025-10-07T14:30:00Z",
                "updated_at": "2025-10-07T14:30:00Z"
            },
            {
                "risk_blocker_id": "uuid",
                "deal_id": "uuid",
                "item_type": "RISK",
                "category": "BUDGET",
                "severity": "HIGH",
                "status": "IN_PROGRESS",
                "title": "Pricing approval pending",
                "description": "Finance team pushback on pricing.",
                "quote": null,
                "source_type": "MANUAL",
                "source_context": "Added by John (AE)",
                "ai_confidence": null,
                "is_ai_generated": false,
                "owner_name": "Finance",
                "assigned_team": "finance",
                "created_by_user_name": "John Doe",
                "identified_at": "2025-10-08T09:00:00Z",
                "created_at": "2025-10-08T09:00:00Z",
                "updated_at": "2025-10-10T11:00:00Z"
            }
        ],
        "cursor": null
    }
}
```

#### 2. Get Summary Statistics

```
GET /deals/{deal_id}/risks-blockers/summary
```

**Handler:**

```python
@risks_blockers_router.get(
    "/deals/{deal_id}/risks-blockers/summary",
    response_model=GenericResponse[RisksBlockersSummary],
    tags=["crm", "risks-blockers"],
    summary="Get risks/blockers summary for a deal",
)
async def get_deal_risks_blockers_summary(
    deal_id: UUID,
    service: RisksBlockersAccessService = Depends(get_risks_blockers_service),
    _rl_read: None = Depends(rate_limit_user_read),
):
    """Get summary statistics for risks and blockers on a deal."""
    summary = await service.get_summary(deal_id)
    return Response.success_response(
        message="Summary retrieved",
        data=RisksBlockersSummary(**summary),
    )
```

**Response:**

```json
{
    "success": true,
    "data": {
        "total_count": 5,
        "total_risks": 2,
        "total_blockers": 3,
        "by_status": {
            "AI_SUGGESTED": 2,
            "OPEN": 1,
            "IN_PROGRESS": 2
        },
        "by_severity": {
            "HIGH": 2,
            "MEDIUM": 2,
            "LOW": 1
        },
        "ai_suggested_count": 2
    }
}
```

#### 3. Get Single Risk/Blocker Detail

```
GET /risks-blockers/{risk_blocker_id}
```

**Handler:**

```python
@risks_blockers_router.get(
    "/risks-blockers/{risk_blocker_id}",
    response_model=GenericResponse[RiskBlockerDetail],
    tags=["crm", "risks-blockers"],
    summary="Get detailed risk/blocker information",
)
async def get_risk_blocker(
    risk_blocker_id: UUID,
    service: RisksBlockersAccessService = Depends(get_risks_blockers_service),
    _rl_read: None = Depends(rate_limit_user_read),
):
    """Get full details for a single risk or blocker."""
    item = await service.get_by_id(risk_blocker_id)
    if not item:
        return Response.error_response(message="Risk/blocker not found", status_code=404)
    return Response.success_response(
        message="Risk/blocker retrieved",
        data=RiskBlockerDetail.model_validate(item),
    )
```

#### 4. Create Risk/Blocker (Manual)

```
POST /deals/{deal_id}/risks-blockers
```

**Handler:**

```python
@risks_blockers_router.post(
    "/deals/{deal_id}/risks-blockers",
    response_model=GenericResponse[RiskBlockerDetail],
    tags=["crm", "risks-blockers"],
    summary="Manually add a risk or blocker",
)
async def create_risk_blocker(
    deal_id: UUID,
    request: CreateRiskBlockerRequest,
    user_ids: UserIDs = Depends(get_current_user),
    service: RisksBlockersAccessService = Depends(get_risks_blockers_service),
    _rl_write: None = Depends(rate_limit_user_write),
):
    """Manually create a new risk or blocker for a deal."""
    item = await service.create(
        deal_id=deal_id,
        item_type=request.item_type,
        category=request.category,
        severity=request.severity,
        title=request.title,
        description=request.description,
        owner_name=request.owner_name,
        assigned_team=request.assigned_team,
        # Provenance: manually created
        is_ai_generated=False,
        created_by_user_id=user_ids.user_id,
    )
    return Response.success_response(
        message="Risk/blocker created",
        data=RiskBlockerDetail.model_validate(item),
        status_code=201,
    )
```

#### 5. Update Risk/Blocker

```
PATCH /risks-blockers/{risk_blocker_id}
```

**Handler:**

```python
@risks_blockers_router.patch(
    "/risks-blockers/{risk_blocker_id}",
    response_model=GenericResponse[RiskBlockerDetail],
    tags=["crm", "risks-blockers"],
    summary="Update a risk or blocker",
)
async def update_risk_blocker(
    risk_blocker_id: UUID,
    request: UpdateRiskBlockerRequest,
    user_ids: UserIDs = Depends(get_current_user),
    service: RisksBlockersAccessService = Depends(get_risks_blockers_service),
    _rl_write: None = Depends(rate_limit_user_write),
):
    """Update an existing risk or blocker.
    
    When updating an AI_SUGGESTED item to OPEN status, this is treated as
    'user confirmation' and sets user_confirmed_at timestamp.
    """
    item = await service.update(
        risk_blocker_id=risk_blocker_id,
        updates=request.model_dump(exclude_unset=True),
        modified_by_user_id=user_ids.user_id,
    )
    if not item:
        return Response.error_response(message="Risk/blocker not found", status_code=404)
    return Response.success_response(
        message="Risk/blocker updated",
        data=RiskBlockerDetail.model_validate(item),
    )
```

#### 6. Resolve Risk/Blocker

```
POST /risks-blockers/{risk_blocker_id}/resolve
```

**Handler:**

```python
@risks_blockers_router.post(
    "/risks-blockers/{risk_blocker_id}/resolve",
    response_model=GenericResponse[RiskBlockerDetail],
    tags=["crm", "risks-blockers"],
    summary="Mark a risk or blocker as resolved",
)
async def resolve_risk_blocker(
    risk_blocker_id: UUID,
    request: ResolveRiskBlockerRequest,
    user_ids: UserIDs = Depends(get_current_user),
    service: RisksBlockersAccessService = Depends(get_risks_blockers_service),
    _rl_write: None = Depends(rate_limit_user_write),
):
    """Mark a risk or blocker as resolved with resolution notes."""
    item = await service.resolve(
        risk_blocker_id=risk_blocker_id,
        resolution_notes=request.resolution_notes,
        resolved_by_user_id=user_ids.user_id,
    )
    if not item:
        return Response.error_response(message="Risk/blocker not found", status_code=404)
    return Response.success_response(
        message="Risk/blocker resolved",
        data=RiskBlockerDetail.model_validate(item),
    )
```

#### 7. Delete/Dismiss Risk/Blocker

```
DELETE /risks-blockers/{risk_blocker_id}
```

**Handler:**

```python
@risks_blockers_router.delete(
    "/risks-blockers/{risk_blocker_id}",
    response_model=GenericResponse[dict],
    tags=["crm", "risks-blockers"],
    summary="Dismiss a risk or blocker",
)
async def dismiss_risk_blocker(
    risk_blocker_id: UUID,
    user_ids: UserIDs = Depends(get_current_user),
    service: RisksBlockersAccessService = Depends(get_risks_blockers_service),
    _rl_write: None = Depends(rate_limit_user_write),
):
    """Dismiss (soft-delete) a risk or blocker.
    
    Sets status to DISMISSED rather than hard-deleting.
    """
    success = await service.dismiss(
        risk_blocker_id=risk_blocker_id,
        dismissed_by_user_id=user_ids.user_id,
    )
    if not success:
        return Response.error_response(message="Risk/blocker not found", status_code=404)
    return Response.success_response(message="Risk/blocker dismissed")
```

#### 8. Trigger AI Extraction (Job-Based)

```
POST /deals/{deal_id}/risks-blockers/extract
```

Following the `deal-summary/generate` pattern:

**Handler:**

```python
@risks_blockers_router.post(
    "/deals/{deal_id}/risks-blockers/extract",
    response_model=GenericResponse[DealJobResponse],
    tags=["crm", "risks-blockers", "ai"],
    summary="Trigger AI extraction of risks and blockers",
    description="""
    Initiate AI-powered extraction of risks and blockers from deal communications.
    
    Analyzes meeting transcripts and emails to identify potential risks and blockers.
    Returns a job_id for tracking progress via SSE stream.
    """,
)
async def trigger_extraction(
    deal_id: UUID,
    sources: list[str] | None = Query(None, description="meetings, emails, slack"),
    service: DealJobService = Depends(get_deal_job_service),
):
    """Trigger AI extraction of risks/blockers for a deal."""
    job = await service.create_risk_blocker_extraction_job(deal_id, sources=sources)
    
    is_new = job.status == JobStatus.PENDING
    message = (
        "Risk/blocker extraction initiated"
        if is_new
        else "Extraction already in progress"
    )
    
    return Response.accepted_response(
        message,
        DealJobResponse(
            job_id=job.job_id,
            job_type=str(job.job_type),
            status=str(job.status),
        ),
    )
```

#### 9. SSE Stream for Extraction Progress

```
GET /deals/{deal_id}/risks-blockers/extract/stream/{job_id}
```

Following the `deal-summary/stream` pattern for SSE updates during AI extraction.

**Response:**

```json
{
    "job_id": "uuid",
    "status": "queued",
    "message": "Risk/blocker extraction queued for 5 meetings, 12 emails"
}
```

---

## AI Agent Architecture

### Overview

The Risk/Blocker extraction follows the established agent pattern used by `DealSummaryAgent`. It leverages existing tools to gather context and uses structured prompts to identify risks and blockers.

### Agent: `DealRiskBlockerAgent`

```
src/agents/workflows/risk_blocker/
├── __init__.py
├── agent.py           # DealRiskBlockerAgent class
├── models.py          # Input/output Pydantic models
└── prompts.py         # System and user prompts
```

### Input Model

```python
class RiskBlockerInput(BaseModel):
    """Input for risk/blocker extraction."""
    
    deal_id: str = Field(..., description="UUID of the deal")
    deal_name: str = Field(..., description="Name of the deal")
    company_name: str | None = Field(None, description="Company name")
    deal_stage: str | None = Field(None, description="Current deal stage")
    
    # Counts to help agent plan data gathering
    meeting_count: int = Field(0, description="Number of meetings available")
    email_count: int = Field(0, description="Number of emails linked to deal")
    slack_channel_count: int = Field(0, description="Number of Slack channels linked")
    
    # Existing risks/blockers (to avoid duplicates)
    existing_item_titles: list[str] = Field(
        default_factory=list,
        description="Titles of existing risks/blockers to avoid duplicates"
    )
```

### Output Model

```python
class ExtractedRiskBlocker(BaseModel):
    """Single risk or blocker extracted by AI."""
    
    item_type: Literal["RISK", "BLOCKER"]
    category: str = Field(..., description="Category from RiskBlockerCategory enum")
    severity: str = Field(..., description="Severity from RiskBlockerSeverity enum")
    title: str = Field(..., max_length=500, description="Short title")
    description: str = Field(..., description="Detailed description")
    quote: str | None = Field(None, description="Direct quote from source")
    source_type: str = Field(..., description="Source type")
    source_context: str = Field(..., description="Human-readable source context")
    source_record_id: str | None = Field(None, description="ID of source record")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence score")
    reasoning: str = Field(..., description="Why this was identified")
    mentioned_stakeholders: list[str] = Field(default_factory=list)


class RiskBlockerExtractionResult(BaseModel):
    """Output from risk/blocker extraction."""
    
    deal_id: str
    extracted_items: list[ExtractedRiskBlocker]
    sources_analysed: dict[str, int] = Field(
        default_factory=dict,
        description="Count of sources analysed by type"
    )
    extraction_summary: str = Field(
        ...,
        description="Brief summary of findings"
    )
```

### Tools Used

The agent reuses existing tools from the tool registry:

```python
RISK_BLOCKER_AGENT_TOOLS = [
    ToolName.LIST_DEAL_MEETINGS,
    ToolName.GET_MEETING_DETAILS,
    ToolName.GET_MEETING_TRANSCRIPT,
    ToolName.LIST_DEAL_EMAILS,
    ToolName.GET_EMAIL_DETAILS,
    ToolName.LIST_DEAL_STAKEHOLDERS,
    ToolName.GET_STAKEHOLDER_DETAILS,
]
```

### System Prompt

```python
RISK_BLOCKER_SYSTEM_PROMPT = """You are an expert pre-sales analyst specialising in identifying deal risks and blockers.

Your task is to analyse deal communications (meeting transcripts, emails) and identify:

1. **BLOCKERS**: Active impediments that MUST be resolved for the deal to progress
   - Examples: Missing credentials, awaiting approvals, resource unavailable
   - Indicators: "waiting for", "blocked by", "can't proceed until", "need before"

2. **RISKS**: Potential issues that MAY negatively impact the deal
   - Examples: Budget concerns, competitive threats, stakeholder changes, timeline pressure
   - Indicators: "concerned about", "worried", "might", "could be a problem", "competitor"

For each identified item, you MUST provide:
- Classification (RISK or BLOCKER)
- Category (TECHNICAL, BUDGET, TIMELINE, STAKEHOLDER, SECURITY, etc.)
- Severity (LOW, MEDIUM, HIGH, CRITICAL)
- Direct quote from the source that indicates this issue
- Confidence score (0.0-1.0) based on how explicit the issue is
- Clear reasoning for why this is a risk/blocker

IMPORTANT GUIDELINES:
- Be specific - vague concerns are not actionable
- Include direct quotes when available
- Distinguish between customer-side and internal-side blockers
- Consider the deal stage when assessing severity
- Avoid duplicating items already identified (check existing_item_titles)
- Prioritise quality over quantity - only flag genuine risks/blockers

Output format: Return a structured list of ExtractedRiskBlocker items.
"""
```

### Agent Implementation

```python
class DealRiskBlockerAgent:
    """Agent for extracting risks and blockers from deal communications."""
    
    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        publisher: DealJobRedisPublisher | None = None,
    ):
        self.session = session
        self.organisation_id = organisation_id
        self.publisher = publisher
        
        # Build context for tools
        self.context = AgentContext(
            session=session,
            organisation_id=organisation_id,
        )
        
        # Get tools
        registry = ToolRegistry(self.context, lambda: None)
        tools = registry.get_tools(RISK_BLOCKER_AGENT_TOOLS)
        
        # Create agent
        self.agent = Agent(
            model=AzureOpenAI(id="gpt-5", api_version="2024-12-01-preview"),
            response_model=RiskBlockerExtractionResult,
            description=RISK_BLOCKER_SYSTEM_PROMPT,
            tools=tools,
            show_tool_calls=True,
        )
    
    async def extract(self, input_data: RiskBlockerInput) -> RiskBlockerExtractionResult:
        """Extract risks and blockers from deal communications."""
        user_prompt = build_risk_blocker_user_prompt(input_data)
        response = await self.agent.arun(user_prompt)
        return cast(RiskBlockerExtractionResult, response.content)
```

---

## Celery Task Architecture

### Task: `process_risk_blocker_extraction`

Following the pattern from `deal_summary_tasks.py`:

```python
# src/workers/deal_worker/tasks/risk_blocker_tasks.py

@app.task(bind=True, name="risk_blocker.extract", acks_late=True, max_retries=3)
def extract_deal_risks_blockers_task(
    self: Task,
    organisation_id: str,
    job_id: str,
):
    """Extract risks and blockers for a deal using AI.
    
    Args:
        organisation_id: The organisation ID (string for JSON serialization)
        job_id: The job ID to process (string for JSON serialization)
    
    Returns:
        Dictionary with extraction results
    """
    async def _run():
        org_uuid = UUID(organisation_id)
        job_uuid = UUID(job_id)
        
        async for session in acquire_org_async_session(org_uuid):
            # 1. Load job
            job = await session.get(DealJobs, job_uuid)
            if not job:
                raise RuntimeError(f"Deal job {job_id} not found")
            
            publisher = DealJobRedisPublisher(org_uuid, job_uuid, str(job.job_type))
            
            try:
                # 2. Update status
                job.status = JobStatus.IN_PROGRESS
                await session.commit()
                
                # 3. Create service and extract
                service = RiskBlockerExtractionService(session, org_uuid, publisher)
                result = await service.extract_for_deal(job.deal_id)
                
                # 4. Save results
                job.status = JobStatus.COMPLETED
                job.result_data = result
                await session.commit()
                
                # 5. Publish completion
                publisher.publish_result(result)
                publisher.publish_finalize("done")
                
                return {"status": "completed", "job_id": job_id, "items_found": len(result["items"])}
                
            except RiskBlockerExtractionError as e:
                # Handle non-retryable errors
                ...
    
    try:
        return run_async(_run())
    except Exception as e:
        raise self.retry(exc=e, countdown=60 * (2 ** self.request.retries)) from e
```

### Service: `RiskBlockerExtractionService`

```python
# src/workers/deal_worker/services/risk_blocker_service.py

class RiskBlockerExtractionService:
    """Service for extracting and persisting deal risks/blockers."""
    
    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        publisher: DealJobRedisPublisher | None = None,
    ):
        self.session = session
        self.organisation_id = organisation_id
        self.publisher = publisher
    
    async def extract_for_deal(self, deal_id: UUID) -> dict:
        """Extract risks/blockers for a deal and persist them.
        
        1. Load deal context
        2. Get existing risks/blockers (to avoid duplicates)
        3. Call DealRiskBlockerAgent for extraction
        4. Persist new items to database
        5. Create timeline events
        
        Returns:
            Dictionary with extraction results
        """
        # 1. Load deal
        deal = await get_deal(self.session, self.organisation_id, deal_id)
        if not deal:
            raise DealNotFoundError(deal_id)
        
        # 2. Get counts for agent planning
        meeting_count = await count_deal_meetings(self.session, self.organisation_id, deal_id)
        email_count = await count_deal_emails(self.session, self.organisation_id, deal_id)
        
        # 3. Get existing items to avoid duplicates
        existing_items = await self._get_existing_items(deal_id)
        existing_titles = [item.title for item in existing_items]
        
        # 4. Build input
        input_data = RiskBlockerInput(
            deal_id=str(deal_id),
            deal_name=deal.name,
            company_name=deal.company_name,
            deal_stage=deal.stage_label or deal.stage_name,
            meeting_count=meeting_count,
            email_count=email_count,
            existing_item_titles=existing_titles,
        )
        
        # 5. Call agent
        agent = DealRiskBlockerAgent(self.session, self.organisation_id, self.publisher)
        result = await agent.extract(input_data)
        
        # 6. Persist items
        created_items = await self._persist_items(deal_id, result.extracted_items)
        
        # 7. Create timeline events
        await self._create_timeline_events(deal_id, created_items)
        
        return {
            "deal_id": str(deal_id),
            "items": [item.model_dump() for item in created_items],
            "sources_analysed": result.sources_analysed,
            "summary": result.extraction_summary,
        }
    
    async def _persist_items(
        self,
        deal_id: UUID,
        extracted_items: list[ExtractedRiskBlocker]
    ) -> list[DealRiskBlocker]:
        """Persist extracted items to database."""
        created = []
        for item in extracted_items:
            db_item = DealRiskBlocker(
                organisation_id=self.organisation_id,
                deal_id=deal_id,
                item_type=RiskBlockerType(item.item_type),
                category=RiskBlockerCategory(item.category),
                severity=RiskBlockerSeverity(item.severity),
                status=RiskBlockerStatus.AI_SUGGESTED,
                title=item.title,
                description=item.description,
                quote=item.quote,
                source_type=RiskBlockerSource(item.source_type),
                source_record_id=item.source_record_id,
                source_context=item.source_context,
                ai_confidence=item.confidence,
                ai_reasoning=item.reasoning,
                # Provenance: AI-generated
                is_ai_generated=True,
                created_by_user_id=None,  # AI, not user
                created_by_user_name=None,
                metadata={"mentioned_stakeholders": item.mentioned_stakeholders},
            )
            self.session.add(db_item)
            created.append(db_item)
        
        await self.session.commit()
        return created
```

---

## Database Migration

### Alembic Migration Script

```python
def upgrade():
    # Create enum types (using string columns for flexibility)
    
    # Create deal_risks_blockers table
    op.create_table(
        'deal_risks_blockers',
        sa.Column('risk_blocker_id', sa.UUID(), nullable=False),
        sa.Column('organisation_id', sa.UUID(), nullable=False),
        sa.Column('deal_id', sa.UUID(), nullable=False),
        sa.Column('item_type', sa.String(32), nullable=False),
        sa.Column('category', sa.String(32), nullable=False),
        sa.Column('severity', sa.String(32), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('title', sa.String(500), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('quote', sa.Text(), nullable=True),
        sa.Column('source_type', sa.String(32), nullable=False),
        sa.Column('source_record_id', sa.String(512), nullable=True),
        sa.Column('source_context', sa.String(255), nullable=True),
        sa.Column('ai_confidence', sa.Float(), nullable=True),
        sa.Column('ai_reasoning', sa.Text(), nullable=True),
        sa.Column('extraction_run_id', sa.UUID(), nullable=True),
        sa.Column('owner_name', sa.String(255), nullable=True),
        sa.Column('owner_email', sa.String(255), nullable=True),
        sa.Column('assigned_team', sa.String(128), nullable=True),
        sa.Column('resolution_notes', sa.Text(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_by', sa.String(255), nullable=True),
        # Provenance & Audit fields
        sa.Column('is_ai_generated', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('created_by_user_id', sa.UUID(), nullable=True),
        sa.Column('created_by_user_name', sa.String(255), nullable=True),
        sa.Column('last_modified_by_user_id', sa.UUID(), nullable=True),
        sa.Column('last_modified_by_user_name', sa.String(255), nullable=True),
        sa.Column('user_confirmed_at', sa.DateTime(timezone=True), nullable=True),
        # Timestamps
        sa.Column('identified_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('metadata', JSONB(), nullable=False, server_default='{}'),
        sa.PrimaryKeyConstraint('risk_blocker_id'),
        sa.ForeignKeyConstraint(['deal_id'], ['crm_deals.deal_id']),
    )
    
    # Create indexes
    op.create_index('ix_risk_blocker_deal_status', 'deal_risks_blockers', ['deal_id', 'status'])
    op.create_index('ix_risk_blocker_deal_type', 'deal_risks_blockers', ['deal_id', 'item_type'])
    op.create_index('ix_risk_blocker_org_status', 'deal_risks_blockers', ['organisation_id', 'status'])
    op.create_index('ix_risk_blocker_org_severity', 'deal_risks_blockers', ['organisation_id', 'severity'])
    op.create_index('ix_risk_blocker_category', 'deal_risks_blockers', ['organisation_id', 'category'])
    op.create_index('ix_risk_blocker_source_ref', 'deal_risks_blockers', ['source_type', 'source_record_id'])
    # Index for AI-generated items pending review
    op.create_index('ix_risk_blocker_ai_suggested', 'deal_risks_blockers', ['organisation_id', 'is_ai_generated', 'status'])


def downgrade():
    op.drop_table('deal_risks_blockers')
```

---

## Implementation Phases

### Phase 1: Foundation (Sprint 1)

- [ ] Design document review and approval
- [ ] Create `DealRiskBlocker` model and enums
- [ ] Create database migration
- [ ] Basic CRUD API endpoints
- [ ] Manual add/update/resolve functionality

### Phase 2: AI Extraction (Sprint 2)

- [ ] Create `DealRiskBlockerAgent` with prompts
- [ ] Create `RiskBlockerExtractionService`
- [ ] Create Celery task `extract_deal_risks_blockers_task`
- [ ] Extraction API endpoint (trigger extraction)
- [ ] Job queue integration (reuse existing `DealJobs` pattern)

### Phase 3: Integration (Sprint 3)

- [ ] Timeline event creation on risk/blocker changes
- [ ] Update tool registry for new agent
- [ ] SSE progress updates during extraction
- [ ] Frontend integration support (response schemas)

### Phase 4: Enhancement (Future)

- [ ] Periodic background extraction for all deals
- [ ] Slack-specific extraction (requires Slack message association)
- [ ] Risk/blocker analytics dashboard
- [ ] Smart duplicate detection (semantic similarity)
- [ ] Auto-resolve detection (AI identifies when issue mentioned as resolved)

---

## Performance Considerations

### Query Patterns

| Query | Index Used | Expected Performance |
|-------|------------|---------------------|
| All items for deal by status | `ix_risk_blocker_deal_status` | < 5ms |
| Filter by type | `ix_risk_blocker_deal_type` | < 5ms |
| Org-wide severity report | `ix_risk_blocker_org_severity` | < 20ms for 10K items |
| Category distribution | `ix_risk_blocker_category` | < 20ms |

### Storage Estimates

| Field | Avg Size | Notes |
|-------|----------|-------|
| Fixed columns | ~200 bytes | UUIDs, timestamps, enums |
| Title/description | ~300 bytes | Variable length |
| Quote | ~200 bytes | Truncated excerpt |
| AI fields | ~150 bytes | Confidence, reasoning |
| Metadata JSON | ~100 bytes | Stakeholders, related |
| **Total per item** | **~950 bytes** | |

For 100 deals × 10 items each: ~1 MB per organisation

### AI Extraction Performance

- Estimated 3-5 seconds per meeting transcript analysis
- Parallel tool calls for multiple meetings
- Rate limiting: Max 10 meetings per extraction request
- Batch processing for org-wide extraction

---

## Security Considerations

1. **Data Access**: Risks/blockers inherit deal-level access control
2. **AI Content**: Mark AI-generated content clearly (confidence scores, AI badges)
3. **Audit Trail**: Track who created, modified, resolved items
4. **PII in Quotes**: Be cautious about storing verbatim quotes with customer names

---

## Open Questions for Review

1. **Deduplication Strategy**: How aggressively should we deduplicate similar risks/blockers across extraction runs?

2. **Auto-Dismiss Threshold**: Should AI-suggested items below a confidence threshold be auto-dismissed?

3. **Notification Integration**: Should status changes trigger notifications to deal owners?

4. **CRM Sync**: Should resolved blockers be synced back to CRM as notes/activities?

5. **Historical Analysis**: Should we analyse historical transcripts for closed-won/lost deals to train patterns?

6. **Multi-Deal Blockers**: Can a single blocker span multiple deals (e.g., company-wide security concern)?

---

## Appendix: UI Reference

Based on the mockup provided:

### Risks & Blockers Section Layout

```
┌─────────────────────────────────────────────────────────────────┐
│ Risks & Blockers                                                │
├─────────────────────────────────────────────────────────────────┤
│ ⚠️ AI SUGGESTED  Sandbox credentials not received               │
│    "Still waiting for sandbox access." POC to start in 3 days   │
│    Source: Slack channel discussion                             │
├─────────────────────────────────────────────────────────────────┤
│ ⚠️ AI SUGGESTED  Data anonymization concerns                    │
│    "We need internal approval to share real customer data"      │
│    Source: Call Transcript(Gong) - Evaluation call              │
├─────────────────────────────────────────────────────────────────┤
│ TITLE              TYPE       OWNER        SOURCE    STATUS     │
│ Pricing approval   🔺 RISK    Finance      🔮 Gong   In Progress│
│ Integration API    🛑 BLOCKER Customer IT  Manual    Open       │
│ Legal review MSA   🔺 RISK    Legal        💬 Slack  Open       │
├─────────────────────────────────────────────────────────────────┤
│ + Add a blocker to track                                        │
└─────────────────────────────────────────────────────────────────┘
```

### Display Logic

1. **AI Suggested Section** (top): Items with `status = AI_SUGGESTED`, sorted by confidence descending
2. **Table Section** (below): Items with `status IN (OPEN, IN_PROGRESS)`, sorted by severity then date
3. **Add Button**: Opens manual entry form

---

## Related Documentation

- [Deal Timeline Feature](deal-timeline-feature.md) - Timeline integration architecture
- [deal-worker-tasks.md](../Technical%20Documentation/deal/deal-worker-tasks.md) - Celery task patterns
- [CRM Activities Model](../../src/domain/models/organisation_tables/sync/crm/activities.py) - Activities table reference
