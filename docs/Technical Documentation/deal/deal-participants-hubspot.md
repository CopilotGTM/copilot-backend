# Deal Participants - HubSpot Integration

**Created**: January 2026
**File**: `src/domain/models/organisation_tables/sync/crm/deals.py`

---

## Overview

This document describes how deal participants are structured and synced from HubSpot CRM to the `CRMDealParticipant` table, and subsequently to `DealStakeholder`.

Current CRM identity contract: `CRMDealParticipant` links to local CRM entities
with `related_local_id`. HubSpot object IDs and raw payloads are stored in
`crm_source_object_mappings` for integration and migration lookup.

---

## Architecture: Normalized Data Layer

```
┌─────────────────────────────────────────────────────────────────┐
│  PROVIDER LAYER (CRM-Specific)                                  │
│                                                                 │
│  HubSpot                    Salesforce                          │
│  - Associations API         - OpportunityContactRole            │
│  - Contact associations     - Contact lookup                    │
│  - Company associations     - Account lookup                    │
│  - User/Owner lookup        - Owner lookup                      │
└─────────────────────────────┬───────────────────────────────────┘
                              │
                              ▼ NORMALIZE
┌─────────────────────────────────────────────────────────────────┐
│  NORMALIZED LAYER (Provider-Agnostic)                           │
│                                                                 │
│  CRMDealParticipant table                                       │
│  - participant_type: CONTACT, USER, COMPANY                     │
│  - related_email: email address                                 │
│  - related_name: display name                                   │
│  - related_local_id: local CRM entity ID                        │
│  - relationship_role: role in deal                              │
└─────────────────────────────┬───────────────────────────────────┘
                              │
                              ▼ FILTER (type=CONTACT only)
┌─────────────────────────────────────────────────────────────────┐
│  APPLICATION LAYER                                              │
│                                                                 │
│  DealStakeholder table                                          │
│  - Only CONTACT participants become stakeholders                │
│  - USER = internal team (for matching, not stakeholders)        │
│  - COMPANY = organization (for domain matching)                 │
└─────────────────────────────────────────────────────────────────┘
```

**Key Point**: Stakeholder sync reads from `CRMDealParticipant` and does NOT need provider-specific logic. The CRM sync layer handles HubSpot/Salesforce differences.

---

## Data Model: CRMDealParticipant

```python
class CRMDealParticipant(SQLModel, table=True):
    """Represents a participant (contact, company, or user) associated with a CRM deal."""

    participant_id: UUID              # Primary key
    organisation_id: UUID             # Organisation scope
    integration_id: UUID              # Which CRM integration (HubSpot instance)
    deal_id: UUID                     # FK to CRMDeal

    # Participant identification
    participant_type: CRMParticipantType  # CONTACT, COMPANY, USER
    related_local_id: UUID | None     # Local CRM entity ID when resolved
    related_email: str | None         # Email address (for CONTACT type)
    related_name: str | None          # Display name

    # Role and status
    relationship_role: str | None     # Role in deal (e.g., "decision_maker")
    is_primary: bool                  # Primary contact flag
    is_deleted: bool                  # Soft delete flag

    # Timestamps
    synced_at: datetime               # Last sync from HubSpot
    created_at: datetime
    updated_at: datetime
```

---

## Participant Types

```python
class CRMParticipantType(str, Enum):
    CONTACT = "contact"    # External person (becomes stakeholder)
    COMPANY = "company"    # Organization entity
    USER = "user"          # Internal team member (HubSpot user)
```

### Type Usage

| Type | Description | Becomes Stakeholder? | Used For |
|------|-------------|---------------------|----------|
| `CONTACT` | External contacts associated with deal | ✅ Yes | Stakeholder creation |
| `COMPANY` | Companies associated with deal | ❌ No | Domain matching |
| `USER` | Internal team members (deal owner, etc.) | ❌ No | Calendar/email matching |

---

## Database Example (from HubSpot sync)

Based on actual synced data:

| participant_type | related_local_id | related_email | related_name | relationship_role |
|------------------|------------------|---------------|--------------|-------------------|
| `contact` | contact UUID | alice@acme.com | Alice Acme | null |
| `contact` | contact UUID | bob@acme.com | Bob Smith | decision_maker |
| `user` | user UUID | owner@internal.com | Deal Owner | null |
| `company` | company UUID | null | Acme Corp | null |

**Key observations:**
- `CONTACT` has email and name → becomes stakeholder
- `USER` has email → used for matching (not stakeholder)
- `COMPANY` has no email → used for domain extraction
- `relationship_role` may be null for unlabeled associations

---

## HubSpot Data Mapping

### How Participants are Fetched

HubSpot deals have associated contacts via the Associations API:

```
GET /crm/v3/objects/deals/{dealId}/associations/contacts
GET /crm/v3/objects/deals/{dealId}/associations/companies
```

### Field Mapping

| HubSpot Field | CRMDealParticipant Field |
|---------------|--------------------------|
| `associatedObjectId` | `crm_source_object_mappings.external_id`, resolved to `related_local_id` |
| Contact's `email` | `related_email` |
| Contact's `firstname + lastname` | `related_name` |
| Association label | `relationship_role` |
| `isPrimary` association | `is_primary` |

### Association Roles (relationship_role)

HubSpot supports labeled associations that map to `relationship_role`:

| HubSpot Association Label | relationship_role Value |
|---------------------------|-------------------------|
| "Primary Contact" | `primary_contact` |
| "Decision Maker" | `decision_maker` |
| "Billing Contact" | `billing_contact` |
| "Technical Contact" | `technical_contact` |
| (unlabeled) | `null` |

---

## Sync Flow

```
HubSpot API → Nango Proxy → CRM Deal Sync Task
                                    ↓
                        CRMDealParticipant table (normalized)
                                    ↓
                        Stakeholder Sync Task (Task 1)
                                    ↓
                        DealStakeholder table (CONTACT type only)
                                    ↓
                        ┌───────────┴───────────┐
                        ↓                       ↓
                Calendar Match (Task 2)    Email Match (Task 3)
```

### Step 1: Deal Sync (CRM Sync Service)

```python
# Fetches deals with associations from HubSpot
records = await fetcher(
    nango=nango,
    connection_id=integration.connection_id,
    ...
)

# Syncs to CRMDeal and CRMDealParticipant tables
metrics = await deal_service.sync(
    integration=integration,
    records=records,
    provider="hubspot",
)
```

### Step 2: Stakeholder Sync (Stakeholder Task)

```python
# Only processes CONTACT type participants
for participant in deal.participants:
    if participant.participant_type != CRMParticipantType.CONTACT:
        continue  # Skip USER and COMPANY types

    if not participant.related_email:
        continue  # Skip contacts without email

    # Creates DealStakeholder from participant
    stakeholder = DealStakeholder(
        email=participant.related_email,
        name=participant.related_name,
        source=StakeholderSource.CRM,
        source_reference_id=participant.participant_id,
        integration_id=deal.integration_id,  # Track which CRM
        ...
    )
```

---

## Important Notes

### 1. Primary Contact Email (NOT USED)

The `CRMDeal.primary_contact_email` field exists but is **NOT used** for stakeholder sync.

**Why?**
- All contact data comes from `CRMDealParticipant` table
- Primary contact is identified by `is_primary=True` in participants
- `primary_contact_email` is reserved for future use

### 2. Integration Tracking

Stakeholders track their CRM source via `integration_id`:
- Links back to the specific HubSpot/Salesforce integration
- Allows filtering stakeholders by CRM instance
- Enables multi-CRM support per organization

### 3. Provider-Agnostic Sync

The `StakeholderSyncService` does NOT have HubSpot or Salesforce specific code:
- Reads from normalized `CRMDealParticipant` table
- Same logic works for any CRM provider
- Provider differences are handled at the CRM sync layer

### 4. Soft Deletes

When a contact is removed from a deal in HubSpot:
- `CRMDealParticipant.is_deleted = True`
- `DealStakeholder.is_deleted = True`
- Historical associations (calendar, email) are preserved

---

## Example Data

### CRMDealParticipant Record (type=CONTACT)

```json
{
  "participant_id": "550e8400-e29b-41d4-a716-446655440000",
  "organisation_id": "org-uuid",
  "integration_id": "hubspot-integration-uuid",
  "deal_id": "deal-uuid",
  "participant_type": "contact",
  "related_local_id": "contact-uuid",
  "related_email": "alice@acme.com",
  "related_name": "Alice Johnson",
  "relationship_role": "decision_maker",
  "is_primary": true,
  "is_deleted": false,
  "synced_at": "2026-01-14T10:00:00Z"
}
```

### Resulting DealStakeholder

```json
{
  "stakeholder_id": "uuid",
  "organisation_id": "org-uuid",
  "integration_id": "hubspot-integration-uuid",
  "deal_id": "deal-uuid",
  "email": "alice@acme.com",
  "name": "Alice Johnson",
  "source": "CRM",
  "source_reference_id": "550e8400-e29b-41d4-a716-446655440000",
  "job_title": "decision_maker",
  "is_deleted": false
}
```

---

## Related Documents

- [Stakeholder Change Detection](./stakeholder-change-detection.md)
- [Deal Meeting Automation](./deal-meeting-automation.md)
- [Deal Email Matching](./deal-email-matching-automation.md)
