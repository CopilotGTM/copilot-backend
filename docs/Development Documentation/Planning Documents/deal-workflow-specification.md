# Deal Workflow Specification

> Date: 2026-01-22
> Status: Active Development
> Last Updated: 2026-01-22

---

## Overview

This document defines the complete workflow for deal creation and editing in the Internal CRM, based on industry research from HubSpot, Salesforce, Pipedrive, and CRM best practices.

**Key Principle:** Minimize friction at creation, enrich data progressively.

---

## 1. Research Summary

### Industry Best Practices

| CRM | Create Form | Edit View |
|-----|-------------|-----------|
| **HubSpot** | 5-7 fields, only Deal Name required | Full field access with stage-based requirements |
| **Salesforce** | Quick Actions for minimal entry | Page Layouts for comprehensive editing |
| **Pipedrive** | Configurable "Add deal" modal | Pipeline-specific required fields |

### Key Findings

1. **Only `name` is truly required** - All major CRMs allow deal creation with just a name
2. **Stage-based data collection** - Require more fields as deals progress through pipeline
3. **Progressive enrichment** - Start minimal, add details over time
4. **Too many required fields = less adoption** - Research shows over-requiring reduces CRM usage

---

## 2. Deal Workflow Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           DEAL LIFECYCLE                                     │
└─────────────────────────────────────────────────────────────────────────────┘

     ┌──────────────┐          ┌──────────────────┐          ┌─────────────┐
     │              │          │                  │          │             │
     │  CREATE DEAL │   ───►   │   DEAL DETAIL    │   ───►   │   CLOSE     │
     │   (Quick)    │          │   (Full Edit)    │          │   (Win/Loss)│
     │              │          │                  │          │             │
     └──────────────┘          └──────────────────┘          └─────────────┘
           │                          │                            │
           │                          │                            │
           ▼                          ▼                            ▼
     ┌─────────────┐          ┌──────────────────┐          ┌─────────────┐
     │ Phase 1 + 2 │          │ Phase 1 + 2 + 3  │          │ Close Reason│
     │ Fields      │          │ Fields           │          │ Win/Loss    │
     │ (Essential) │          │ (All Available)  │          │ Details     │
     └─────────────┘          └──────────────────┘          └─────────────┘
```

---

## 3. Create Deal Flow (Quick Entry)

### Purpose
Fast deal entry with minimal friction. User can create a deal quickly and add details later.

### API Endpoint
```
POST /users/crm/deals
```

### Available Fields (Phase 1 + Phase 2)

| Field | Required | Type | Default | Notes |
|-------|----------|------|---------|-------|
| **name** | ✅ Yes | string | - | 1-500 chars, the only required field |
| pipeline_id | No | UUID | Default pipeline | Read-only in UI for MVP |
| stage_id | No | UUID | First stage | Dropdown with probability % |
| owner_user_id | No | UUID | - | User search dropdown |
| amount | No | float | - | Deal value (≥ 0) |
| currency | No | string | "USD" | From `/settings/currencies` API |
| close_date | No | datetime | - | Target close date |
| expected_close_date | No | datetime | - | Forecasted close date |
| deal_type | No | string | - | From `/settings/deal-options` API |
| priority | No | string | - | From `/settings/deal-options` API |
| lead_source | No | string | - | From `/settings/deal-options` API |
| description | No | string | - | Free-text notes |

### UI Layout

```
┌─────────────────────────────────────────────────────────────┐
│                      Create Deal                             │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  Deal Name *                                                 │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ Enter deal name                                         ││
│  └─────────────────────────────────────────────────────────┘│
│                                                              │
│  ┌─────────────────────────┐  ┌────────────────────────────┐│
│  │ Pipeline               │  │ Stage                      ││
│  │ [Default Pipeline    ▼]│  │ [Qualification (10%)    ▼] ││
│  └─────────────────────────┘  └────────────────────────────┘│
│                                                              │
│  Owner                                                       │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ 🔍 Search for owner...                           [Clear]││
│  └─────────────────────────────────────────────────────────┘│
│                                                              │
│  ┌───────────────────────────────────┐  ┌──────────────────┐│
│  │ Amount                            │  │ Currency         ││
│  │ [0.00                           ] │  │ [USD - US$    ▼] ││
│  └───────────────────────────────────┘  └──────────────────┘│
│                                                              │
│  ┌─────────────────────────┐  ┌────────────────────────────┐│
│  │ Close Date             │  │ Expected Close Date        ││
│  │ [📅 Select date      ] │  │ [📅 Select date          ] ││
│  └─────────────────────────┘  └────────────────────────────┘│
│                                                              │
│  ┌─────────────┐  ┌─────────────┐  ┌───────────────────────┐│
│  │ Deal Type   │  │ Priority    │  │ Lead Source           ││
│  │ [Select.. ▼]│  │ [Select.. ▼]│  │ [Select...         ▼] ││
│  └─────────────┘  └─────────────┘  └───────────────────────┘│
│                                                              │
│  Description                                                 │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ Add notes about this deal...                            ││
│  │                                                         ││
│  └─────────────────────────────────────────────────────────┘│
│                                                              │
│                          [Cancel]  [Create Deal]             │
└─────────────────────────────────────────────────────────────┘
```

### Request Payload Example

```json
{
  "name": "Acme Corp - Enterprise License",
  "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
  "stage_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
  "owner_user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "amount": 50000,
  "currency": "USD",
  "close_date": "2026-03-15T00:00:00Z",
  "expected_close_date": "2026-03-31T00:00:00Z",
  "deal_type": "New Business",
  "priority": "High",
  "lead_source": "Inbound",
  "description": "Enterprise deal for 100 seats"
}
```

---

## 4. Edit Deal Flow (Full Details)

### Purpose
Comprehensive deal management with all available fields. User can update any field and add Phase 3 advanced fields.

### API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/users/crm/deals/{deal_id}` | GET | Load deal details |
| `/users/crm/deals/{deal_id}` | PATCH | Update deal fields |
| `/users/crm/deals/{deal_id}/stage` | POST | Move to new stage |
| `/users/crm/deals/{deal_id}/owner` | POST | Change owner |

### Available Fields (All Phases)

#### Phase 1 + 2 Fields (Same as Create)

All fields from Create Deal are editable in the detail view.

#### Phase 3 Fields (Edit Only)

| Field | Type | UI Element | Storage | Notes |
|-------|------|------------|---------|-------|
| probability | float | Slider (0-100) | `probability` column | Override stage default |
| forecast_category | string | Dropdown | `forecast_category` column | Pipeline/Best Case/Commit/Closed |
| tags | array | Tag input | `properties.tags` | Multi-select |
| industry | string | Dropdown | `properties.industry` | Technology, Healthcare, etc. |
| products | array | Multi-select | `properties.products` | Products/SKUs |
| competitors | array | Multi-select | `properties.competitors` | Competitor names |
| contract_length | integer | Number input | `properties.contract_length` | Months |

### UI Layout (Deal Detail Page)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  ← Back to Deals                                                             │
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────────┐│
│  │  DEAL HEADER                                                            ││
│  │  ┌──────────────────────────────────────────────────────────────┐      ││
│  │  │ Acme Corp - Enterprise License                       [Edit] │      ││
│  │  │ $50,000 USD  •  Qualification (10%)  •  Owner: John Doe     │      ││
│  │  └──────────────────────────────────────────────────────────────┘      ││
│  └─────────────────────────────────────────────────────────────────────────┘│
│                                                                              │
│  ┌─────────────────────────────────┐  ┌────────────────────────────────────┐│
│  │  DEAL DETAILS (Left Panel)     │  │  ACTIVITY (Right Panel)            ││
│  │  ─────────────────────────────  │  │  ─────────────────────────────     ││
│  │                                 │  │                                    ││
│  │  BASIC INFORMATION              │  │  [Timeline] [Notes] [Tasks]        ││
│  │  ┌───────────────────────────┐ │  │                                    ││
│  │  │ Name: [Acme Corp...    ]  │ │  │  Recent Activity:                  ││
│  │  │ Stage: [Qualification  ▼] │ │  │  • Note added 2h ago              ││
│  │  │ Owner: [John Doe       ▼] │ │  │  • Stage changed 1d ago           ││
│  │  └───────────────────────────┘ │  │  • Deal created 3d ago            ││
│  │                                 │  │                                    ││
│  │  FINANCIAL                      │  │                                    ││
│  │  ┌───────────────────────────┐ │  │                                    ││
│  │  │ Amount: [$50,000        ] │ │  │                                    ││
│  │  │ Currency: [USD         ▼] │ │  │                                    ││
│  │  │ Probability: [====10%==] │ │  │                                    ││
│  │  └───────────────────────────┘ │  │                                    ││
│  │                                 │  │                                    ││
│  │  DATES                          │  │                                    ││
│  │  ┌───────────────────────────┐ │  │                                    ││
│  │  │ Close Date: [2026-03-15]  │ │  │                                    ││
│  │  │ Expected: [2026-03-31]    │ │  │                                    ││
│  │  └───────────────────────────┘ │  │                                    ││
│  │                                 │  │                                    ││
│  │  CLASSIFICATION                 │  │                                    ││
│  │  ┌───────────────────────────┐ │  │                                    ││
│  │  │ Deal Type: [New Business] │ │  │                                    ││
│  │  │ Priority: [High        ▼] │ │  │                                    ││
│  │  │ Lead Source: [Inbound  ▼] │ │  │                                    ││
│  │  │ Forecast: [Pipeline    ▼] │ │  │                                    ││
│  │  └───────────────────────────┘ │  │                                    ││
│  │                                 │  │                                    ││
│  │  ADVANCED (Phase 3)             │  │                                    ││
│  │  ┌───────────────────────────┐ │  │                                    ││
│  │  │ Tags: [enterprise] [+]    │ │  │                                    ││
│  │  │ Industry: [Technology  ▼] │ │  │                                    ││
│  │  │ Products: [Pro Plan] [+]  │ │  │                                    ││
│  │  │ Contract: [12 months   ]  │ │  │                                    ││
│  │  └───────────────────────────┘ │  │                                    ││
│  │                                 │  │                                    ││
│  │            [Save Changes]       │  │                                    ││
│  └─────────────────────────────────┘  └────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 5. API Reference

### 5.1 Settings APIs (New)

#### GET /users/crm/settings/currencies

Returns available currencies for deal amount selection.

**Response:**
```json
{
  "status": "SUCCESS",
  "message": "Currencies retrieved successfully",
  "data": {
    "items": [
      { "code": "USD", "name": "US Dollar", "symbol": "$" },
      { "code": "INR", "name": "Indian Rupee", "symbol": "₹" }
    ],
    "default_code": "USD"
  }
}
```

#### GET /users/crm/settings/deal-options

Returns all dropdown options in a single call (for caching).

**Response:**
```json
{
  "status": "SUCCESS",
  "message": "Deal form options retrieved successfully",
  "data": {
    "currencies": {
      "items": [
        { "code": "USD", "name": "US Dollar", "symbol": "$" },
        { "code": "INR", "name": "Indian Rupee", "symbol": "₹" }
      ],
      "default_code": "USD"
    },
    "deal_types": {
      "items": [
        { "value": "New Business", "label": "New Business", "description": "First-time customer deal" },
        { "value": "Renewal", "label": "Renewal", "description": "Existing customer renewal" },
        { "value": "Expansion", "label": "Expansion", "description": "Expanding existing contract" },
        { "value": "Upsell", "label": "Upsell", "description": "Upgrading to higher tier" }
      ],
      "default_value": null
    },
    "priorities": {
      "items": [
        { "value": "High", "label": "High", "description": "Urgent, needs immediate attention" },
        { "value": "Medium", "label": "Medium", "description": "Normal priority" },
        { "value": "Low", "label": "Low", "description": "Can be addressed later" }
      ],
      "default_value": "Medium"
    },
    "lead_sources": {
      "items": [
        { "value": "Inbound", "label": "Inbound", "description": "Customer reached out" },
        { "value": "Outbound", "label": "Outbound", "description": "Sales outreach" },
        { "value": "Referral", "label": "Referral", "description": "Referred by existing customer" },
        { "value": "Partner", "label": "Partner", "description": "Partner referral" },
        { "value": "Event", "label": "Event", "description": "Trade show or conference" },
        { "value": "Website", "label": "Website", "description": "Website form submission" },
        { "value": "Other", "label": "Other", "description": "Other source" }
      ],
      "default_value": null
    },
    "forecast_categories": {
      "items": [
        { "value": "Pipeline", "label": "Pipeline", "description": "In pipeline, not yet qualified" },
        { "value": "Best Case", "label": "Best Case", "description": "Possible but not committed" },
        { "value": "Commit", "label": "Commit", "description": "Highly likely to close" },
        { "value": "Closed", "label": "Closed", "description": "Deal is closed" }
      ],
      "default_value": "Pipeline"
    }
  }
}
```

### 5.2 Existing Deal APIs

| Endpoint | Method | Purpose | Status |
|----------|--------|---------|--------|
| `POST /users/crm/deals` | POST | Create deal | ✅ Implemented |
| `GET /users/crm/deals/{deal_id}` | GET | Get deal details | ✅ Implemented |
| `PATCH /users/crm/deals/{deal_id}` | PATCH | Update deal | ✅ Implemented |
| `POST /users/crm/deals/{deal_id}/stage` | POST | Change stage | ✅ Implemented |
| `POST /users/crm/deals/{deal_id}/owner` | POST | Change owner | ✅ Implemented |
| `DELETE /users/crm/deals/{deal_id}` | DELETE | Delete deal | ✅ Implemented |

---

## 6. Implementation Status

### Backend

| Component | Status | Files |
|-----------|--------|-------|
| Create Deal API | ✅ Complete | `deals_handler.py:475` |
| Edit Deal API | ✅ Complete | `deals_handler.py:511` |
| Settings API | ✅ Complete | `settings_handler.py` (new) |
| Currency Models | ✅ Complete | `crm_settings_models.py` (new) |

### Frontend

| Component | Status | Files |
|-----------|--------|-------|
| Create Deal Modal (Phase 1+2) | ✅ Complete | `create-deal-modal.tsx` |
| Settings API Integration | ⬜ Pending | - |
| Deal Detail Page | ⬜ Pending | - |
| Deal Edit Form (Phase 3) | ⬜ Pending | - |

---

## 7. Phase Summary

| Phase | Scope | Fields | Status |
|-------|-------|--------|--------|
| **Phase 1** | Create (MVP) | Name, Pipeline, Stage, Close Date, Amount, Description | ✅ Complete |
| **Phase 2** | Create (Extended) | Owner, Currency, Deal Type, Priority, Lead Source, Expected Close Date | ✅ Complete |
| **Phase 3** | Edit Only | Probability, Forecast Category, Tags, Industry, Products, Competitors, Contract Length | ⬜ Pending |

### Why Phase 3 is Edit-Only

Based on CRM best practices:

1. **Reduced friction** - Users create deals faster with fewer fields
2. **Progressive enrichment** - Details are added as deal progresses
3. **Stage-based requirements** - Advanced fields become relevant later in pipeline
4. **Better adoption** - Simpler create flow = more deals logged

---

## 8. Frontend Implementation Checklist

### Next Steps

1. **[ ] Update CreateDealModal to use Settings API**
   - Fetch currencies from `GET /settings/currencies`
   - Cache response for 5 minutes

2. **[ ] Create Deal Detail Page**
   - Route: `/deals/{deal_id}`
   - Layout: Two-panel (details + activity)

3. **[ ] Add Phase 3 Fields to Detail Page**
   - Probability slider/input
   - Forecast category dropdown
   - Tags input
   - Industry dropdown
   - Products/Competitors multi-select
   - Contract length input

4. **[ ] Implement Edit Flow**
   - Inline editing or edit modal
   - Save changes via PATCH API

---

## 9. Files Reference

### Backend Files Created (This Session)

| File | Purpose |
|------|---------|
| `src/api/schemas/crm_settings_models.py` | Settings response models |
| `src/api/routers/crm/settings_handler.py` | Settings API endpoints |

### Backend Files Modified (This Session)

| File | Changes |
|------|---------|
| `src/api/routers/crm/__init__.py` | Added `settings_router` export |
| `src/api/fastapi_app.py` | Registered `settings_router` |

### Documentation Files

| File | Purpose |
|------|---------|
| `deal-workflow-specification.md` | This file - comprehensive workflow spec |
| `create-deal-fields-specification.md` | Field-level specification |
| `internal-crm-frontend-stepwise-integration.md` | Step-by-step integration guide |

---

## 10. Changelog

| Date | Changes |
|------|---------|
| 2026-01-22 | Initial document creation |
| 2026-01-22 | Added Settings API endpoints (currencies, deal-options) |
| 2026-01-22 | Defined Create vs Edit workflow |
| 2026-01-22 | Added Phase 3 field specification |
