# Internal CRM Frontend Stepwise Integration Guide

> Date: 2026-01-21
> Status: In Progress (Step 3 Phase 2 Complete)
> Owner: Copilot Backend
> Last Updated: 2026-01-22 (Phase 2: Dual-session pattern + auto-sync users + type fixes)

---

## Progress Summary

| Step | Phase | Status | Description |
|------|-------|--------|-------------|
| **Step 1** | - | ✅ Complete | Pipeline Load (Internal CRM Readiness) |
| **Step 2** | - | ⏸️ Deferred | Pipeline Display (read-only screen) - Not blocking |
| **Step 3** | Phase 1 | ✅ Complete | MVP Deal Creation (Name, Stage, Amount, Description) |
| **Step 3** | Phase 2 | ✅ Complete | Ownership & Classification (Owner, Currency, Type, Priority, Source) |
| **Step 3** | Phase 3 | ⬜ Pending | Extended Fields (Probability, Forecast, Tags, Industry) |
| **Step 3** | CRUD | 🚧 In Progress | Delete ✅ \| Update ⬜ \| Move Stage ⬜ \| Assign Owner ⬜ |
| **Step 4** | - | ⬜ Pending | Contacts and Companies CRUD |
| **Step 5** | - | ⬜ Pending | Deal Participants and Team Roles |
| **Step 6** | - | ⬜ Pending | Activities and Insights |

**Latest Achievement (2026-01-22):**
- ✅ Dual-session architecture for internal CRM
- ✅ Auto-sync users from `public.users` → `crm_users`
- ✅ Full type safety (no `type: ignore`)
- ✅ Deal creation with owner assignment working end-to-end
- ✅ Delete deal functionality with three-dot menu (internal deals only)

---

## API Documentation Reference

**IMPORTANT:** For detailed API contracts (request/response formats, field validation, error codes), refer to the API documentation files:

| API Category | Documentation File | Description | Status |
|--------------|-------------------|-------------|--------|
| **Deals CRUD** | `docs/API Documentation/crm-deals-api.md` | Create, update, delete deals; stage moves; owner assignment | ✅ Created |
| **Settings/Options** | `docs/API Documentation/crm-settings-api.md` | Currencies, deal types, priorities, lead sources dropdowns | ✅ Created |
| **Users** | `docs/API Documentation/crm-users-api.md` | User search for owner/team selection | ✅ Created |
| **Pipelines** | `docs/API Documentation/crm-pipelines-api.md` | Pipeline and stage listing | ✅ Created |

**Workflow Specification:** `docs/Development Documentation/Planning Documents/deal-workflow-specification.md`

**Field Specification:** `docs/Development Documentation/Planning Documents/create-deal-fields-specification.md`

---

## Purpose

This document defines a small-step, API-by-API frontend integration plan for the
internal CRM capabilities. Each step is independently testable so we can validate
UI behavior before moving to the next step. The plan also calls out the business
value delivered at each step so stakeholders can decide when to pause or ship.

---

## Business Perspective (Why This Order)

- Start with CRM readiness so every org can enter the flow without confusion.
- Show pipelines early to explain the sales process and guide user expectations.
- Enable deals next because they are the core business object.
- Add contacts/companies so deals can be linked to real entities.
- Add participants to capture buying committee and internal team context.
- Finish with activities and insights to unlock daily workflows and AI value.

Success criteria by the end:

- Users can create and manage internal deals in the app without external CRM.
- Deals can be linked to contacts, companies, and internal team members.
- Notes and tasks work as day-to-day operational tools.
- Timeline, meetings, risks, and insights render correctly with the same data.

---

## Frontend Integration Principles

- Base URLs:
  - `/users/crm` for CRM top-level APIs
  - `/users/crm/deals` for deal-scoped APIs
- Response envelope: `GenericResponse` with `status`, `message`, `data`.
- Validation errors from FastAPI return `422` with `detail` array.
- Read-only guard: backend enforces write restrictions on external CRM records.
  UI should surface a "read-only" banner on 409 responses and disable edits.
- Rate limits: handle `429` with retry messaging; do not auto-retry writes.
- Internal CRM integration + default pipeline are auto-created at org creation.
- Existing orgs are backfilled via `scripts/maintenance/ensure_internal_crm_setup.py`.
- No setup/approval flow needed; pipeline is guaranteed to exist for active orgs.
- SSE endpoints are long-lived; use reconnect logic with job IDs.

---

## Step-by-Step Integration Plan

### Step 1: Internal CRM Readiness (Pipeline Load)

Business goal: Ensure the org's default pipeline and stages are loaded and ready for use.

Why this step exists:

- The internal CRM integration is required for all write operations.
- A pipeline is required before the first deal can be created or staged.

Why we are creating this:

- Orgs without HubSpot/Salesforce need a clear first pipeline without manual setup.
- The default pipeline is auto-created during org provisioning (no user approval needed).

Who uses it:

- Any user entering the CRM area.

Prerequisites and assumptions:

- User is authenticated and belongs to an organisation.
- Internal CRM integration + default pipeline are auto-created at org creation.
- For existing orgs, the backfill script has run: `scripts/maintenance/ensure_internal_crm_setup.py`
- Pipeline is guaranteed to exist for all active organizations.

User-facing flow (non-technical):

1) User clicks "Create Deal" or opens Pipelines from the sidebar.
2) App loads pipelines (with stages) - the default pipeline already exists.
3) App loads org users for owner/team selection.
4) User can immediately start creating deals.

Which API to use first (order):

1) `GET /users/crm/pipelines?include_stages=true` (load default pipeline + stages)
   📄 **Docs:** `docs/API Documentation/crm-pipelines-api.md#1-list-pipelines`
2) `GET /users/crm/users?q={search}` (search app users for owner/team selection)
   📄 **Docs:** `docs/API Documentation/crm-users-api.md#1-search-users`
3) `GET /users/crm/settings/deal-options` (load dropdown options for deal form)
   📄 **Docs:** `docs/API Documentation/crm-settings-api.md#2-get-all-deal-form-options`

What is the use of each API (with source file and documentation):

- `GET /users/crm/pipelines?include_stages=true`:
  - Use to render the default pipeline and stages on first load.
  - Pipeline is guaranteed to exist (auto-created during org provisioning).
  - Source: `src/api/routers/crm/pipelines_handler.py#L72`
  - **📄 API Docs:** `docs/API Documentation/crm-pipelines-api.md`
- `GET /users/crm/users?q={search}`:
  - **Searches app users** (not CRM users) for owner/team selection.
  - Queries `public.users` table - returns org members who can be assigned as deal owners or team members.
  - Supports `limit`, `cursor`, and `q` (search by name or email).
  - Source: `src/api/routers/crm/users_handler.py#L45`
  - **📄 API Docs:** `docs/API Documentation/crm-users-api.md`
- `GET /users/crm/settings/deal-options`:
  - Returns all dropdown options in single call (currencies, deal types, priorities, lead sources).
  - Cache for 5-10 minutes - options rarely change.
  - Source: `src/api/routers/crm/settings_handler.py`
  - **📄 API Docs:** `docs/API Documentation/crm-settings-api.md`

What data the UI must capture:

- `pipeline.pipeline_id`, `pipeline.name`, `pipeline.stages[]` for stage columns.
- `CRMUsersPage.items[]` for owner/team selections.

Validation and behavior:

- Pipeline list will always have at least one pipeline (the default).
- User list should be cached per session for faster owner selection.

Error handling and fallback:

- `429`: show a short retry message and disable reload briefly.
- `500`: show a generic error and offer retry.
- Empty pipelines (edge case - should not happen): show error message and contact support.

Stop-and-test checklist:

- ✅ Pipeline list loads with default pipeline and 6 stages.
- ✅ Stages are in correct order: Qualification → Closed Lost.
- ✅ User list loads and is visible in owner selector. (Implemented in Phase 2)

---

## Implementation (Step 1)

**Status:** ✅ Completed

**What was implemented:**

1. **Create Deal Button** - Added to deals page header (top right)
   - Opens modal on click
   - Triggers pipeline fetch if not cached

2. **Pipeline Fetch on Demand** - Fetches pipelines only when "Create Deal" is clicked
   - API: `GET /users/crm/pipelines?include_stages=true`
   - 📄 **Docs:** `docs/API Documentation/crm-pipelines-api.md#1-list-pipelines`
   - **Caching:** 5 minutes (staleTime) - subsequent clicks within 5 min use cached data
   - **Garbage collection:** 10 minutes - data removed from memory after 10 min of inactivity

3. **Create Deal Modal** - Displays pipeline/stage data
   - Shows default pipeline name (read-only)
   - Shows first stage (read-only)
   - Form fields: Deal Name (required), Amount, Description
   - Submit button logs data to console (no API call yet)

**Frontend files created:**

| File | Purpose |
|------|---------|
| `src/modules/deals/api/crm.client.ts` | Axios client with base URL `/users/crm` |
| `src/modules/deals/api/pipelines.api.ts` | `listPipelines()`, `getPipelineStages()` API functions |
| `src/modules/deals/types/pipelines.types.ts` | TypeScript types: `PipelineItem`, `PipelineStageItem`, etc. |
| `src/modules/deals/hooks/use-pipelines.ts` | `usePipelines()` React Query hook with 5-min cache |
| `src/app/(root)/deals/components/create-deal-button.tsx` | Create Deal button component |
| `src/app/(root)/deals/components/create-deal-modal.tsx` | Create Deal modal form |

**Frontend files updated:**

| File | Changes |
|------|---------|
| `src/modules/deals/api/index.ts` | Added exports for `crmApiClient`, `pipelines.api` |
| `src/modules/deals/types/index.ts` | Added exports for pipeline types |
| `src/modules/deals/hooks/index.ts` | Added exports for `usePipelines` and helper functions |
| `src/app/(root)/deals/page.tsx` | Added `CreateDealButton` to page header |

**Backend files updated:**

| File | Changes |
|------|---------|
| `src/service/orgnisation/organization_management.py` | Fixed import: `internal.internal_crm_setup_service` |
| `src/service/crm/internal/crm_company_service.py` | Fixed imports to use `internal/` subfolder |
| `src/service/crm/internal/crm_contact_service.py` | Fixed imports to use `internal/` subfolder |
| `src/service/crm/internal/deal_participant_service.py` | Fixed imports to use `internal/` subfolder |
| `src/service/crm/__init__.py` | Updated docstring with new folder paths |

**Caching behavior:**

- **First click:** Fetches `GET /users/crm/pipelines?include_stages=true` (📄 `crm-pipelines-api.md`)
- **Within 5 minutes:** Uses cached data (no API call)
- **After 5 minutes:** Data marked stale, refetch on next click
- **After 10 minutes of inactivity:** Data removed from memory (garbage collected)

**Testing:**

1. Click "Create Deal" button → Modal opens, pipelines load
2. Close modal, click again within 5 min → Modal opens instantly (cached)
3. Check browser DevTools Network tab → Only 1 API call on first click
4. Fill form and submit → Check console for logged data

---

### Step 2: Pipeline Display (Read-Only)

**Status:** ⏸️ Deferred (Not blocking)

**Decision:** Skipping this step for now. Step 1 already fetches and displays pipeline/stage data in the Create Deal modal, and the deals table shows stages in the "STAGE" column. A dedicated Pipeline screen will be valuable later when adding Kanban board view or pipeline customization (editing stages, probabilities), but it's not required to proceed with deal creation functionality.

**When to revisit:** After Step 3 (Deal CRUD) is complete, or when implementing Kanban board view.

---

Business goal: Expose the sales process structure to users.

Why this step exists:

- Users need to understand the pipeline before they can manage deals.
- It validates stage ordering, labels, and probability display in the UI.

Why we are creating this:

- It provides a safe, read-only screen to validate pipeline rendering.
- It becomes the base for Kanban columns in Step 3.

User-facing flow (non-technical):

1) User opens Pipeline or Sales Process screen.
2) App loads default pipeline and stage list.
3) UI renders stages with labels, probability, and closed state.

Which API to use first (order):

1) `GET /users/crm/pipelines?include_stages=true` (primary data source)
   📄 **Docs:** `docs/API Documentation/crm-pipelines-api.md#1-list-pipelines`
2) `GET /users/crm/pipelines/{pipeline_id}/stages` (optional refresh)
   📄 **Docs:** `docs/API Documentation/crm-pipelines-api.md#2-get-pipeline-stages`

What is the use of each API (with source file and documentation):

- `GET /users/crm/pipelines?include_stages=true`:
  - Use for initial screen load.
  - Includes stage list so you avoid an extra network call.
  - Source: `src/api/routers/crm/pipelines_handler.py#L72`
  - 📄 **Docs:** `docs/API Documentation/crm-pipelines-api.md#1-list-pipelines`
- `GET /users/crm/pipelines/{pipeline_id}/stages`:
  - Use for refresh or when `include_stages=false` on the list call.
  - Source: `src/api/routers/crm/pipelines_handler.py#L116`
  - 📄 **Docs:** `docs/API Documentation/crm-pipelines-api.md#2-get-pipeline-stages`
What data the UI must capture:

- `PipelineItem.name` and `label` for the pipeline header.
- `PipelineStageItem.label` for column names.
- `stage_state` (`open`, `won`, `lost`) to color closed columns.
- `display_order` to sort columns in the correct order.

Validation and behavior:

- Respect `display_order` for stage ordering.
- Mark `won` and `lost` columns as closed.

Error handling and fallback:

- No pipelines returned: show "No pipelines configured" and link to Step 1.
- Empty stages: show retry button and a short explanation.

Stop-and-test checklist:

- Pipeline list renders with stage columns in the correct order.
- Stage labels and probabilities match backend values.

---

### Step 3: Deals Core CRUD and Stage/Owner Actions

**Status:** 🚧 In Progress (Next step after Step 1)

**Note:** Proceeding directly from Step 1 to Step 3. Step 2 (dedicated Pipeline screen) is deferred as it's not blocking - pipeline/stage data is already available via Step 1's implementation.

---

Business goal: Users can create, manage, and advance internal deals.

Why this step exists:

- Deals are the core object for forecasting, pipeline tracking, and AI insights.
- All later features (tasks, notes, stakeholders) depend on deals.

Why we are creating this:

- It is the first step that delivers day-to-day user value.
- It validates internal vs external write restrictions (409).

User-facing flow (non-technical):

1) User opens Deals list (table or Kanban).
2) User creates a deal.
3) User edits fields in the deal detail view.
4) User moves the deal across stages.
5) User assigns an owner from org users.

Which API to use first (order):

1) `GET /users/crm/deals` (render list and filters) - ✅ Implemented
   📄 **Docs:** `docs/API Documentation/crm-deals-api.md#2-list-deals`
2) `GET /users/crm/deals/stats` (summary cards and KPIs) - ⏸️ Deferred (not critical for MVP)
   📄 **Docs:** `docs/API Documentation/crm-deals-api.md` (stats section TBD)
3) `POST /users/crm/deals` (create first internal deal) - ✅ Implemented (Phase 1+2)
   📄 **Docs:** `docs/API Documentation/crm-deals-api.md#1-create-deal`
4) `GET /users/crm/deals/{deal_id}` (detail view)
   📄 **Docs:** `docs/API Documentation/crm-deals-api.md#3-get-deal-detail`
5) `PATCH /users/crm/deals/{deal_id}` (edit core fields)
   📄 **Docs:** `docs/API Documentation/crm-deals-api.md#4-update-deal`
6) `POST /users/crm/deals/{deal_id}/stage` (Kanban move)
   📄 **Docs:** `docs/API Documentation/crm-deals-api.md#5-move-stage`
7) `POST /users/crm/deals/{deal_id}/owner` (owner assignment)
   📄 **Docs:** `docs/API Documentation/crm-deals-api.md#6-assign-owner`
8) `DELETE /users/crm/deals/{deal_id}` (soft delete)
   📄 **Docs:** `docs/API Documentation/crm-deals-api.md#7-delete-deal`

What is the use of each API (with source file and documentation):

- `GET /users/crm/deals`:
  - Main list endpoint with pagination and filters.
  - Use `fetch_all=true` for small datasets (max 500).
  - Source: `src/api/routers/crm/deals_handler.py#L117`
  - 📄 **Docs:** `docs/API Documentation/crm-deals-api.md#2-list-deals`
- `GET /users/crm/deals/stats`:
  - Populate dashboard cards and quick filters.
  - Source: `src/api/routers/crm/deals_handler.py#L274`
  - 📄 **Docs:** `docs/API Documentation/crm-deals-api.md` (stats section TBD)
- `POST /users/crm/deals`:
  - Creates a deal in internal CRM.
  - `pipeline_id` and `stage_id` are optional; defaults to first stage if omitted.
  - Source: `src/api/routers/crm/deals_handler.py#L475`
  - 📄 **Docs:** `docs/API Documentation/crm-deals-api.md#1-create-deal`
  - **Important: No account/contact required first**
    - Based on research from HubSpot, Pipedrive, Salesforce, and CRM best practices:
    - Deals can be created without requiring account/contact association
    - Linking contacts/companies comes later via deal participants (Step 5)
    - This approach improves user adoption and allows quick deal entry
  - **Create Deal Form Fields (All Backend Fields):**
    | Field | Required | Type | Validation |
    |-------|----------|------|------------|
    | `name` | ✅ Yes | string | 1-500 chars, cannot be empty |
    | `stage_id` | No | UUID | Must belong to selected pipeline |
    | `pipeline_id` | No | UUID | Defaults to default pipeline |
    | `amount` | No | float | Must be ≥ 0 |
    | `currency` | No | string | 3 chars ISO code (USD, EUR) |
    | `close_date` | No | datetime | Expected close date |
    | `expected_close_date` | No | datetime | Forecasted close date |
    | `owner_user_id` | No | UUID | App user from user search |
    | `description` | No | string | Deal notes/description |
    | `deal_type` | No | string | e.g., "New Business", "Renewal" |
    | `priority` | No | string | e.g., "High", "Medium", "Low" |
    | `lead_source` | No | string | e.g., "Inbound", "Outbound" |
  - **Create Deal Form (MVP Implementation):**
    | Field | UI Element | Notes |
    |-------|------------|-------|
    | Deal Name | Text input | Required, placeholder "Enter deal name" |
    | Pipeline | Read-only input | Shows default pipeline name |
    | Stage | Dropdown | All stages with probability %, sorted by display_order |
    | Close Date | Date picker | Optional, for forecasting |
    | Amount | Number input | Optional, min=0, step=0.01 |
    | Description | Textarea | Optional notes |
  - **Deferred for later iterations:**
    - Owner selection (needs user search API integration)
    - Currency selector (default to USD for now)
    - Deal type, Priority, Lead source (can add later)
  - **Create Deal Button Flow:**
    1. User clicks "Create Deal" button
    2. Check if pipeline data is cached → if not, fetch `GET /users/crm/pipelines?include_stages=true`
       📄 **Docs:** `docs/API Documentation/crm-pipelines-api.md#1-list-pipelines`
    3. Open Create Deal modal/form with stage dropdown populated
    4. User fills form (only `name` is required)
    5. Submit → `POST /users/crm/deals`
       📄 **Docs:** `docs/API Documentation/crm-deals-api.md#1-create-deal`
    6. On success → refresh deals list, close modal
- `GET /users/crm/deals/{deal_id}`:
  - Loads the detail view with owner/team and summary data.
  - Source: `src/api/routers/crm/deals_handler.py#L322`
  - 📄 **Docs:** `docs/API Documentation/crm-deals-api.md#3-get-deal-detail`
- `PATCH /users/crm/deals/{deal_id}`:
  - Updates editable fields (name, amount, close_date, etc).
  - Source: `src/api/routers/crm/deals_handler.py#L511`
  - 📄 **Docs:** `docs/API Documentation/crm-deals-api.md#4-update-deal`
- `POST /users/crm/deals/{deal_id}/stage`:
  - Moves a deal to a new stage by `stage_id`.
  - Source: `src/api/routers/crm/deals_handler.py#L578`
  - 📄 **Docs:** `docs/API Documentation/crm-deals-api.md#5-move-stage`
- `POST /users/crm/deals/{deal_id}/owner`:
  - Assigns or clears the primary owner (`owner_user_id` can be null).
  - Source: `src/api/routers/crm/deals_handler.py#L612`
  - 📄 **Docs:** `docs/API Documentation/crm-deals-api.md#6-assign-owner`
- `DELETE /users/crm/deals/{deal_id}`:
  - Soft delete; remove from lists.
  - Source: `src/api/routers/crm/deals_handler.py#L545`
  - 📄 **Docs:** `docs/API Documentation/crm-deals-api.md#7-delete-deal`
What data the UI must capture:

- List view: `deal_id`, `name`, `amount`, `currency`, `stage_label`, `deal_status`.
- Detail view: `deal_summary_data`, `owner_details`, `deal_team`.

Validation and behavior:

- Stage move should update both stage label and deal status.
- Owner assignment should update owner name and email in the header.

Error handling and fallback:

- `409 External CRM deals are read-only`: show read-only banner and disable edits.
- `400 Stage does not belong to pipeline`: show toast and revert UI.
- `404 Deal not found`: remove from UI and show brief notice.

Stop-and-test checklist:

- ✅ Create a deal and see it in list (Phase 1 complete)
- ✅ Owner selection with user search dropdown (Phase 2 complete)
- ✅ Classification fields: Currency, Deal Type, Priority, Lead Source (Phase 2 complete)
- ✅ Expected Close Date field (Phase 2 complete)
- ✅ Delete deal with three-dot menu and confirmation (CRUD: Delete complete)
- ⬜ Update fields and verify saved values on refresh.
- ⬜ Move stages and verify stage label and status update.
- ⬜ Assign owner and verify name/email update.

---

## Implementation (Step 3 - Phase 1 Complete)

**Status:** ✅ Phase 1 Complete (MVP Deal Creation)

**Date:** 2026-01-22

**Reference:** See `create-deal-fields-specification.md` for comprehensive field documentation.

### What was implemented:

**1. Create Deal Modal (MVP Fields)**
   - Deal Name (required, 1-500 chars)
   - Pipeline (read-only, shows default)
   - Stage dropdown (all stages, sorted by display_order, shows probability %)
   - Close Date (date picker)
   - Amount (number input, >= 0)
   - Description (textarea)

**2. API Integration**
   - `createDeal()` function calls `POST /users/crm/deals`
   - 📄 **Docs:** `docs/API Documentation/crm-deals-api.md#1-create-deal`
   - `useCreateDeal()` React Query mutation hook
   - Automatic deals list refresh on success (invalidates `["crm_deals"]` queries)
   - Loading state during submission ("Creating...")
   - Error handling with user-friendly messages

**3. Theme Integration**
   - Modal uses semantic theme tokens from `theme.ts`
   - Supports dark/light mode via tokens: `bg.surface`, `fg.primary`, `border.default`, etc.
   - Uses theme spacing: `modal.padding`, `form.gap`, `form.label.mb`
   - Uses theme shadows: `shadow="modal"`
   - Uses theme text styles: `textStyle="heading.lg"`, `textStyle="input.label"`

**4. Request Payload (Phase 1)**
```json
{
  "name": "Deal Name",
  "pipeline_id": "uuid",
  "stage_id": "uuid",
  "close_date": "2026-03-15T00:00:00Z",
  "amount": 50000,
  "description": "Notes..."
}
```

### Frontend files created:

| File | Purpose |
|------|---------|
| `src/modules/deals/hooks/use-create-deal.ts` | `useCreateDeal()` mutation hook with error handling |

### Frontend files updated:

| File | Changes |
|------|---------|
| `src/modules/deals/types/deals.types.ts` | Added `CreateDealRequest`, `CreateDealResponseData`, `CreateDealResponse` types |
| `src/modules/deals/types/index.ts` | Export new deal create types |
| `src/modules/deals/api/deals.api.ts` | Added `createDeal()` function using `crmApiClient` |
| `src/modules/deals/api/index.ts` | Export `createDeal` |
| `src/modules/deals/hooks/index.ts` | Export `useCreateDeal`, `createDealKeys` |
| `src/app/(root)/deals/components/create-deal-modal.tsx` | Full API integration, theme tokens, loading/error states |

### Key Implementation Details:

**API Client:**
- Uses `crmApiClient` (base URL: `/users/crm`) not `dealsApiClient`
- Endpoint: `POST /deals` → `POST /users/crm/deals`
- 📄 **Docs:** `docs/API Documentation/crm-deals-api.md#1-create-deal`

**Query Invalidation:**
- On success, invalidates `["crm_deals"]` query key
- This refreshes the deals list automatically

**Error Handling:**
- Transforms Axios errors into `CreateDealError` type
- Handles 422 validation errors with detailed messages
- Displays errors in red banner within modal

**Percentage Display:**
- Probabilities displayed as-is (not multiplied by 100)
- Backend stores probabilities as whole numbers (10, 25, 50, etc.)

### Testing:

1. Click "Create Deal" button → Modal opens with theme styling
2. Enter deal name (required), optionally select stage, close date, amount
3. Click "Create Deal" → Shows "Creating..." loading state
4. On success → Modal closes, deals list refreshes, new deal appears
5. On error → Shows error message in red banner, modal stays open

---

## Create Deal - Phased Implementation Plan

### Phase 1: MVP (✅ Complete)

**Goal:** Basic deal creation with minimal required fields.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| Deal Name | Text input | ✅ Yes | 1-500 chars |
| Pipeline | Read-only | No | Shows default pipeline |
| Stage | Dropdown | No | All stages with probability % |
| Close Date | Date picker | No | For forecasting |
| Amount | Number input | No | >= 0 |
| Description | Textarea | No | Optional notes |

**API:** `POST /users/crm/deals`
📄 **Docs:** `docs/API Documentation/crm-deals-api.md#1-create-deal`

---

### Phase 2: Ownership & Classification (✅ Complete - 2026-01-22)

**Goal:** Add owner assignment and deal classification fields.

| Field | Type | Required | Notes | Endpoint Required | Docs |
|-------|------|----------|-------|-------------------|------|
| Owner | User search dropdown | No | Search app users | `GET /users/crm/users?q=` | `crm-users-api.md#1-search-users` |
| Currency | Dropdown | No | ISO codes (USD, INR) | `GET /users/crm/settings/deal-options` | `crm-settings-api.md#2-get-all-deal-form-options` |
| Deal Type | Dropdown | No | New Business, Renewal, Expansion, Upsell | `GET /users/crm/settings/deal-options` | `crm-settings-api.md#2-get-all-deal-form-options` |
| Priority | Dropdown | No | High, Medium, Low | `GET /users/crm/settings/deal-options` | `crm-settings-api.md#2-get-all-deal-form-options` |
| Lead Source | Dropdown | No | Inbound, Outbound, Referral, etc. | `GET /users/crm/settings/deal-options` | `crm-settings-api.md#2-get-all-deal-form-options` |
| Expected Close Date | Date picker | No | Forecasted close (separate from Close Date) | None | N/A |

**What was implemented:**
- Owner field with user search dropdown (uses `GET /users/crm/users?q=`)
- All dropdown options fetched dynamically from `GET /users/crm/settings/deal-options`
- Currency dropdown with default (USD) - currently supports USD, INR
- Deal Type dropdown (New Business, Renewal, Expansion, Upsell)
- Priority dropdown with default (Medium) - High, Medium, Low
- Lead Source dropdown (Inbound, Outbound, Referral, Partner, Event, Website, Other)
- Expected Close Date picker

**Request Payload (Phase 2):**
```json
{
  "name": "Deal Name",
  "pipeline_id": "uuid",
  "stage_id": "uuid",
  "close_date": "2026-03-15T00:00:00Z",
  "expected_close_date": "2026-03-31T00:00:00Z",
  "amount": 50000,
  "currency": "USD",
  "owner_user_id": "uuid-of-owner",
  "deal_type": "New Business",
  "priority": "High",
  "lead_source": "Inbound",
  "description": "Notes..."
}
```

---

### Phase 3: Extended Fields & Custom Data (⬜ Pending)

**Goal:** Add advanced fields and custom metadata support.

| Field | Type | Required | Notes | Storage |
|-------|------|----------|-------|---------|
| Probability | Slider/Number | No | Override stage default (0-100) | `probability` column |
| Forecast Category | Dropdown | No | Pipeline, Best Case, Commit, Closed | `forecast_category` column |
| Tags | Tag input | No | Multi-select tags | `properties` JSONB |
| Industry | Dropdown | No | Technology, Healthcare, etc. | `properties` JSONB |
| Products | Multi-select | No | Products/SKUs in deal | `properties` JSONB |
| Competitors | Multi-select | No | Competitor names | `properties` JSONB |
| Contract Length | Number | No | Months | `properties` JSONB |

**Dependencies:**
- JSONB `properties` column support in API
- Tag management system
- Product catalog integration (optional)

---

### Phase Summary

| Phase | Fields | Status | Priority |
|-------|--------|--------|----------|
| **Phase 1** | Name, Pipeline, Stage, Close Date, Amount, Description | ✅ Complete | High |
| **Phase 2** | Owner, Currency, Deal Type, Priority, Lead Source, Expected Close Date | ✅ Complete | Medium |
| **Phase 3** | Probability, Forecast Category, Tags, Industry, Products, Competitors, Contract Length | ⬜ Pending | Low |

---

## Implementation (Step 3 - Phase 2 Complete)

**Status:** ✅ Phase 2 Complete (Ownership & Classification)

**Date:** 2026-01-22

### What was implemented:

**1. Owner Search Dropdown**
   - Searchable dropdown using `GET /users/crm/users?q=`
   - 📄 **Docs:** `docs/API Documentation/crm-users-api.md#1-search-users`
   - Shows user name with email subtitle
   - Clear button to remove selection
   - Debounced search (triggers on 1+ characters)

**2. Dynamic Dropdown Options via API**
   - All dropdown options fetched from `GET /users/crm/settings/deal-options`
   - 📄 **Docs:** `docs/API Documentation/crm-settings-api.md#2-get-all-deal-form-options`
   - Single API call returns: currencies, deal_types, priorities, lead_sources, forecast_categories
   - Options cached for 10 minutes (rarely change)
   - Default values applied automatically (currency: USD, priority: Medium)

**3. Currency Dropdown**
   - Fetched from API (currently USD, INR)
   - Shows code, name, and symbol: "USD - US Dollar ($)"

**4. Deal Type Dropdown**
   - Fetched from API: New Business, Renewal, Expansion, Upsell

**5. Priority Dropdown**
   - Fetched from API: High, Medium, Low
   - Default: Medium

**6. Lead Source Dropdown**
   - Fetched from API: Inbound, Outbound, Referral, Partner, Event, Website, Other

**7. Expected Close Date**
   - Date picker (separate from Close Date)

### Frontend files created:

| File | Purpose |
|------|---------|
| `src/modules/deals/types/users.types.ts` | `SearchUsersParams` type |
| `src/modules/deals/api/users.api.ts` | `searchUsers()` API function |
| `src/modules/deals/hooks/use-users.ts` | `useUserSearch()` hook, `formatUserDisplay()` helpers |
| `src/modules/deals/types/settings.types.ts` | Types for deal form options (currencies, dropdowns) |
| `src/modules/deals/api/settings.api.ts` | `getDealFormOptions()`, `getCurrencies()` API functions |
| `src/modules/deals/hooks/use-deal-options.ts` | `useDealOptions()` hook with 10-min cache |

### Frontend files updated:

| File | Changes |
|------|---------|
| `src/modules/deals/types/deals.types.ts` | Added Phase 2 fields to `CreateDealRequest` |
| `src/modules/deals/types/index.ts` | Export `SearchUsersParams`, settings types |
| `src/modules/deals/api/index.ts` | Export `searchUsers`, `getDealFormOptions`, `getCurrencies` |
| `src/modules/deals/hooks/index.ts` | Export `useUserSearch`, `useDealOptions`, format helpers |
| `src/app/(root)/deals/components/create-deal-modal.tsx` | Dynamic dropdowns from API, default values, responsive grid |

### UI Layout:

- **Row 1:** Deal Name (full width, required)
- **Row 2:** Pipeline + Stage (2 columns)
- **Row 3:** Owner (full width with search dropdown)
- **Row 4:** Amount + Currency (2:1 ratio)
- **Row 5:** Close Date + Expected Close Date (2 columns)
- **Row 6:** Deal Type + Priority + Lead Source (3 columns)
- **Row 7:** Description (full width)

### Testing:

1. Click "Create Deal" button → Modal opens with all Phase 2 fields
2. Search for owner → Type in search box, see user suggestions
3. Select owner → User name fills input, Clear button appears
4. Fill all optional fields (currency, deal type, priority, lead source, expected close date)
5. Submit → All fields sent in request payload
6. On success → Modal closes, deals list refreshes

### Backend Implementation (Phase 2):

**Status:** ✅ Complete

#### Critical Architecture Change: Dual-Session Pattern

**Problem Identified:**
- Deal creation with owner assignment was failing with `permission denied for table users`
- Org-scoped database sessions don't have SELECT permission on `public.users` table
- `InternalCRMUserService` needed to read from `public.users` but write to `org_*.crm_users`

**Solution Implemented:**
- Implemented **dual-session pattern** where services receive both:
  - **Master session**: For reading `public.users` (schema: public)
  - **Org session**: For reading/writing org-scoped tables (schema: org_*)

#### Auto-Sync Users Feature

**MVP Implementation:**
- When creating first deal with owner, system auto-syncs ALL org users from `public.users` to `crm_users`
- Subsequent operations only query `crm_users` (org schema)
- Toggle available: `InternalCRMUserService.AUTO_SYNC_ENABLED = True` (MVP) / `False` (future admin control)

**Flow:**
```
1. ensure_crm_user(user_id)
2. Check if user exists in crm_users → Found? Return ✓
3. If NOT found AND no CRM users exist → sync_org_users_to_crm()
4. Bulk sync ALL active users from public.users → org_*.crm_users
5. Retry lookup → Found? Return ✓
```

**Future Admin Flow (when AUTO_SYNC_ENABLED=False):**
- Admin must manually add users via `/users/crm/settings/users/sync`
- Error if user not in crm_users: "User not available in CRM. Add users via CRM Settings."

#### Backend Files Modified:

| File | Changes | Type |
|------|---------|------|
| `src/service/crm/internal/internal_crm_user_service.py` | Complete rewrite with dual-session pattern + auto-sync logic | Modified |
| `src/service/crm/internal/deal_write_service.py` | Accept both sessions, pass to InternalCRMUserService | Modified |
| `src/api/routers/crm/deals_handler.py` | Inject both `get_org_async_session` and `get_master_async_session` | Modified |
| `src/service/crm/access/crm_user_service.py` | Fixed SQLAlchemy type errors using `col()` | Modified |

#### New Methods in InternalCRMUserService:

| Method | Purpose |
|--------|---------|
| `has_crm_users()` | Check if any CRM users exist for internal integration |
| `get_crm_user_by_app_user_id()` | Look up CRM user by app user UUID |
| `sync_org_users_to_crm()` | Bulk sync ALL org users from `public.users` → `crm_users` |
| `ensure_crm_user()` | Main entry: check → auto-sync if needed → return user |

#### Type Safety Improvements:

**Fixed all type errors using production-grade patterns (no `type: ignore`):**

```python
from sqlmodel import col

# ✅ Correct pattern for SQLModel/SQLAlchemy queries
stmt = select(Model).where(
    col(Model.column1) == value1,           # Comparison
    col(Model.column2).ilike("%search%"),   # String operations
    col(Model.boolean_col).is_not(True),    # Boolean operations
)
stmt = stmt.order_by(
    col(Model.created_at).desc(),           # Ordering
    col(Model.id).asc()
)
```

**Files with type fixes:**
- `crm_user_service.py` - All column refs wrapped with `col()`
- `internal_crm_user_service.py` - All column refs wrapped with `col()`
- `deal_write_service.py` - Column refs + enum fixes (removed `.value`)
- `deals_handler.py` - Proper `DealListItem` construction instead of dicts

#### Key Technical Decisions:

1. **CRM users table design**: Syncs app users to org-scoped CRM table for consistency with external CRM data (HubSpot/Salesforce pattern)
2. **Schema isolation maintained**: Org sessions never access `public.users` directly after initial sync
3. **Performance**: After first sync, zero cross-schema queries needed
4. **Future extensibility**: Easy to add admin controls by setting `AUTO_SYNC_ENABLED=False`

#### Internal vs External CRM Deal Identification (Phase 2 Enhancement)

**Feature Added:** `is_internal` field to distinguish internal CRM deals from external CRM deals

**Problem:**
- Frontend needs to know which deals are editable (internal CRM) vs read-only (synced from HubSpot/Salesforce)
- Edit/delete buttons should only appear for internal CRM deals

**Solution Implemented:**
- Added `is_internal: bool` field to both `DealListItem` and `DealDetail` response schemas
- `true` = Internal CRM deal (editable)
- `false` = External CRM deal (read-only, synced from HubSpot/Salesforce)

**Implementation:**

1. **Schema Changes:**
```python
# src/api/schemas/crm_models.py
class DealListItem(BaseModel):
    # ... existing fields ...
    is_internal: bool = Field(
        ...,
        description="True if internal CRM deal (editable), False if external CRM (read-only).",
    )

class DealDetail(BaseModel):
    # ... existing fields ...
    is_internal: bool = Field(
        ...,
        description="True if internal CRM deal (editable), False if external CRM (read-only).",
    )
```

2. **Handler Logic:**
```python
# src/api/routers/crm/deals_handler.py

async def _get_integration_types_map(
    session: AsyncSession, organisation_id: UUID, integration_ids: set[UUID]
) -> dict[UUID, bool]:
    """Batch fetch integration types and return mapping of integration_id -> is_internal."""
    # Query KnowledgeIntegration table for integration types
    # Return {integration_id: integration_type == IntegrationType.INTERNAL_CRM}

# In list_deals():
integration_ids = {deal.integration_id for deal in deals if deal.integration_id}
integration_types_map = await _get_integration_types_map(session, org_id, integration_ids)

# When constructing response:
is_internal=integration_types_map.get(deal.integration_id, False)

# In get_deal():
# Same logic for single deal
```

**Performance:**
- Batch query for integration types (single query for all deals in list)
- No N+1 query problem
- Minimal overhead (<1ms for typical lists)

**Frontend Usage:**
```typescript
// Conditionally show edit/delete buttons
{deal.is_internal && (
  <>
    <EditButton onClick={() => editDeal(deal.id)} />
    <DeleteButton onClick={() => deleteDeal(deal.id)} />
  </>
)}

// Or disable actions
<Button disabled={!deal.is_internal}>Edit Deal</Button>
```

**Files Modified:**
| File | Changes |
|------|---------|
| `src/api/schemas/crm_models.py` | Added `is_internal` field to `DealListItem` and `DealDetail` |
| `src/api/routers/crm/deals_handler.py` | Added `_get_integration_types_map()` helper, updated list/detail endpoints |
| `docs/API Documentation/crm-deals-api.md` | Documented `is_internal` field in response schemas |

### Frontend Implementation (is_internal field):

**Status:** ✅ Complete

**Date:** 2026-01-22

**Frontend files updated:**

| File | Changes |
|------|---------|
| `src/modules/deals/types/deals.types.ts` | Added `is_internal?: boolean` to `DealListItem` and `DealDetail` interfaces |

**Frontend Usage:**

The `is_internal` field is now available on both `DealListItem` and `DealDetail` types:

```typescript
// Example: Conditionally show edit/delete buttons
{deal.is_internal && (
  <Button onClick={handleEdit}>Edit</Button>
  <Button onClick={handleDelete}>Delete</Button>
)}

// Example: Disable actions for external CRM deals
<Button disabled={!deal.is_internal}>Move Stage</Button>
```

**Field Behavior:**
- `is_internal: true` → Internal CRM deal, fully editable
- `is_internal: false` → External CRM deal (HubSpot/Salesforce), read-only
- `is_internal: undefined` → Legacy data, treat as external (read-only)

---

## Implementation (Step 3 - CRUD Operations: Delete Deal)

**Status:** ✅ Complete (Delete Functionality)

**Date:** 2026-01-22

### What was implemented:

**1. Delete API Function**
   - `deleteDeal()` function calls `DELETE /users/crm/deals/{deal_id}`
   - 📄 **Docs:** `docs/API Documentation/crm-deals-api.md#7-delete-deal`
   - Only works for internal CRM deals (`is_internal: true`)
   - Returns success message on completion

**2. Delete Mutation Hook**
   - `useDeleteDeal()` React Query mutation hook
   - Automatic deals list refresh on success (invalidates `["crm_deals"]` queries)
   - Error handling with user-friendly toast messages

**3. Three-Dot Menu Component**
   - Added to each deal row in the deals list
   - Only appears when `deal.is_internal === true`
   - Uses Chakra UI Menu component with dropdown positioning
   - Delete option with trash icon
   - Prevents row click navigation when clicking menu

**4. Delete Confirmation Dialog**
   - Confirmation dialog before deletion to prevent accidental deletions
   - Shows deal name in confirmation message
   - Cancel and Delete buttons
   - Loading state during deletion
   - Success/error toast notifications

### Frontend files created:

| File | Purpose |
|------|---------|
| `src/modules/deals/hooks/use-delete-deal.ts` | `useDeleteDeal()` mutation hook |

### Frontend files updated:

| File | Changes |
|------|---------|
| `src/modules/deals/api/deals.api.ts` | Added `deleteDeal()` API function |
| `src/modules/deals/hooks/index.ts` | Export `useDeleteDeal` hook |
| `src/app/(root)/deals/page.tsx` | Added three-dot menu, delete dialog, and delete handlers |

### UI Implementation:

**Deal List Row Structure:**
- Main row content (clickable) → Navigates to deal details
- Three-dot menu button (only for internal deals) → Opens action menu
  - Delete option → Opens confirmation dialog

**Delete Flow:**
1. User clicks three-dot menu on internal deal
2. User clicks "Delete Deal"
3. Confirmation dialog appears with deal name
4. User confirms → `DELETE /users/crm/deals/{deal_id}` called
5. On success → Deal removed from list, success toast shown
6. On error → Error toast with message

**Code Example:**
```typescript
// Three-dot menu (only for internal deals)
{deal.is_internal && (
  <MenuRoot>
    <MenuTrigger>
      <Icon as={MoreVertical} />
    </MenuTrigger>
    <MenuContent>
      <MenuItem onClick={() => handleDeleteClick(deal)}>
        <Icon as={Trash2} />
        Delete Deal
      </MenuItem>
    </MenuContent>
  </MenuRoot>
)}
```

### Testing:

1. ✅ Three-dot menu only appears for internal deals (`is_internal: true`)
2. ✅ External deals (`is_internal: false`) have no three-dot menu
3. ✅ Clicking Delete opens confirmation dialog
4. ✅ Confirmation dialog shows correct deal name
5. ✅ Cancel button closes dialog without action
6. ✅ Confirm delete calls API and removes deal from list
7. ✅ Success toast appears on successful deletion
8. ✅ Error toast appears if deletion fails

---

## Implementation (Step 4 - CRUD Operations: Edit Deal)

**Status:** 🔄 Ready for Frontend Implementation

**Date:** 2026-01-22

### Backend Implementation: ✅ Complete

The backend has complete edit functionality across multiple endpoints:

#### 1. Update Deal Basic Fields
**Endpoint:** `PATCH /users/crm/deals/{deal_id}`

**Editable Fields** (all optional, only send changed fields):
- `name` - Deal name (1-500 chars)
- `amount` - Deal value (>= 0, can be null)
- `currency` - 3-char ISO code (USD, INR, etc.)
- `close_date` - Target close date (ISO 8601)
- `expected_close_date` - Forecasted close date (ISO 8601)
- `description` - Deal notes/description
- `deal_type` - Classification (e.g., "New Business", "Renewal")
- `priority` - Priority level (e.g., "High", "Medium", "Low")
- `lead_source` - Lead source (e.g., "Inbound", "Outbound")

**Validation:**
- ✅ Only works for `is_internal: true` deals
- ✅ Returns 409 error for external CRM deals (HubSpot/Salesforce)
- ✅ All fields validated (min/max length, data types)
- ✅ Empty request returns 400 error

**Documentation:** `docs/API Documentation/crm-deals-api.md#4-update-deal`

#### 2. Move Deal Stage
**Endpoint:** `POST /users/crm/deals/{deal_id}/stage`

**Request:**
```json
{
  "stage_id": "uuid-of-target-stage"
}
```

**Behavior:**
- Validates stage belongs to same pipeline as deal
- Creates stage history record automatically
- Updates deal's `stage_id`, `stage_name`, and timestamps

**Documentation:** `docs/API Documentation/crm-deals-api.md#5-move-stage`

#### 3. Assign/Clear Owner
**Endpoint:** `POST /users/crm/deals/{deal_id}/owner`

**Request:**
```json
{
  "owner_user_id": "user-uuid"  // or null to clear
}
```

**Behavior:**
- Auto-syncs org users on first owner assignment (MVP mode)
- Validates user exists in CRM users table
- Can be set to `null` to unassign owner

**Documentation:** `docs/API Documentation/crm-deals-api.md#6-assign-owner`

#### 4. Backend Files
| File | What's Implemented |
|------|-------------------|
| `src/service/crm/internal/deal_write_service.py` | `update_deal()`, `move_stage()`, `assign_owner()` methods |
| `src/api/schemas/crm_deal_write_models.py` | `DealUpdateRequest`, `DealStageMoveRequest`, `DealOwnerAssignRequest` |
| `src/api/routers/crm/deals_handler.py` | All PATCH/POST endpoints registered and working |

---

### Frontend Implementation: 📋 TODO

Based on the current UI (see screenshots), here's the recommended implementation:

#### UI Pattern: Edit Icon in "Context" Card Header

The "Context" card in the deal detail page contains all the basic deal fields. Add edit functionality there:

**Visual Design:**
```
┌─────────────────────────────────────────────────────────┐
│  Context                                    [✏️ Edit]   │  ← Add pencil icon here
├─────────────────────────────────────────────────────────┤
│  SALES STAGE              DEAL VALUE                    │
│  Appointment Scheduled    N/A                           │
│  ...                                                     │
└─────────────────────────────────────────────────────────┘
```

**Edit Mode Behavior:**
```
┌─────────────────────────────────────────────────────────┐
│  Context                        [💾 Save] [✕ Cancel]    │
├─────────────────────────────────────────────────────────┤
│  SALES STAGE              DEAL VALUE                    │
│  [Dropdown ▼]            [$_____ USD▼]                  │
│                                                          │
│  CLOSE DATE              DEAL AGE                       │
│  [📅 Date Picker]        9 days (auto-calculated)       │
│  ...                                                     │
└─────────────────────────────────────────────────────────┘
```

#### Implementation Checklist

**1. Context Card Edit Mode**
- [ ] Add edit icon to Context card header (only if `deal.is_internal === true`)
- [ ] Toggle between view mode and edit mode on click
- [ ] Convert read-only fields to form inputs in edit mode
- [ ] Show Save/Cancel buttons in edit mode

**2. Form Inputs for Editable Fields**
- [ ] **Sales Stage**: Dropdown from `/users/crm/pipelines/:pipelineId/stages`
- [ ] **Deal Value**: Number input with currency dropdown
- [ ] **Close Date**: Date picker component
- [ ] **Expected Close Date**: Date picker component
- [ ] **Deal Type**: Dropdown or text input (e.g., "New Business", "Renewal", "Expansion")
- [ ] **Priority**: Dropdown (e.g., "High", "Medium", "Low")
- [ ] **Lead Source**: Dropdown or text input (e.g., "Inbound", "Outbound", "Referral")
- [ ] **Description**: Textarea (if shown in Context card)
- [ ] **Industry**: Dropdown or text input (if editable)

**3. Read-Only Fields** (Don't make these editable)
- Deal Age (calculated field)
- Last Activity (system-generated)
- Deal Health (AI-generated)

**4. API Integration**
- [ ] Create `useUpdateDeal()` mutation hook
- [ ] Handle field updates via `PATCH /users/crm/deals/{dealId}`
- [ ] Handle stage changes via `POST /users/crm/deals/{dealId}/stage`
- [ ] Handle owner changes via `POST /users/crm/deals/{dealId}/owner`
- [ ] Invalidate deal queries on success (`["crm_deals"]`, `["crm_deal", dealId]`)

**5. Validation & Error Handling**
- [ ] Validate required fields before save (e.g., name cannot be empty)
- [ ] Show loading state during save
- [ ] Show success toast on save
- [ ] Show error toast on failure
- [ ] Prevent editing if `is_internal === false` (show read-only message)

**6. User Experience**
- [ ] Auto-save on field blur OR explicit Save button (recommend explicit Save/Cancel)
- [ ] Warn user if they navigate away with unsaved changes
- [ ] Disable Edit button during save operation
- [ ] Refresh deal data after successful save

#### Frontend Files to Create/Update

**Create:**
```
src/modules/deals/hooks/
  ├── use-update-deal.ts          // PATCH mutation
  ├── use-move-deal-stage.ts      // POST stage mutation
  └── use-assign-deal-owner.ts    // POST owner mutation

src/modules/deals/components/
  └── DealContextCardEdit.tsx     // Edit mode for Context card
```

**Update:**
```
src/modules/deals/components/
  └── DealContextCard.tsx         // Add edit icon + toggle logic

src/modules/deals/types/
  └── deals.types.ts              // Add UpdateDealRequest type
```

#### Example Code Snippets

**Update Deal Hook:**
```typescript
export function useUpdateDeal() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async ({ dealId, updates }: { dealId: string; updates: UpdateDealRequest }) => {
      const response = await apiClient.patch(`/users/crm/deals/${dealId}`, updates);
      return response.data;
    },
    onSuccess: (data, variables) => {
      // Invalidate deal queries
      queryClient.invalidateQueries(['crm_deals']);
      queryClient.invalidateQueries(['crm_deal', variables.dealId]);
      toast.success('Deal updated successfully');
    },
    onError: (error: any) => {
      const message = error.response?.data?.message || 'Failed to update deal';
      toast.error(message);
    },
  });
}
```

**Context Card Edit Mode:**
```typescript
const [isEditing, setIsEditing] = useState(false);
const [editedData, setEditedData] = useState(deal);
const updateDeal = useUpdateDeal();

const handleSave = async () => {
  if (!deal.is_internal) {
    toast.error('Cannot edit external CRM deals');
    return;
  }

  await updateDeal.mutateAsync({
    dealId: deal.deal_id,
    updates: {
      amount: editedData.amount,
      close_date: editedData.close_date,
      // ... only include changed fields
    }
  });

  setIsEditing(false);
};

return (
  <Card>
    <CardHeader>
      <Text>Context</Text>
      {deal.is_internal && !isEditing && (
        <IconButton icon={<EditIcon />} onClick={() => setIsEditing(true)} />
      )}
      {isEditing && (
        <>
          <Button onClick={handleSave} isLoading={updateDeal.isPending}>Save</Button>
          <Button onClick={() => setIsEditing(false)}>Cancel</Button>
        </>
      )}
    </CardHeader>
    <CardBody>
      {isEditing ? (
        <EditMode data={editedData} onChange={setEditedData} />
      ) : (
        <ViewMode data={deal} />
      )}
    </CardBody>
  </Card>
);
```

---

### What's next (Phase 3):

1. ⬜ Probability override (0-100%)
2. ⬜ Forecast Category dropdown
3. ⬜ Tags input
4. ⬜ Industry dropdown
5. ⬜ Products/Competitors multi-select
6. ⬜ Contract Length field

---

### Step 4: Contacts and Companies CRUD

Business goal: Build the account and contact directory that deals link to.

**📄 API Documentation:**
- Contacts: `docs/API Documentation/crm-contacts-api.md` (To be created)
- Companies: `docs/API Documentation/crm-companies-api.md` (To be created)

APIs:

- `GET /users/crm/contacts` - List contacts with search/pagination
  📄 **Docs:** `docs/API Documentation/crm-contacts-api.md#1-list-contacts` (TBD)
- `POST /users/crm/contacts` - Create new contact
  📄 **Docs:** `docs/API Documentation/crm-contacts-api.md#2-create-contact` (TBD)
- `PATCH /users/crm/contacts/{contact_id}` - Update contact
  📄 **Docs:** `docs/API Documentation/crm-contacts-api.md#3-update-contact` (TBD)
- `DELETE /users/crm/contacts/{contact_id}` - Delete contact
  📄 **Docs:** `docs/API Documentation/crm-contacts-api.md#4-delete-contact` (TBD)
- `GET /users/crm/companies` - List companies with search/pagination
  📄 **Docs:** `docs/API Documentation/crm-companies-api.md#1-list-companies` (TBD)
- `POST /users/crm/companies` - Create new company
  📄 **Docs:** `docs/API Documentation/crm-companies-api.md#2-create-company` (TBD)
- `PATCH /users/crm/companies/{company_id}` - Update company
  📄 **Docs:** `docs/API Documentation/crm-companies-api.md#3-update-company` (TBD)
- `DELETE /users/crm/companies/{company_id}` - Delete company
  📄 **Docs:** `docs/API Documentation/crm-companies-api.md#4-delete-company` (TBD)

Frontend scope:

- Contacts directory with search.
- Companies directory with search.
- Create/edit modals for each.

Validation and behavior:

- Email format is validated; show inline errors on 422 responses.
- Company domain is normalized; show a hint when `www` is stripped.

Stop-and-test checklist:

- Create and update a contact; search returns it.
- Create and update a company; search returns it.
- Delete entries and confirm they no longer appear in list.

---

### Step 5: Deal Participants and Team Roles

Business goal: Attach the right people and accounts to a deal.

**📄 API Documentation:** `docs/API Documentation/crm-participants-api.md` (To be created)

APIs:

- `POST /users/crm/deals/{deal_id}/participants` - Add participant to deal
  📄 **Docs:** `docs/API Documentation/crm-participants-api.md#1-add-participant` (TBD)
- `DELETE /users/crm/deals/{deal_id}/participants/{participant_id}` - Remove participant
  📄 **Docs:** `docs/API Documentation/crm-participants-api.md#2-remove-participant` (TBD)
- `GET /users/crm/contacts` - Search contacts for participant selection
  📄 **Docs:** `docs/API Documentation/crm-contacts-api.md#1-list-contacts` (TBD)
- `GET /users/crm/companies` - Search companies for participant selection
  📄 **Docs:** `docs/API Documentation/crm-companies-api.md#1-list-companies` (TBD)
- `GET /users/crm/users` - Search users for team member selection
  📄 **Docs:** `docs/API Documentation/crm-users-api.md#1-search-users`

Frontend scope:

- Deal participants panel with tabs for Contacts, Companies, and Team.
- Role selection for team members (e.g., "SC") via `relationship_role`.

Validation and behavior:

- Enforce no duplicate participants in the UI before submit.
- If a participant is removed, show a confirmation prompt.

Stop-and-test checklist:

- Add contact, company, and user participants to a deal.
- Remove a participant and verify it disappears immediately.
- Add a team member with a role and verify it renders.

---

### Step 6: Activities and Insights (Incremental)

Business goal: Enable daily work (notes, tasks) and surface AI insights.

**📄 API Documentation:** (To be created per substep)

---

**Substep 6A: Notes**
📄 **Docs:** `docs/API Documentation/crm-notes-api.md` (To be created)

APIs:
- `GET /users/crm/deals/{deal_id}/notes` - List notes for deal
  📄 **Docs:** `docs/API Documentation/crm-notes-api.md#1-list-notes` (TBD)
- `POST /users/crm/deals/{deal_id}/notes` - Create note
  📄 **Docs:** `docs/API Documentation/crm-notes-api.md#2-create-note` (TBD)
- `PATCH /users/crm/deals/{deal_id}/notes/{note_id}` - Update note
  📄 **Docs:** `docs/API Documentation/crm-notes-api.md#3-update-note` (TBD)
- `DELETE /users/crm/deals/{deal_id}/notes/{note_id}` - Delete note
  📄 **Docs:** `docs/API Documentation/crm-notes-api.md#4-delete-note` (TBD)

UI: Notes panel with editor, inline validation, and @mention suggestions.
Check: create, edit, and delete notes; validate 422 handling.

---

**Substep 6B: Tasks**
📄 **Docs:** `docs/API Documentation/crm-tasks-api.md` (To be created)

APIs:
- `GET /users/crm/deals/{deal_id}/tasks` - List tasks for deal
  📄 **Docs:** `docs/API Documentation/crm-tasks-api.md#1-list-tasks` (TBD)
- `POST /users/crm/deals/{deal_id}/tasks` - Create task
  📄 **Docs:** `docs/API Documentation/crm-tasks-api.md#2-create-task` (TBD)
- `PATCH /users/crm/tasks/{task_id}` - Update task
  📄 **Docs:** `docs/API Documentation/crm-tasks-api.md#3-update-task` (TBD)
- `DELETE /users/crm/tasks/{task_id}` - Delete task
  📄 **Docs:** `docs/API Documentation/crm-tasks-api.md#4-delete-task` (TBD)

UI: Task list with status/priority filters and due date indicator.
Check: create, update status, delete; verify overdue flag.

---

**Substep 6C: Timeline and Meetings**
📄 **Docs:** `docs/API Documentation/crm-timeline-api.md` (To be created)
📄 **Docs:** `docs/API Documentation/crm-meetings-api.md` (To be created)

APIs:
- `GET /users/crm/deals/{deal_id}/timeline` - List timeline events
  📄 **Docs:** `docs/API Documentation/crm-timeline-api.md#1-list-timeline` (TBD)
- `GET /users/crm/deals/timeline/{timeline_event_id}` - Get timeline event detail
  📄 **Docs:** `docs/API Documentation/crm-timeline-api.md#2-get-timeline-event` (TBD)
- `GET /users/crm/deals/{deal_id}/meetings` - List meetings for deal
  📄 **Docs:** `docs/API Documentation/crm-meetings-api.md#1-list-meetings` (TBD)
- `POST /users/crm/deals/{deal_id}/meetings/{event_id}/jobs/{job_type}` - Start meeting job
  📄 **Docs:** `docs/API Documentation/crm-meetings-api.md#2-start-meeting-job` (TBD)
- `GET /users/crm/deals/{deal_id}/meetings/{event_id}/jobs/{job_id}/stream` - SSE job stream
  📄 **Docs:** `docs/API Documentation/crm-meetings-api.md#3-job-stream-sse` (TBD)
- `PATCH /users/crm/deals/{deal_id}/meetings/{event_id}/status` - Update meeting status
  📄 **Docs:** `docs/API Documentation/crm-meetings-api.md#4-update-meeting-status` (TBD)

UI: Timeline list with detail drawer; meeting cards with "Generate" actions.
Check: timeline list renders; meeting job starts and SSE updates UI.

---

**Substep 6D: Risks, Stakeholders, and Insights**
📄 **Docs:** `docs/API Documentation/crm-risks-blockers-api.md` (To be created)
📄 **Docs:** `docs/API Documentation/crm-stakeholders-api.md` (To be created)

Risks/Blockers APIs:
- `GET /users/crm/deals/{deal_id}/risks-blockers` - List risks/blockers
  📄 **Docs:** `docs/API Documentation/crm-risks-blockers-api.md#1-list-risks-blockers` (TBD)
- `POST /users/crm/deals/{deal_id}/risks-blockers` - Create risk/blocker
  📄 **Docs:** `docs/API Documentation/crm-risks-blockers-api.md#2-create-risk-blocker` (TBD)
- `PATCH /users/crm/deals/risks-blockers/{risk_blocker_id}` - Update risk/blocker
  📄 **Docs:** `docs/API Documentation/crm-risks-blockers-api.md#3-update-risk-blocker` (TBD)
- `POST /users/crm/deals/risks-blockers/{risk_blocker_id}/confirm|dismiss|resolve|reopen` - Actions
  📄 **Docs:** `docs/API Documentation/crm-risks-blockers-api.md#4-risk-blocker-actions` (TBD)
- `DELETE /users/crm/deals/risks-blockers/{risk_blocker_id}` - Delete risk/blocker
  📄 **Docs:** `docs/API Documentation/crm-risks-blockers-api.md#5-delete-risk-blocker` (TBD)

Stakeholders APIs:
- `GET /users/crm/deals/{deal_id}/stakeholders` - List stakeholders
  📄 **Docs:** `docs/API Documentation/crm-stakeholders-api.md#1-list-stakeholders` (TBD)
- `GET /users/crm/deals/{deal_id}/stakeholders/available-contacts` - Available contacts
  📄 **Docs:** `docs/API Documentation/crm-stakeholders-api.md#2-available-contacts` (TBD)
- `POST /users/crm/deals/{deal_id}/stakeholders/add-from-contacts` - Add from contacts
  📄 **Docs:** `docs/API Documentation/crm-stakeholders-api.md#3-add-from-contacts` (TBD)
- `POST /users/crm/deals/{deal_id}/stakeholders` - Create stakeholder
  📄 **Docs:** `docs/API Documentation/crm-stakeholders-api.md#4-create-stakeholder` (TBD)
- `POST /users/crm/deals/{deal_id}/stakeholders/{stakeholder_id}/approve|reject` - Approve/reject
  📄 **Docs:** `docs/API Documentation/crm-stakeholders-api.md#5-approve-reject-stakeholder` (TBD)
- `DELETE /users/crm/deals/{deal_id}/stakeholders/{stakeholder_id}` - Delete stakeholder
  📄 **Docs:** `docs/API Documentation/crm-stakeholders-api.md#6-delete-stakeholder` (TBD)

Stakeholder Insights APIs:
- `GET /users/crm/deals/{deal_id}/stakeholder-insights` - List insights
  📄 **Docs:** `docs/API Documentation/crm-stakeholders-api.md#7-list-stakeholder-insights` (TBD)
- `POST /users/crm/deals/{deal_id}/stakeholder-insights/{insight_id}/acknowledge|dismiss|resolve` - Insight actions
  📄 **Docs:** `docs/API Documentation/crm-stakeholders-api.md#8-insight-actions` (TBD)

UI: Panels for Risks/Blockers and Insights with status actions.
Check: list + action flows update status immediately.

---

## Feedback Template (Use After Each Step)

Please send back the following for each step:

- Step number:
- What worked:
- What broke (API or UI):
- Error messages / status codes:
- Missing data in response:
- UX friction or confusing behavior:
- Suggested changes:

---

## Notes and Known Limitations

- Default pipeline is auto-created at org creation; no setup flow needed.
- Pipeline editing (rename, reorder stages) is planned for future phases; UI should be read-only for now.
- Deal list does not include a `source_system` field; rely on backend write
  responses (409) to determine read-only state for external deals.
- User-created notes/tasks are allowed on external deals as internal annotations
  (backend enforces this policy).
