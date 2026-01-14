# Stakeholder Change Detection Logic

**Created**: January 2026
**File**: `src/workers/deal_worker/services/deal_change_detector.py`

---

## Overview

The change detection system identifies modifications to deal participants during CRM synchronization. When HubSpot or Salesforce data is synced, this system detects:
- New deals being created
- Participants added to existing deals
- Participant information updated (email changes)
- Participants removed from deals

These detected changes trigger stakeholder synchronization and calendar/email re-matching.

---

## Why Change Detection?

Without change detection, we would have two inefficient options:

1. **Full resync every time**: Re-process all stakeholders for all deals (expensive, slow)
2. **No historical matching**: Only match new calendar events/emails to existing stakeholders (misses history)

Change detection enables **targeted processing**:
- Only process deals that actually changed
- Trigger re-matching only when new participants are added
- Preserve historical associations when participants are removed

---

## The Detection Algorithm

### High-Level Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  CRM SYNC INCOMING DATA                                                     │
│                                                                             │
│  For each deal from HubSpot/Salesforce:                                     │
│  - deal_external_id, deal_name, stage, amount, ...                          │
│  - participants: [                                                          │
│      {type: CONTACT, external_id: "123", email: "alice@acme.com"},          │
│      {type: COMPANY, external_id: "456", name: "Acme Corp"},                │
│      {type: USER, external_id: "789", email: "bob@internal.com"}            │
│    ]                                                                        │
└───────────────────────────────────┬─────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 1: LOAD EXISTING PARTICIPANTS FROM DATABASE                           │
│                                                                             │
│  Query: SELECT * FROM crm_deal_participants                                 │
│         WHERE integration_id = {current_integration}                        │
│         AND deal_id IN ({deal_ids_being_synced})                            │
│                                                                             │
│  Build lookup map: deal_id → {lookup_key → CRMDealParticipant}              │
└───────────────────────────────────┬─────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 2: FOR EACH DEAL, CLASSIFY AS NEW OR EXISTING                         │
│                                                                             │
│  IF deal_external_id NOT in our database:                                   │
│  → Mark as NEW_DEAL                                                         │
│  → All CONTACT participants become stakeholders                             │
│                                                                             │
│  IF deal_external_id EXISTS in database:                                    │
│  → Compare existing vs incoming participants                                │
│  → Detect ADDED, UPDATED, REMOVED                                           │
└───────────────────────────────────┬─────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 3: OUTPUT CHANGE RESULTS                                              │
│                                                                             │
│  Return: dict[deal_id, DealChangeResult]                                    │
│  Each result contains list of ParticipantChange objects                     │
└─────────────────────────────────────────────────────────────────────────────┘
```

### The Comparison Logic (Two-Pass Algorithm)

For existing deals, we use a two-pass comparison:

```
PASS 1: Find ADDITIONS and UPDATES
────────────────────────────────────
For each INCOMING participant (from CRM API):
  1. Build lookup key: "{type}:{external_id}:{role}"
  2. Mark this key as "seen"
  3. Look up key in existing participants map

  IF not found:
    → PARTICIPANT_ADDED (new stakeholder)

  IF found AND email differs:
    → PARTICIPANT_UPDATED (email changed)

PASS 2: Find REMOVALS
────────────────────────────────────
For each EXISTING participant (from database):
  1. Check if its key was "seen" in Pass 1

  IF not seen:
    → PARTICIPANT_REMOVED (no longer in CRM)
```

### Visual Example

```
EXISTING (in database)              INCOMING (from CRM API)
─────────────────────               ───────────────────────
CONTACT:123:decision_maker          CONTACT:123:decision_maker  ← SAME (no change)
CONTACT:456:influencer              CONTACT:789:technical_buyer ← 789 is ADDED
CONTACT:999:champion                                            ← 999 is REMOVED
                                    CONTACT:456:influencer      ← SAME (no change)
```

Result:
- `CONTACT:789:technical_buyer` → **PARTICIPANT_ADDED**
- `CONTACT:999:champion` → **PARTICIPANT_REMOVED**

---

## The Lookup Key

Each participant is uniquely identified within a deal using a composite key:

```
Key Format: "{participant_type}:{related_external_id}:{relationship_role}"

Examples:
- "contact:hubspot_12345:decision_maker"
- "contact:salesforce_abc123:technical_buyer"
- "company:hubspot_comp_789:"
- "user:hubspot_user_001:owner"
```

### Why Include Role in the Key?

The same contact might have multiple roles on a deal:
- Alice could be both "decision_maker" and "billing_contact"
- These are tracked as separate participant records

```
alice@acme.com as decision_maker  → Key: "contact:123:decision_maker"
alice@acme.com as billing_contact → Key: "contact:123:billing_contact"
```

Both are valid, separate stakeholder relationships.

---

## Why Integration ID Matters

### Multi-CRM Scenario

An organization might have:
- **HubSpot** for SMB sales team
- **Salesforce** for Enterprise sales team

Each CRM has its own:
- Deal records
- Participant associations
- External IDs

### The Problem Without Integration Scoping

```
Without integration_id scoping:

HubSpot sync runs:
- Deal "Acme Contract" has participants: Alice, Bob, Charlie

Salesforce sync runs:
- Deal "Acme Enterprise" has participants: Alice, Diana

When detecting changes for Salesforce sync:
- System sees Bob and Charlie in database (from HubSpot)
- Bob and Charlie are NOT in Salesforce incoming data
- System incorrectly flags them as REMOVED! ❌
```

### The Solution: Integration Scoping

```python
# Query only participants from THIS integration
stmt = select(CRMDealParticipant).where(
    col(CRMDealParticipant.integration_id) == self.integration_id,  # ← KEY!
    col(CRMDealParticipant.deal_id).in_(deal_ids),
)
```

This ensures:
- HubSpot sync only compares against HubSpot participants
- Salesforce sync only compares against Salesforce participants
- No cross-contamination between CRMs

---

## Change Types and Their Implications

### NEW_DEAL

**When**: A deal is synced for the first time (not in our database)

**Action**:
- Create `DealStakeholder` records for all CONTACT participants
- All contacts become known stakeholders for matching

**Triggers**:
- Task 2: Match calendar events to this deal
- Task 3: Match emails to this deal

### PARTICIPANT_ADDED

**When**: A new contact is added to an existing deal in the CRM

**Action**:
- Create new `DealStakeholder` record
- This stakeholder can now be matched against calendar/email

**Triggers**:
- Task 2: Re-match all calendar events for this deal (to find historical meetings)
- Task 3: Re-match all emails for this deal (to find historical emails)

### PARTICIPANT_UPDATED

**When**: A contact's email address changes in the CRM

**Action**:
- Update `DealStakeholder.email`
- Future matching will use the new email

**Triggers**:
- No re-matching (email change doesn't mean new history to find)

### PARTICIPANT_REMOVED

**When**: A contact is removed from the deal in the CRM

**Action**:
- Mark `DealStakeholder.is_active = False` (soft delete)
- **DO NOT** remove calendar/email associations

**Triggers**:
- None (historical data is preserved)

**Reasoning**:
- The meetings happened, the emails were sent - that's historical fact
- User might want to see "who was involved before the contact left"
- If wrong association, user can manually remove from frontend

---

## Data Structures

### StakeholderChangeType Enum

```python
class StakeholderChangeType(str, Enum):
    NEW_DEAL = "NEW_DEAL"              # New deal created
    PARTICIPANT_ADDED = "PARTICIPANT_ADDED"    # New contact on existing deal
    PARTICIPANT_UPDATED = "PARTICIPANT_UPDATED"  # Contact email changed
    PARTICIPANT_REMOVED = "PARTICIPANT_REMOVED"  # Contact removed from deal
    MANUAL_REFRESH = "MANUAL_REFRESH"          # User-triggered resync
```

### ParticipantChange Dataclass

```python
@dataclass
class ParticipantChange:
    change_type: StakeholderChangeType
    deal_id: UUID
    integration_id: UUID                    # Which CRM this is from
    integration_type: IntegrationType       # HUBSPOT, SALESFORCE, etc.
    participant_type: CRMParticipantType    # CONTACT, COMPANY, USER
    related_external_id: str                # CRM's ID for the entity
    email: str | None                       # Contact email (for matching)
    name: str | None                        # Contact display name
    relationship_role: str | None           # Role in deal
    participant_id: UUID | None             # DB record ID (for updates/removals)
```

### DealChangeResult Dataclass

```python
@dataclass
class DealChangeResult:
    deal_id: UUID
    integration_id: UUID
    integration_type: IntegrationType
    is_new_deal: bool
    changes: list[ParticipantChange]

    @property
    def has_additions(self) -> bool:
        """True if re-matching should be triggered."""
        return self.is_new_deal or any(
            c.change_type == StakeholderChangeType.PARTICIPANT_ADDED
            for c in self.changes
        )
```

---

## Integration with CRM Sync

### Where Change Detection is Called

```
CRM Sync Task (org_task.py)
    │
    ├── 1. Fetch deals from HubSpot/Salesforce API
    │
    ├── 2. Upsert deals to CRMDeal table
    │
    ├── 3. Upsert participants to CRMDealParticipant table
    │
    └── 4. Call change detection ← HERE
            │
            ├── Input: deal_records, incoming_participants, new_deal_ids
            │
            └── Output: dict[deal_id, DealChangeResult]
                    │
                    └── For each result with changes:
                        Dispatch sync_deal_stakeholders_task (Task 1)
```

### Usage Example

```python
# In CRM sync task
from src.workers.deal_worker.services.deal_change_detector import detect_deal_changes

# After upserting deals and participants...
changes = await detect_deal_changes(
    session=session,
    organisation_id=org_id,
    integration_id=integration.integration_id,
    integration_type=integration.integration_type,
    deal_records=synced_deals,
    incoming_participants_map=participants_by_deal,
    new_deal_ids=newly_created_deal_ids,
)

# Dispatch stakeholder sync for deals with changes
for deal_id, result in changes.items():
    if result.has_changes:
        sync_deal_stakeholders_task.delay(
            organisation_id=str(org_id),
            deal_id=str(deal_id),
            change_type=result.changes[0].change_type.value,
        )
```

---

## Edge Cases Handled

### 1. Participant Type Filtering

Only CONTACT type participants are tracked as stakeholders:
- **CONTACT**: External person (stakeholder) ✓
- **USER**: Internal team member ✗
- **COMPANY**: Organization entity ✗

USER and COMPANY participants are used for matching but not stored as stakeholders.

### 2. Soft-Deleted Participants

Query filters out soft-deleted records:
```python
col(CRMDealParticipant.is_deleted) == False
```

Deleted participants don't participate in comparison.

### 3. Email Case Sensitivity

Email comparison is case-insensitive:
```python
if new_email.lower() != old_email.lower():
    # Flag as UPDATED
```

### 4. Missing External IDs

Empty external IDs are handled in key generation:
```python
key = f"{type}:{external_id or ''}:{role or ''}"
```

### 5. Provider-Specific Participant Types

Different CRMs may use different participant type names. The code normalizes to enum:
```python
p_type = (
    p.participant_type.value
    if isinstance(p.participant_type, CRMParticipantType)
    else str(p.participant_type)
)
```

---

## Summary

| Aspect | Implementation |
|--------|----------------|
| **Algorithm** | Two-pass comparison (additions/updates, then removals) |
| **Lookup Key** | `{type}:{external_id}:{role}` |
| **Integration Scoping** | Changes are scoped to single integration_id |
| **Tracked Types** | Only CONTACT participants become stakeholders |
| **Removal Strategy** | Soft-delete (keep associations, mark inactive) |
| **Re-matching Trigger** | NEW_DEAL and PARTICIPANT_ADDED only |

---

## Related Documents

- [Calendar & Email Stakeholder Mapping Plan](../../../.claude/plans/jolly-spinning-fog.md)
- [Deal Participants](./deal-participants.md)
- [Deal Meeting Automation](./deal-meeting-automation.md)
- [Deal Email Matching](./deal-email-matching-automation.md)
