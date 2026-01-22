# Create Deal - Comprehensive Field Specification

> Date: 2026-01-22
> Status: Production-Grade Specification
> References: HubSpot, Salesforce, Pipedrive, Backend CRMDeal Model

---

## Overview

This document provides a comprehensive, production-grade specification for all fields available when creating a deal in the Internal CRM. Fields are categorized by:

1. **Core Fields** - Direct columns in `crm_deals` table
2. **Additional Fields** - Can be stored in `properties` JSONB column
3. **Dependent Data** - Requires additional API endpoints

---

## Research Summary

Based on research from major CRM platforms:

| CRM | Key Required Fields | Key Optional Fields |
|-----|---------------------|---------------------|
| [HubSpot](https://knowledge.hubspot.com/properties/hubspots-default-deal-properties) | Deal Name, Pipeline, Stage | Amount, Close Date, Owner, Deal Type, Lead Source, Priority |
| [Salesforce](https://developer.salesforce.com/docs/atlas.en-us.object_reference.meta/object_reference/sforce_api_objects_opportunity.htm) | Name, Stage, Close Date | Amount, Probability, Account, Type, Lead Source, Forecast Category |
| [Pipedrive](https://support.pipedrive.com/en/article/data-fields-in-pipedrive) | Title, Pipeline | Value, Currency, Expected Close Date, Owner, Custom Fields |

**Key Insight:** Only `name` is truly required. All CRMs allow deal creation without account/contact first.

---

## 1. Core Fields (Database Columns)

These fields map directly to `crm_deals` table columns.

### 1.1 Required Fields

| Field | DB Column | Type | Validation | UI Element |
|-------|-----------|------|------------|------------|
| **Deal Name** | `name` | string | 1-500 chars, cannot be empty | Text input |

### 1.2 Pipeline & Stage Fields

| Field | DB Column | Type | Validation | UI Element | Notes |
|-------|-----------|------|------------|------------|-------|
| Pipeline | `pipeline_id` | UUID | FK to crm_pipelines | Dropdown (read-only for MVP) | Defaults to default pipeline |
| Stage | `stage_id` | UUID | Must belong to pipeline | Dropdown | Shows all stages with probability % |

### 1.3 Financial Fields

| Field | DB Column | Type | Validation | UI Element | Endpoint Required |
|-------|-----------|------|------------|------------|-------------------|
| Amount | `amount` | numeric(18,2) | >= 0 | Number input | No |
| Currency | `currency` | string(3) | ISO 4217 code | Dropdown | **Yes - GET /users/crm/settings/currencies** |
| Probability | `probability` | float | 0-100 | Number input or slider | No (usually auto-set by stage) |
| Predicted Amount | `predicted_amount` | numeric(18,2) | Auto-calculated | Display only | No (amount * probability) |

### 1.4 Date Fields

| Field | DB Column | Type | Validation | UI Element |
|-------|-----------|------|------------|------------|
| Close Date | `close_date` | datetime | Valid date | Date picker |
| Expected Close Date | `expected_close_date` | datetime | Valid date | Date picker |

### 1.5 Classification Fields

| Field | DB Column | Type | Validation | UI Element | Suggested Values |
|-------|-----------|------|------------|------------|------------------|
| Deal Type | `deal_type` | string(128) | Optional | Dropdown | New Business, Renewal, Expansion, Upsell, Cross-sell |
| Priority | `priority` | string(64) | Optional | Dropdown | High, Medium, Low, Critical |
| Lead Source | `lead_source` | string(128) | Optional | Dropdown | Inbound, Outbound, Referral, Partner, Event, Website, Cold Call, LinkedIn |
| Forecast Category | `forecast_category` | string(64) | Optional | Dropdown | Pipeline, Best Case, Commit, Closed |

### 1.6 Ownership Fields

| Field | DB Column | Type | Validation | UI Element | Endpoint Required |
|-------|-----------|------|------------|------------|-------------------|
| Owner | `owner_id` | UUID | FK to crm_users | User search dropdown | **Yes - GET /users/crm/users** |

**Owner Selection API:** `GET /users/crm/users?q={search}&limit=50`

Returns:
```json
{
  "items": [
    {
      "user_id": "uuid",
      "name": "John Doe",
      "email": "john@company.com",
      "role": "sales_rep",
      "job_title": "Account Executive",
      "organisation_role": "Sales",
      "status": "active"
    }
  ],
  "cursor": "next_page_cursor"
}
```

### 1.7 Association Fields (Display Only on Create)

| Field | DB Column | Type | Notes |
|-------|-----------|------|-------|
| Company Name | `company_name` | string(255) | Set via participants later |
| Company Domain | `company_domain` | string(255) | Set via participants later |
| Primary Contact Name | `primary_contact_name` | string(255) | Set via participants later |
| Primary Contact Email | `primary_contact_email` | string(255) | Set via participants later |

**Note:** Account/Contact association is done via Deal Participants (Step 5), not during initial deal creation.

### 1.8 Description Fields

| Field | DB Column | Type | Validation | UI Element |
|-------|-----------|------|------------|------------|
| Description | `description` | text | Optional | Textarea (multi-line) |
| Closed Reason | `closed_reason` | string(255) | Only when closing | Text input (on close modal) |

---

## 2. Additional Fields (Properties JSONB)

These fields can be stored in the `properties` JSONB column for extensibility.

### 2.1 Industry & Vertical

| Field | Type | Suggested Values |
|-------|------|------------------|
| Industry | string | Technology, Healthcare, Finance, Retail, Manufacturing, Education, Government, etc. |
| Vertical | string | Enterprise, SMB, Mid-Market, Startup |
| Segment | string | Strategic, Named, Territory |

### 2.2 Competition & Win/Loss

| Field | Type | Suggested Values |
|-------|------|------------------|
| Competitors | array[string] | List of competitor names |
| Competitive Status | string | No Competition, Limited Competition, Heavy Competition |
| Win/Loss Reason | string | Price, Features, Relationship, Timing, Competition |

### 2.3 Product & Licensing

| Field | Type | Notes |
|-------|------|-------|
| Products | array[string] | Products/SKUs in the deal |
| License Type | string | Perpetual, Subscription, Usage-based |
| Contract Length | integer | Months |
| Payment Terms | string | Net 30, Net 60, Annual, Quarterly |

### 2.4 Tags & Custom

| Field | Type | Notes |
|-------|------|-------|
| Tags | array[string] | User-defined tags |
| Custom Fields | object | Key-value pairs for org-specific fields |

### 2.5 Source Attribution

| Field | Type | Notes |
|-------|------|-------|
| Campaign | string | Marketing campaign name |
| UTM Source | string | Marketing source tracking |
| Referrer | string | Who referred the deal |

---

## 3. Endpoints Required

### 3.1 Existing Endpoints

| Endpoint | Purpose | Status |
|----------|---------|--------|
| `GET /users/crm/pipelines?include_stages=true` | Load pipelines and stages | ✅ Implemented |
| `GET /users/crm/users?q={search}` | Search app users for owner selection | ✅ Implemented |
| `POST /users/crm/deals` | Create deal | ✅ Implemented |
| `GET /users/crm/settings/currencies` | List available currencies (USD, INR) | ✅ Implemented |
| `GET /users/crm/settings/deal-options` | All dropdown options in single call | ✅ Implemented |

### 3.2 Settings API Details

**GET /users/crm/settings/currencies**

Returns available currencies for deal amount selection.

```json
{
  "status": "SUCCESS",
  "data": {
    "items": [
      { "code": "USD", "name": "US Dollar", "symbol": "$" },
      { "code": "INR", "name": "Indian Rupee", "symbol": "₹" }
    ],
    "default_code": "USD"
  }
}
```

**GET /users/crm/settings/deal-options**

Returns all dropdown options in a single API call (currencies, deal types, priorities, lead sources, forecast categories).

See `deal-workflow-specification.md` for full response schema.

---

## 4. Create Deal Form Specification

### 4.1 MVP Form (Immediate)

| Field | Required | UI Element | Default |
|-------|----------|------------|---------|
| Deal Name | ✅ Yes | Text input | - |
| Pipeline | No | Read-only | Default pipeline |
| Stage | No | Dropdown | First stage |
| Close Date | No | Date picker | - |
| Amount | No | Number input | - |
| Description | No | Textarea | - |

### 4.2 Phase 2 Form (Production)

Add these fields:

| Field | UI Element | Notes |
|-------|------------|-------|
| Owner | User search dropdown | Uses `GET /users/crm/users` |
| Currency | Dropdown | Uses currencies endpoint or hardcoded list |
| Deal Type | Dropdown | Configurable options |
| Priority | Dropdown | High/Medium/Low |
| Lead Source | Dropdown | Configurable options |
| Expected Close Date | Date picker | For forecasting |

### 4.3 Phase 3 Form (Full)

Add these fields:

| Field | UI Element | Notes |
|-------|------------|-------|
| Probability | Slider or input | Override stage default |
| Forecast Category | Dropdown | Pipeline/Best Case/Commit/Closed |
| Tags | Tag input | Multi-select |
| Industry | Dropdown | Stored in properties |
| Products | Multi-select | Stored in properties |

---

## 5. Validation Rules

### 5.1 Field-Level Validation

| Field | Rule |
|-------|------|
| Deal Name | Required, 1-500 chars, trimmed, not blank |
| Amount | Optional, must be >= 0 |
| Currency | Optional, must be 3 uppercase letters (ISO 4217) |
| Probability | Optional, must be 0-100 |
| Close Date | Optional, must be valid date |
| Stage | Must belong to selected pipeline |
| Owner | Must be active user in organization |

### 5.2 Business Rules

| Rule | Description |
|------|-------------|
| Stage-Pipeline | Selected stage must belong to selected pipeline |
| Owner Uniqueness | When assigning team roles, owner cannot also be SE (future) |
| Amount with Currency | If amount is set, currency should be set |
| Closed Reason | Required when moving to won/lost stage (future) |

---

## 6. Default Values

| Field | Default Value | Source |
|-------|---------------|--------|
| Pipeline | Default pipeline (is_default=true) | Backend |
| Stage | First stage (display_order=1) | Backend |
| Currency | Organization default or "USD" | Settings/Hardcoded |
| Deal Status | "open" | Backend |
| Probability | Stage default probability | Backend |

---

## 7. UI/UX Recommendations

### 7.1 Form Layout

```
┌─────────────────────────────────────────────────────┐
│                    Create Deal                       │
├─────────────────────────────────────────────────────┤
│ BASIC INFORMATION                                    │
│ ┌─────────────────────────────────────────────────┐ │
│ │ Deal Name *                    [________________]│ │
│ │ Description                    [________________]│ │
│ │                                [________________]│ │
│ └─────────────────────────────────────────────────┘ │
│                                                      │
│ PIPELINE & STAGE                                     │
│ ┌─────────────────────────────────────────────────┐ │
│ │ Pipeline          [Default Pipeline        ▼]  │ │
│ │ Stage             [Qualification (10%)     ▼]  │ │
│ └─────────────────────────────────────────────────┘ │
│                                                      │
│ FINANCIAL                                            │
│ ┌─────────────────────────────────────────────────┐ │
│ │ Amount            [________]  Currency [USD ▼] │ │
│ │ Close Date        [📅 Select date           ]  │ │
│ └─────────────────────────────────────────────────┘ │
│                                                      │
│ OWNERSHIP (Phase 2)                                  │
│ ┌─────────────────────────────────────────────────┐ │
│ │ Owner             [🔍 Search users...      ▼]  │ │
│ └─────────────────────────────────────────────────┘ │
│                                                      │
│ CLASSIFICATION (Phase 2)                             │
│ ┌─────────────────────────────────────────────────┐ │
│ │ Deal Type         [New Business            ▼]  │ │
│ │ Priority          [Medium                  ▼]  │ │
│ │ Lead Source       [Select...               ▼]  │ │
│ └─────────────────────────────────────────────────┘ │
│                                                      │
│                    [Cancel]  [Create Deal]           │
└─────────────────────────────────────────────────────┘
```

### 7.2 Progressive Disclosure

- Show basic fields first
- Expand "More Options" for classification fields
- Use collapsible sections for advanced fields

### 7.3 Loading States

- Pipeline dropdown: "Loading pipelines..."
- User search: Debounce 300ms, show "Searching..."
- Submit: "Creating deal..." with disabled button

---

## 8. API Request Format

### 8.1 MVP Request

```json
{
  "name": "Acme Corp - Enterprise License",
  "stage_id": "uuid-of-stage",
  "close_date": "2026-03-15T00:00:00Z",
  "amount": 50000,
  "description": "Enterprise deal for 100 seats"
}
```

### 8.2 Full Request (Phase 2+)

```json
{
  "name": "Acme Corp - Enterprise License",
  "pipeline_id": "uuid-of-pipeline",
  "stage_id": "uuid-of-stage",
  "close_date": "2026-03-15T00:00:00Z",
  "expected_close_date": "2026-03-31T00:00:00Z",
  "amount": 50000,
  "currency": "USD",
  "owner_user_id": "uuid-of-owner",
  "deal_type": "New Business",
  "priority": "High",
  "lead_source": "Inbound",
  "description": "Enterprise deal for 100 seats"
}
```

---

## 9. Implementation Checklist

### Phase 1 (MVP) - ✅ Complete (2026-01-22)

- [x] Deal Name field
- [x] Pipeline (read-only)
- [x] Stage dropdown (with probability %)
- [x] Close Date picker
- [x] Amount input
- [x] Description textarea
- [x] API integration (POST /users/crm/deals)
- [x] Success/error handling
- [x] Deal list refresh on create
- [x] Theme integration (dark/light mode support)
- [x] Loading state ("Creating...")

**Frontend Files (Phase 1):**

| File | Purpose |
|------|---------|
| `src/modules/deals/hooks/use-create-deal.ts` | `useCreateDeal()` mutation hook (created) |
| `src/modules/deals/types/deals.types.ts` | `CreateDealRequest`, `CreateDealResponse` types (updated) |
| `src/modules/deals/api/deals.api.ts` | `createDeal()` API function (updated) |
| `src/app/(root)/deals/components/create-deal-modal.tsx` | Modal with full API integration (updated) |
| `src/modules/deals/api/index.ts` | Export `createDeal` (updated) |
| `src/modules/deals/hooks/index.ts` | Export `useCreateDeal` (updated) |
| `src/modules/deals/types/index.ts` | Export deal create types (updated) |

**Key Implementation Notes:**

- Uses `crmApiClient` for API calls (base: `/users/crm`)
- Probabilities displayed as-is (not multiplied by 100)
- Query invalidation: `["crm_deals"]` on success
- Theme tokens: `bg.surface`, `fg.primary`, `border.default`, `modal.padding`, etc.

### Phase 2: Ownership & Classification (✅ Complete - 2026-01-22)

**Goal:** Add owner assignment and deal classification fields.

| Field | UI Element | Endpoint Required | Priority |
|-------|------------|-------------------|----------|
| Owner | User search dropdown | `GET /users/crm/users?q=` | High |
| Currency | Dropdown | None (static list) | Medium |
| Deal Type | Dropdown | None (static/configurable) | Medium |
| Priority | Dropdown | None (static: High/Medium/Low) | Medium |
| Lead Source | Dropdown | None (static/configurable) | Medium |
| Expected Close Date | Date picker | None | Low |

**Checklist:**
- [x] Owner selection with user search
- [x] Currency dropdown (10 currencies: USD, EUR, GBP, CAD, AUD, JPY, INR, CHF, CNY, BRL)
- [x] Deal Type dropdown (New Business, Renewal, Expansion, Upsell)
- [x] Priority dropdown (High, Medium, Low)
- [x] Lead Source dropdown (Inbound, Outbound, Referral, Partner, Event, Website, Other)
- [x] Expected Close Date picker

**Frontend Files (Phase 2):**

| File | Purpose |
|------|---------|
| `src/modules/deals/hooks/use-users.ts` | `useUserSearch()` hook for owner selection (created) |
| `src/modules/deals/api/users.api.ts` | `searchUsers()` API function (created) |
| `src/modules/deals/types/users.types.ts` | `SearchUsersParams`, `CRMUserItem` types (created) |
| `src/modules/deals/types/deals.types.ts` | Added Phase 2 fields to `CreateDealRequest` (updated) |
| `src/app/(root)/deals/components/create-deal-modal.tsx` | Added all Phase 2 fields with UI (updated) |

**Key Implementation Notes:**
- Owner search with debounce-friendly configuration (search on 1+ chars)
- Dropdown results show user name, email, and role
- Clear button to remove selected owner
- Currency dropdown uses static ISO 4217 codes
- All Phase 2 fields are optional

### Phase 3: Extended Fields & Custom Data (⬜ Pending)

**Goal:** Add advanced fields and support for custom metadata via `properties` JSONB.

| Field | UI Element | Storage | Priority |
|-------|------------|---------|----------|
| Probability | Slider/Number (0-100) | `probability` column | Medium |
| Forecast Category | Dropdown | `forecast_category` column | Medium |
| Tags | Tag input (multi-select) | `properties.tags` | Low |
| Industry | Dropdown | `properties.industry` | Low |
| Products | Multi-select | `properties.products` | Low |
| Competitors | Multi-select | `properties.competitors` | Low |
| Contract Length | Number (months) | `properties.contract_length` | Low |

**Checklist:**
- [ ] Probability input/slider
- [ ] Forecast Category dropdown
- [ ] Tags input (multi-select)
- [ ] Industry dropdown
- [ ] Products multi-select
- [ ] Competitors multi-select
- [ ] Contract Length input
- [ ] Form validation improvements

---

## 10. Phase Summary

| Phase | Fields | Status | Dependencies |
|-------|--------|--------|--------------|
| **Phase 1** | Name, Pipeline, Stage, Close Date, Amount, Description | ✅ Complete | None |
| **Phase 2** | Owner, Currency, Deal Type, Priority, Lead Source, Expected Close Date | ✅ Complete | User search API |
| **Phase 3** | Probability, Forecast Category, Tags, Industry, Products, Competitors, Contract Length | ⬜ Pending | JSONB properties support |

### Field Count by Phase

| Phase | Required | Optional | Total |
|-------|----------|----------|-------|
| Phase 1 | 1 | 5 | **6 fields** |
| Phase 2 | 0 | 6 | **6 fields** |
| Phase 3 | 0 | 7 | **7 fields** |
| **Total** | **1** | **18** | **19 fields** |

---

## 11. Sources

- [HubSpot Default Deal Properties](https://knowledge.hubspot.com/properties/hubspots-default-deal-properties)
- [HubSpot Deal Properties Guide](https://www.leadcrm.io/blog/hubspot-deal-properties-guide/)
- [Salesforce Opportunity Object Reference](https://developer.salesforce.com/docs/atlas.en-us.object_reference.meta/object_reference/sforce_api_objects_opportunity.htm)
- [Pipedrive Custom Fields](https://support.pipedrive.com/en/article/custom-fields)
- [Pipedrive Data Fields](https://support.pipedrive.com/en/article/data-fields-in-pipedrive)
- [SugarCRM Required Fields Best Practices](https://www.sugarcrm.com/blog/standardizing-required-fields-crm/)
