# CRM Contacts & Accounts Design

**Date:** 2026-04-03
**Status:** Draft
**Scope:** Backend APIs + DB migration + Frontend pages for Contacts and Accounts

---

## Overview

CopilotGTM acts as an agile CRM — not just a viewer of HubSpot/Salesforce data. This design adds standalone Contacts and Accounts pages, restructures navigation under a "CRM" group, and elevates enrichment from deal-scoped stakeholders to the contact level.

**Core principle:** Minimize manual work. Email domain auto-resolves accounts. Enrichment fills profile data. AI intelligence layers on top later.

---

## 1. Navigation Restructure

**Current sidebar:**
```
Chat
Deals        ← top-level
Tasks
Knowledge Hub
Integrations
Reminders
Analytics
```

**New sidebar:**
```
Chat
▾ CRM         ← collapsible group, icon: Building2 (lucide-react)
    Deals      ← /deals (unchanged)
    Contacts   ← /contacts (new)
    Accounts   ← /accounts (new)
Tasks
Knowledge Hub
Integrations
Reminders
Analytics
```

**Implementation:**
- Extend `LinkItemProps` to support `children` (sub-items)
- New `NavGroup` component — click parent toggles expand/collapse with chevron rotation
- Collapsed sidebar: CRM icon shows, hover reveals popover with 3 sub-items
- Expand/collapse state persisted in localStorage
- Clicking "CRM" parent expands the group (does not navigate anywhere)

---

## 2. Database Migration

### crm_contacts — new columns

| Column | Type | Default | Purpose |
|--------|------|---------|---------|
| `contact_research` | TEXT | NULL | Markdown profile from LinkedIn/enrichment |
| `last_researched_at` | TIMESTAMPTZ | NULL | When research was last completed |
| `research_status` | VARCHAR(20) | NULL | pending / completed / failed |

### crm_companies — new columns

| Column | Type | Default | Purpose |
|--------|------|---------|---------|
| `company_research` | TEXT | NULL | Markdown profile from company enrichment |
| `last_researched_at` | TIMESTAMPTZ | NULL | When research was last completed |
| `research_status` | VARCHAR(20) | NULL | pending / completed / failed |

### Enrichment flow change

- New contact enrichment writes to `CRMContact.contact_research`
- When a contact is added to a deal as stakeholder, the enriched profile is available via `contact_id` FK
- Existing `DealStakeholder.stakeholder_research` remains for deal-specific context overlay
- No migration of existing stakeholder research data (deferred)

---

## 3. Backend API Endpoints

### Contacts (`/users/crm/contacts`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | List contacts — search by name/email/company, filter by account/source, paginate |
| POST | `/` | Create contact — email (required) + name (required), auto-resolve account from domain |
| GET | `/{contact_id}` | Contact detail with deal involvement list |
| PATCH | `/{contact_id}` | Update contact fields |
| POST | `/{contact_id}/enrich` | Trigger LinkedIn research at contact level |
| POST | `/{contact_id}/add-to-deal` | Add contact as stakeholder on a deal (body: `{deal_id}`) |

### Accounts (`/users/crm/accounts`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | List accounts — search by name/domain, filter by industry/lifecycle, paginate |
| POST | `/` | Create account — name (required) + domain (optional) |
| GET | `/{company_id}` | Account detail with contacts list + deals list + pipeline value |
| PATCH | `/{company_id}` | Update account fields |
| GET | `/{company_id}/contacts` | List contacts belonging to this account |
| GET | `/{company_id}/deals` | List deals involving this account |
| POST | `/{company_id}/enrich` | Trigger company research/enrichment |

### Key backend logic

- **Auto account resolution on contact create:** Extract domain from email → query `crm_companies` by domain → link existing or create new CRMCompany
- **Internal CRM integration:** Locally created contacts/accounts get the internal CRM `integration_id`, with a separately generated `external_id`
- **Deal involvement query (contacts):** Join `deal_stakeholders` on `contact_id` to list all deals a contact participates in
- **Deal involvement query (accounts):** Join `crm_deals` on `company_external_id` to list all deals for an account
- **Pipeline value aggregation:** Sum `crm_deals.amount` where `deal_status = 'open'` for an account
- **Organisation isolation:** All queries scoped by `organisation_id`

---

## 4. Service Layer

### ContactsService (`src/service/crm/contacts/contacts_service.py`)

| Method | Purpose |
|--------|---------|
| `list_contacts()` | Paginated list with search/filter, annotated with deal count per contact |
| `create_contact()` | Create CRMContact, auto-resolve account from email domain, assign internal CRM integration_id |
| `get_contact_detail()` | Contact profile + deal involvement (join through deal_stakeholders) |
| `update_contact()` | Update editable fields |
| `enrich_contact()` | Dispatch LinkedIn research task, write results to `contact_research` |
| `add_contact_to_deal()` | Creates CRMDealParticipant + DealStakeholder on target deal (delegates to existing deal_participant_service) |

### AccountsService (`src/service/crm/accounts/accounts_service.py`)

| Method | Purpose |
|--------|---------|
| `list_accounts()` | Paginated list with search/filter, annotated with contact count + deal count + pipeline value |
| `create_account()` | Create CRMCompany, auto-link existing contacts by email domain match |
| `get_account_detail()` | Company profile + contacts list + deals list |
| `update_account()` | Update editable fields |
| `get_account_contacts()` | Contacts belonging to this account |
| `get_account_deals()` | Deals involving this account |
| `enrich_account()` | Dispatch company research task |

### Patterns followed

- Fresh session pattern — acquire session on demand, release before external calls
- Enrichment dispatches async Celery tasks (on existing workers)
- Organisation isolation enforced on all queries
- New services call into existing services where appropriate (e.g., `add_contact_to_deal` delegates to `deal_participant_service.add_participants()`)

---

## 5. Frontend — Contacts Page

**Route:** `/contacts`

**Layout:** Search bar + filters at top, table below (consistent with Deals list page).

### Top bar

- Search input (by name, email, company)
- Filter by: Account, Job Title, Source (CRM/Internal/Discovery)
- "Create Contact" button (top-right)

### Table columns

| Column | Content |
|--------|---------|
| Name | Avatar + full name, clickable → contact detail panel |
| Email | Email address |
| Job Title | Job title |
| Account | Company name, clickable → navigates to account detail on Accounts page |
| Deals | Badge with count, clickable → popover listing deal names |
| Source | Badge — CRM synced / Internal / Calendar / Email discovery |
| Actions | "Enrich" button, "Add to Deal" button |

### Create Contact modal

- Fields: Email (required), Name (required)
- On submit: Backend auto-resolves account from domain
- Modal closes, toast confirms, new contact appears in list

### Contact Detail (slide-over panel)

- Profile: Name, email, phone, job title, LinkedIn URL, account link
- Research profile (markdown rendered from `contact_research`)
- Deal involvement: List of deals where this contact is a stakeholder, showing role/stance per deal
- "Add to Deal" and "Enrich" buttons available here too

### Pagination

Same pattern as Deals list — offset-based with page size selector.

---

## 6. Frontend — Accounts Page

**Route:** `/accounts`

**Layout:** Same pattern as Contacts — search + filters + table.

### Top bar

- Search input (by name, domain)
- Filter by: Industry, Lifecycle Stage
- "Create Account" button (top-right)

### Table columns

| Column | Content |
|--------|---------|
| Company | Logo placeholder + name, clickable → account detail panel |
| Domain | Domain |
| Industry | Industry |
| Contacts | Count badge, clickable → popover listing contact names |
| Deals | Count badge, clickable → popover listing deal names |
| Pipeline Value | Sum of `amount` across all open deals for this account |

### Create Account modal

- Fields: Company Name (required), Domain (optional)
- On submit: Creates CRMCompany. If domain provided, auto-links existing contacts with matching email domain

### Account Detail (slide-over panel)

- Company profile: Name, domain, website, industry, employee count, revenue, LinkedIn URL
- Contacts at this account: List of CRMContacts with deal count + "Add to Deal" per contact
- Deals involving this account: List of CRMDeals with stage, amount, owner
- Total pipeline value and deal count summary at top
- "Enrich" button for company research

---

## 7. Frontend — Module Structure

### New modules

```
src/modules/contacts/
├── api/
│   └── contacts.api.ts
├── hooks/
│   ├── use-contacts.ts
│   ├── use-contact-detail.ts
│   ├── use-create-contact.ts
│   └── use-enrich-contact.ts
├── types/
│   └── contacts.types.ts
└── index.ts

src/modules/accounts/
├── api/
│   └── accounts.api.ts
├── hooks/
│   ├── use-accounts.ts
│   ├── use-account-detail.ts
│   ├── use-create-account.ts
│   └── use-enrich-account.ts
├── types/
│   └── accounts.types.ts
└── index.ts
```

### New pages

```
src/app/(root)/(dashboard)/contacts/page.tsx
src/app/(root)/(dashboard)/accounts/page.tsx
```

### New components

```
src/components/contacts/
├── contacts-table.tsx
├── create-contact-modal.tsx
├── contact-detail-panel.tsx
└── add-to-deal-modal.tsx

src/components/accounts/
├── accounts-table.tsx
├── create-account-modal.tsx
└── account-detail-panel.tsx
```

### Modified components

```
src/components/layout/sidebar/
├── nav-group.tsx               # NEW — collapsible sidebar group
└── sidebar.tsx                 # MODIFIED — CRM group replaces Deals item
```

### UI patterns

- shadcn `DataTable` for tables
- shadcn `Sheet` for slide-over detail panels
- shadcn `Dialog` for create/add-to-deal modals
- React Query for data fetching
- Tailwind styling consistent with existing pages

---

## 8. Deferred (Later Phases)

| Item | Reason |
|------|--------|
| AI-surfaced insights on contacts/accounts pages | Layer on top once base views exist |
| Smart deal suggestions in "Add to Deal" | Needs base flow working first |
| Stakeholder research → contact research data migration | Existing stakeholders keep working as-is |
| Account enrichment implementation (UI + worker logic) | Endpoint created, implementation later |
| Dedicated contacts sub-view under account detail page | Navigate to contacts page filtered by account for now |
| Bi-directional CRM sync (push to HubSpot/Salesforce) | Future phase |
