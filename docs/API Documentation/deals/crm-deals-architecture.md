# CRM Deals API Architecture

> Version: 1.0  
> Last Updated: 2026-01-23  
> Reference: `docs/API Documentation/deals/crm-deals-api.md` (contract-level)

---

## 1) Purpose

This document explains **how** the CRM Deals APIs are structured and how they work together:
- Internal vs external deal handling
- Write flows and their endpoints
- Storage behavior for mass upload
- Separation of concerns across services

---

## 2) System Boundaries

### Internal CRM
- Deals created inside CopilotGTM
- Fully editable (create/update/stage/owner/participants/account)
- Uses org-scoped storage + DB schema

### External CRM (HubSpot/Salesforce)
- Deals are synced and **read-only**
- All write endpoints return HTTP 409

---

## 3) High-Level API Groups

### Deal Core
- `POST /users/crm/deals` → Create internal deal
- `GET /users/crm/deals` → List deals
- `GET /users/crm/deals/{deal_id}` → Deal detail
- `PATCH /users/crm/deals/{deal_id}` → Update core fields
- `DELETE /users/crm/deals/{deal_id}` → Soft delete

### Stage & Owner
- `POST /users/crm/deals/{deal_id}/stage` → Move stage (records history)
- `POST /users/crm/deals/{deal_id}/owner` → Assign/clear owner

### Account (Company)
- `PATCH /users/crm/deals/{deal_id}/account` → Update account info
  - Domain creates/links company
  - Name-only updates deal.company_name (no company record)

### Participants (Internal Team)
- `POST /users/crm/deals/{deal_id}/participants` → Add team members
- `PATCH /users/crm/deals/{deal_id}/participants/{participant_id}` → Update role
- `DELETE /users/crm/deals/{deal_id}/participants/{participant_id}` → Remove member

### Stakeholders (Contacts)
- `POST /users/crm/deals/{deal_id}/stakeholders` → Add stakeholder
- Auto-discovery from meetings/emails (backend jobs)

---

## 4) Write Flow Architecture

### Create Deal
1) Validate pipeline + stage
2) Resolve owner (internal CRM user)
3) Create CRMDeal
4) Write stage history
5) Optional:
   - create/link company from domain
   - add team members

### Edit Context (Core Fields)
- Only fields in `DealUpdateRequest`
- No account, participants, stakeholders, owner, or stage changes

### Account Update
- Uses domain → find/create company
- Attaches company participant
- Marks old company participant as deleted

### Participants
- Participants are separate records (`CRMDealParticipant`)
- Role updates are standalone (no deal update)

---

## 5) Mapping & Background Jobs

Stakeholder auto‑discovery runs only if:
- deal has an owner **or**
- deal has at least one participant

---

## 6) Mass Upload Architecture (CSV)

### Current State
- The previous CSV import implementation has been removed.
- The import route names remain reserved, but every import endpoint now returns `409 Conflict`.

### Endpoints
- `GET /users/crm/deals/import/template`
- `POST /users/crm/deals/import/upload-url`
- `POST /users/crm/deals/import/process`
- `GET /users/crm/deals/import/jobs/{job_id}/events`

### Import Behavior
- All endpoints above return the same disabled-state response while the flow is rebuilt:
  `409 Conflict` with message `Deal CSV import is coming soon. The previous import flow has been removed.`
- Create deals + participants + company link
- Stream progress + final result via SSE

---

## 7) Data Model Overview

Key tables (internal CRM):
- `crm_deals` → core deal record
- `crm_deal_stages` / `crm_pipelines` → pipeline configuration
- `crm_deal_stage_history` → stage change history
- `crm_deal_participants` → users/companies/contacts linked to deal
- `crm_companies` → account records
- `deal_stakeholders` → stakeholder list (separate flow)

---

## 8) Read/Write Rules

- External CRM deals: read-only (HTTP 409 on write)
- Internal CRM deals: editable via dedicated endpoints only
- Stakeholders are never edited via deal PATCH
