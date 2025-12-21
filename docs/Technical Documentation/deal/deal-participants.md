# Deal Participants

## Overview

Deal participants represent all people and organizations involved in a deal. This includes contacts, companies, and internal team members (users/owners). The system syncs participants from HubSpot and enriches them with additional data from related CRM entities.

## Participant Types

| Type | Description | Source |
| --- | --- | --- |
| `CONTACT` | External people involved in the deal | HubSpot deal-to-contact associations |
| `COMPANY` | Organizations involved in the deal | HubSpot deal-to-company associations |
| `USER` | Internal team members (owners, SEs, collaborators) | HubSpot owner properties |

## Data Model

### CRMDealParticipant Table

```text
crm_deal_participants
├── participant_id (UUID, PK)
├── deal_id (UUID, FK → crm_deals)
├── organisation_id (UUID)
├── external_id (String) - Unique identifier for the participant record
├── participant_type (Enum) - CONTACT, COMPANY, or USER
├── related_external_id (String) - HubSpot ID of the related entity
├── related_local_id (UUID) - Local ID from CRMContact/CRMCompany/CRMUser
├── related_name (String) - Enriched name
├── related_email (String) - Enriched email (or domain for companies)
├── relationship_role (String) - Role/association type
└── raw (JSONB) - Original HubSpot data
```

### Unique Constraint

```sql
UNIQUE (deal_id, participant_type, related_external_id, relationship_role)
```

This ensures no duplicate participants for the same deal with the same type, entity, and role.

## HubSpot Data Sources

### 1. Contact Participants

**Source:** Deal associations → contacts

```json
{
  "associations": {
    "contacts": {
      "results": [
        {"id": "123456", "type": "deal_to_contact"},
        {"id": "789012", "type": "deal_to_contact_unlabeled"}
      ]
    }
  }
}
```

**Roles:**

- `deal_to_contact` - Primary contact association
- `deal_to_contact_unlabeled` - Secondary/unlabeled association
- Custom labels (e.g., `decision_maker`, `technical_contact`)

### 2. Company Participants

**Source:** Deal associations → companies

```json
{
  "associations": {
    "companies": {
      "results": [
        {"id": "456789", "type": "deal_to_company"},
        {"id": "321654", "type": "deal_to_company_unlabeled"}
      ]
    }
  }
}
```

**Roles:**

- `deal_to_company` - Primary company association
- `deal_to_company_unlabeled` - Secondary/unlabeled association

### 3. User Participants (Team Members)

**Source:** Deal properties

| HubSpot Property | Role | Description |
| --- | --- | --- |
| `hubspot_owner_id` | `owner` | Primary deal owner/sales rep |
| `hs_all_collaborator_owner_ids` | `collaborator` | Deal collaborators (semicolon-delimited) |
| `hs_all_deal_split_owner_ids` | `split_owner` | Revenue split owners (Enterprise, semicolon-delimited) |

**Example deal properties:**

```json
{
  "hubspot_owner_id": "83275159",
  "hs_all_collaborator_owner_ids": "12345;67890",
  "hs_all_deal_split_owner_ids": null
}
```

## Sync Flow

```text
┌─────────────────────┐
│   HubSpot API       │
│   (Deal + Assoc)    │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│  DealSyncService    │
│  - Extract deals    │
│  - Extract participants
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ _extract_participants│
│ - Contacts          │
│ - Companies         │
│ - Users (owners/SE) │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ _upsert_participants│
│ - Lookup by constraint
│ - Create or update  │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│DealEnrichmentService│
│ enrich_participants()│
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ Enriched Participants│
│ - Names from CRM    │
│ - Emails/domains    │
│ - Local IDs         │
└─────────────────────┘
```

## Enrichment

After sync, participants are enriched with data from related CRM tables:

### Contact Enrichment

- **Source:** `crm_contacts` table
- **Fields:** `related_name` ← `full_name`, `related_email` ← `email`, `related_local_id` ← `contact_id`

### Company Enrichment

- **Source:** `crm_companies` table
- **Fields:** `related_name` ← `name`, `related_email` ← `domain`, `related_local_id` ← `company_id`

### User Enrichment

- **Source:** `crm_users` table
- **Fields:** `related_name` ← `full_name`, `related_email` ← `email`, `related_local_id` ← `user_id`

## Code References

### Key Files

| File | Purpose |
| --- | --- |
| `src/service/integrations/crm/entities/deal_sync_service.py` | Main sync logic, participant extraction |
| `src/service/integrations/crm/entities/deal_enrichment_service.py` | Participant enrichment |
| `src/workers/integrations_worker/providers/crm/hubspot/fetchers/deal_fetcher.py` | HubSpot API fetcher |
| `src/domain/models/organisation_tables/sync/crm/deals.py` | Data models |

### Key Methods

```python
# deal_sync_service.py
_extract_participants(deal_record, associations)  # Main extraction
_extract_contact_participants(deal_record, contacts)  # Contact extraction
_extract_company_participants(deal_record, companies)  # Company extraction
_extract_hubspot_owner_participants(deal_record)  # User/SE extraction
_upsert_participants(participants)  # Database upsert

# deal_enrichment_service.py
enrich_participants(integration_id)  # Enrich all participants
```

## Configuring SEs in HubSpot

### Option 1: Use Collaborators (Recommended)

1. **Enable Collaborators:**
   - Settings → Objects → Deals
   - Check "Allow users to add collaborators"

2. **Add Collaborators to Deals:**
   - Open a deal record
   - Find the Collaborators card in the sidebar
   - Click Add → Select team members (SEs, managers, etc.)

3. **Result:** Synced as USER participants with role `collaborator`

### Option 2: Deal Splits (Enterprise Only)

1. **Enable Deal Splits:**
   - Settings → Objects → Deals
   - Check "Split Deals among users"

2. **Add Split:**
   - Open deal record
   - Deal Splits card → Add
   - Select users and percentages

3. **Result:** Synced as USER participants with role `split_owner`

## Querying Participants

### Get All Participants for a Deal

```sql
SELECT 
    p.participant_type,
    p.relationship_role,
    p.related_name,
    p.related_email,
    p.related_external_id
FROM crm_deal_participants p
JOIN crm_deals d ON p.deal_id = d.deal_id
WHERE d.name = 'My Deal'
ORDER BY p.participant_type, p.relationship_role;
```

### Get All Team Members (Users) Across Deals

```sql
SELECT 
    d.name as deal_name,
    p.relationship_role,
    p.related_name,
    p.related_email
FROM crm_deal_participants p
JOIN crm_deals d ON p.deal_id = d.deal_id
WHERE p.participant_type = 'USER'
ORDER BY d.name, p.relationship_role;
```

### Get Deals by Contact

```sql
SELECT 
    d.name as deal_name,
    d.amount,
    p.relationship_role
FROM crm_deal_participants p
JOIN crm_deals d ON p.deal_id = d.deal_id
WHERE p.participant_type = 'CONTACT'
  AND p.related_email = 'contact@example.com';
```

## Troubleshooting

### Participants Not Syncing

1. **Check HubSpot associations exist:**
   - Open deal in HubSpot
   - Verify contacts/companies are associated

2. **Check sync logs:**

   ```bash
   grep "participants" logs/integrations.log
   ```

3. **Verify deal has associations in API response:**
   - Look for `associations` key in deal data
   - Should contain `contacts` and `companies` with `results` arrays

### User Participants Missing

1. **Check HubSpot properties:**
   - `hubspot_owner_id` should have a value
   - `hs_all_collaborator_owner_ids` for collaborators

2. **Verify collaborators are enabled:**
   - Settings → Objects → Deals → "Allow users to add collaborators"

3. **Verify collaborators are added to deals:**
   - Open deal in HubSpot
   - Check the Collaborators card in the sidebar

### Enrichment Not Working

1. **Check related entity exists:**

   ```sql
   SELECT * FROM crm_contacts WHERE external_id = '<contact_external_id>';
   SELECT * FROM crm_companies WHERE external_id = '<company_external_id>';
   SELECT * FROM crm_users WHERE external_id = '<user_external_id>';
   ```

2. **Run enrichment manually:**

   ```python
   await deal_enrichment_service.enrich_participants(integration_id)
   ```

### Duplicate Key Errors

The system uses a unique constraint on `(deal_id, participant_type, related_external_id, relationship_role)`. If you see duplicate key violations:

1. Check for duplicate associations in HubSpot
2. Verify deduplication logic for unlabeled associations
3. Check the `relationship_role` is being set correctly

## Best Practices

1. **Always run enrichment after sync** to populate names and emails
2. **Use collaborators over custom properties** for better HubSpot integration
3. **Sync users before deals** to ensure enrichment data is available
4. **Monitor for orphaned participants** when deals are deleted
