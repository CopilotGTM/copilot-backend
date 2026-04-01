# Stakeholder-Contact Linking & Domain Matching Fix

**Date:** 2026-03-31
**Status:** Draft

## Problem Statement

When a stakeholder is added manually (via UI or AI agent), three issues prevent proper deal-meeting and deal-email matching:

1. **No CRMContact is created** — the stakeholder exists in `DealStakeholder` but has no corresponding `CRMContact` record. This means the person has no cross-deal identity, no company linkage, and no presence in the contacts table.

2. **No CRMDealParticipant is created** — the matching pipelines rely on `CRMDealParticipant` records of type CONTACT to build customer domain lists. Without a participant record, the stakeholder's domain is invisible to matching.

3. **`_collect_customer_domains()` only reads CRMDealParticipant** — both `deal_meeting_matcher.py` and `deal_email_matcher.py` collect customer domains exclusively from CRM participants, ignoring existing approved `DealStakeholder` records. Even when a stakeholder exists with domain X, new meeting attendees from domain X are not discovered.

Additionally, there is no handling for **existing stakeholders** that were created before this fix — they have `contact_id=None` and no corresponding `CRMContact`.

## Data Model Context

```
CRMCompany ←── CRMContact ←── CRMDealParticipant ──→ CRMDeal
                    ↑                                      ↑
                contact_id                              deal_id
                    │                                      │
               DealStakeholder ────────────────────────────┘
```

**Stakeholder sources and their contact-creation behavior:**

| Source | Creates CRMContact? | Creates CRMDealParticipant? |
|--------|--------------------|-----------------------------|
| **MANUAL** (user via UI) | Yes — find or create | Yes, if deal is internal CRM |
| **CRM** (HubSpot/Salesforce) | Already exists (CRM sync creates it) | Already exists |
| **CALENDAR_DISCOVERY** | No — link if exists | No |
| **EMAIL_DISCOVERY** | No — link if exists | No |
| **AI agent** (create_stakeholder_if_missing) | No — link if exists | No |

**Why:** Only MANUAL stakeholders have high confidence (user explicitly added). Discovery and AI sources are lower confidence and should not pollute the contacts table. They benefit from linking to existing contacts via email lookup, but should not create new ones.

## Changes

### Change 1: Create CRMContact when MANUAL stakeholder is added

**File:** `src/service/crm/insights/stakeholders_service.py`
**Method:** `create_stakeholder()`

After creating the `DealStakeholder`, add a step to ensure a `CRMContact` exists:

1. Query `CRMContact` by `email` + `organisation_id`
2. **If found:** set `stakeholder.contact_id = contact.contact_id`
3. **If not found:**
   - Get the internal CRM integration via `InternalCRMSetupService.get_internal_integration()`
   - If no internal integration exists, skip contact creation (just create the stakeholder as today)
   - Create a new `CRMContact` under the internal integration with available data:
     - `email`, `full_name` (from `request.name`), `job_title`, `phone` (from `request.phone_number`)
     - `external_id`: generate a deterministic ID (e.g., `uuid5` from email)
     - `company_name`: leave null (no auto company matching for now)
   - Set `stakeholder.contact_id = new_contact.contact_id`

**Dependencies:**
- `StakeholdersService.__init__` currently takes `(session, organisation_id)` — no `user_id`. The `InternalCRMSetupService` requires `(session, organisation_id, user_id)`. Fix: add optional `user_id` parameter to `StakeholdersService.__init__`. The handler dependency at `stakeholders_handler.py:44-49` already has `user_ids` available — pass `user_ids.user_id` through.
- `InternalCRMSetupService` uses the same org session, so no additional session is needed.
- The `_collect_customer_domains()` change in the matchers requires the method signature to become async (needs a DB query for stakeholders). Both `deal_meeting_matcher.py` and `deal_email_matcher.py` need their callers updated.

### Change 2: Create CRMDealParticipant for internal CRM deals

**File:** `src/service/crm/insights/stakeholders_service.py`
**Method:** `create_stakeholder()` (same method, after contact creation)

After ensuring the CRMContact exists:

1. Load the deal to get its `integration_id`
2. Get the internal CRM `integration_id`
3. If `deal.integration_id == internal_integration_id`:
   - Check if a `CRMDealParticipant` already exists for this deal + contact
   - If not, create one with `participant_type=CONTACT`, `related_email=email`
4. If the deal belongs to an external CRM: skip participant creation

### Change 3: Soft-delete CRMDealParticipant on stakeholder removal

**File:** `src/service/crm/insights/stakeholders_service.py`
**Method:** `remove_stakeholder()`

After marking the stakeholder as `REMOVED`:

1. If the stakeholder has a `source_reference_id` pointing to a `CRMDealParticipant`, soft-delete it
2. Alternatively, look up the participant by `deal_id + email` and soft-delete if it belongs to the internal CRM integration (don't touch external CRM participants)

### Change 4: Fix `_collect_customer_domains()` in both matchers

**File:** `src/workers/deal_worker/services/deal_mapper_services/deal_meeting_matcher.py`
**Method:** `_collect_customer_domains()`

Add a query for approved `DealStakeholder` records on the deal. Extract email domains and add to the customer domains set. This ensures manually-added stakeholders (and discovered ones) contribute to domain matching regardless of whether they have a CRM participant record.

```python
# Existing: from CRM participants
# New: also from approved stakeholders
stakeholder_stmt = select(col(DealStakeholder.email)).where(
    col(DealStakeholder.deal_id) == deal.deal_id,
    col(DealStakeholder.is_deleted).is_(False),
    col(DealStakeholder.approval_status) == ApprovalStatus.APPROVED,
)
```

**File:** `src/workers/deal_worker/services/deal_mapper_services/deal_email_matcher.py`
**Method:** `_collect_company_domains()`

Same change — also collect domains from approved `DealStakeholder` records.

**Note:** This method currently only takes `deal: CRMDeal` as a parameter (no session). Since querying `DealStakeholder` requires a DB query, the signature needs to change to `async def _collect_customer_domains(self, session, deal)`. Callers must be updated.

### Change 5: Link existing stakeholders to contacts (backfill)

**File:** New script in `scripts/maintenance/backfill_stakeholder_contacts.py`

A one-time maintenance script to fix existing stakeholders that have `contact_id=None` and `source=MANUAL`:

1. Query all `DealStakeholder` records where `contact_id IS NULL` and `source = 'MANUAL'`
2. For each stakeholder:
   - Look up `CRMContact` by `email` + `organisation_id`
   - **If found:** set `stakeholder.contact_id = contact.contact_id`
   - **If not found:** create a `CRMContact` under the org's internal CRM integration
3. For discovery/AI stakeholders (`CALENDAR_DISCOVERY`, `EMAIL_DISCOVERY`) with `contact_id IS NULL`:
   - Look up `CRMContact` by email — link if found, skip if not
4. Process in batches per org, with fresh sessions per batch

### Change 6: Link contacts in discovery paths (opportunistic)

**File:** `src/workers/deal_worker/services/deal_mapper_services/deal_meeting_matcher.py`
**Method:** `_discover_stakeholders_from_event()`

When creating a `CALENDAR_DISCOVERY` stakeholder, also look up `CRMContact` by email and set `contact_id` if found. This is a one-line addition after stakeholder creation.

**File:** `src/workers/deal_worker/services/deal_mapper_services/deal_email_matcher.py`
**Method:** `_discover_stakeholders_from_email()`

Same — link to existing `CRMContact` if one exists for the email.

### Change 7: Add `created_by_user_id` to DealStakeholder

**File:** `src/domain/models/organisation_tables/sync/crm/stakeholders.py`

Add a `created_by_user_id: UUID | None` column to track who added the stakeholder:
- **MANUAL** source → set to the actual `user_id` from the handler
- **CRM/CALENDAR_DISCOVERY/EMAIL_DISCOVERY** → set to `AUTOMATED_PIPELINE_USER_ID`
- **Legacy records** → remain `NULL` (no backfill needed — `created_at` timestamp already exists)

Requires an Alembic org migration to add the column.

## What is NOT in scope

- **Auto-linking stakeholders to CRMCompany by domain** — deferred to a future enhancement. The contact is created without company linkage.
- **Creating CRMContact for discovery/AI sources** — only MANUAL sources create contacts. Others link if a contact already exists.
- **Modifying external CRM data** — we never create or modify `CRMDealParticipant` records that belong to external CRM integrations (HubSpot, Salesforce).
- **Full audit trail (who rejected/removed)** — deferred. For now, `approval_status` + `updated_at` + timeline events cover the removal/rejection case. A `last_modified_by_user_id` or audit log can be added later if needed.

## Affected Files Summary

| File | Change |
|------|--------|
| `src/service/crm/insights/stakeholders_service.py` | Create CRMContact + CRMDealParticipant on manual add; soft-delete participant on removal |
| `src/workers/deal_worker/services/deal_mapper_services/deal_meeting_matcher.py` | `_collect_customer_domains()` reads from DealStakeholder; link contact_id in discovery |
| `src/workers/deal_worker/services/deal_mapper_services/deal_email_matcher.py` | `_collect_company_domains()` reads from DealStakeholder; link contact_id in discovery |
| `scripts/maintenance/backfill_stakeholder_contacts.py` | New script — backfill existing stakeholders |
| `src/agents/tools/crm/create_stakeholder_if_missing.py` | Link contact_id to existing CRMContact if found (no contact creation) |

## Testing Strategy

1. **Unit tests for `create_stakeholder()`** — verify CRMContact is created, contact_id is set, CRMDealParticipant is created for internal CRM deals, skipped for external CRM deals
2. **Unit tests for `remove_stakeholder()`** — verify CRMDealParticipant is soft-deleted for internal CRM deals
3. **Unit tests for `_collect_customer_domains()`** — verify domains are collected from both CRM participants and DealStakeholder records
4. **Integration test** — add a manual stakeholder, run calendar matching, verify new attendees from the same domain are discovered
5. **Backfill script** — run against a test org, verify contact_id is set for existing stakeholders
