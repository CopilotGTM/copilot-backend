# Deal Risks & Blockers API

This API manages risks and blockers associated with CRM deals. Items can be either AI-extracted from communications (meetings, emails, Slack) or manually created by users.

**Base URL**: `/users/crm/deals`

---

## Overview

### What are Risks & Blockers?

| Type | Description | Example |
| --- | --- | --- |
| **RISK** | Potential issue that may impact the deal | "Budget approval pending Q2 review" |
| **BLOCKER** | Active impediment that must be resolved | "Security audit required before procurement" |

### Item Lifecycle

```
AI Extraction Flow:
AI_SUGGESTED → (confirm) → OPEN → IN_PROGRESS → RESOLVED
            ↘ (dismiss) → DISMISSED

Manual Creation Flow:
(create) → OPEN → IN_PROGRESS → RESOLVED
                             ↘ DISMISSED / WONT_FIX
```

### AI vs Manual Items

| Origin | Initial Status | Source |
| --- | --- | --- |
| **AI-extracted** | `AI_SUGGESTED` | Meeting transcripts, emails, Slack messages |
| **Manually created** | `OPEN` | User input via API |

> **Note**: AI extraction is triggered automatically when new meetings/emails are synced. There is no frontend trigger—extraction runs in the background.

---

## Response Envelope

All endpoints return the shared `GenericResponse` envelope:

```json
{
  "status": "SUCCESS | ERROR",
  "message": "Human-readable message",
  "data": { ... } | null
}
```

---

## Authentication

- Bearer token required (`Authorization: Bearer <access_token>`)
- Organisation/user/role are inferred from the token

---

## Rate Limiting

| Operation | Limit |
| --- | --- |
| Read (`GET`) | ~60/min, 1000/hour per user |
| Write (`POST/PATCH/DELETE`) | ~30/min, 500/hour per user |

On limit breach: `429 Too Many Requests` with `Retry-After` header.

---

## Data Shapes (TypeScript)

```ts
export type UUID = string;

// ============================================================================
// Enums
// ============================================================================

type RiskBlockerType = "RISK" | "BLOCKER";

type RiskBlockerStatus =
  | "AI_SUGGESTED"    // AI-identified, pending user review
  | "AI_DISMISSED"    // AI dismissed (low confidence)
  | "OPEN"            // Confirmed as active issue
  | "IN_PROGRESS"     // Being actively addressed
  | "RESOLVED"        // Issue resolved
  | "DISMISSED"       // User dismissed as not relevant
  | "WONT_FIX";       // Acknowledged but not addressing

type RiskBlockerSeverity = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";

type RiskBlockerCategory =
  // Technical
  | "TECHNICAL"
  | "INTEGRATION"
  | "SECURITY"
  | "PERFORMANCE"
  // Commercial
  | "BUDGET"
  | "TIMELINE"
  | "CONTRACT"
  // Stakeholder
  | "CHAMPION_RISK"
  | "STAKEHOLDER"
  | "COMPETITION"
  // Process
  | "INTERNAL_PROCESS"
  | "RESOURCE"
  // General
  | "OTHER";

type RiskBlockerSource =
  // Email
  | "EMAIL_GMAIL"
  | "EMAIL_OUTLOOK"
  // Meetings
  | "MEETING_GOOGLE"
  | "MEETING_OUTLOOK"
  | "TEAMS_MEETING"
  | "ZOOM_MEETING"
  // Messaging
  | "SLACK"
  | "TEAMS_CHAT"
  // CRM
  | "CRM_HUBSPOT"
  | "CRM_SALESFORCE"
  // Other
  | "MANUAL"
  | "SYSTEM"
  | "OTHER";

// ============================================================================
// Response Models
// ============================================================================

/** List item - optimized for table display */
export interface RiskBlockerListItem {
  risk_blocker_id: UUID;
  item_type: RiskBlockerType;
  status: RiskBlockerStatus;
  severity: RiskBlockerSeverity;
  
  // Content
  title: string;
  quote: string | null;           // Supporting quote from source
  
  // Source (for icon display)
  source_type: RiskBlockerSource;
  source_context: string | null;  // e.g., "Discovery Call - Dec 15"
  
  // Assignment
  owner_name: string | null;
  
  // AI metadata
  is_ai_generated: boolean;
}

/** Full detail view with audit trail */
export interface RiskBlockerDetail {
  risk_blocker_id: UUID;
  deal_id: UUID;
  item_type: RiskBlockerType;
  category: RiskBlockerCategory;
  severity: RiskBlockerSeverity;
  status: RiskBlockerStatus;
  
  // Content
  title: string;
  description: string | null;
  quote: string | null;
  
  // Source attribution
  source_type: RiskBlockerSource;
  source_record_id: string | null;  // ID of source meeting/email
  source_context: string | null;
  
  // AI metadata
  is_ai_generated: boolean;
  ai_confidence: number | null;     // 0.0 - 1.0
  ai_reasoning: string | null;
  extraction_run_id: UUID | null;
  
  // Assignment
  owner_name: string | null;
  owner_email: string | null;
  assigned_team: string | null;
  
  // Resolution
  resolution_notes: string | null;
  resolved_at: string | null;       // ISO8601
  resolved_by: string | null;
  
  // Provenance
  created_by_user_id: UUID | null;
  created_by_user_name: string | null;
  last_modified_by_user_id: UUID | null;
  last_modified_by_user_name: string | null;
  user_confirmed_at: string | null;
  
  // Timestamps
  identified_at: string;            // When the issue was identified
  created_at: string;
  updated_at: string;
  
  // Audit trail
  audit_trail: AuditEntry[];
  modification_count: number;
  unique_modifier_count: number;
}

export interface AuditEntry {
  timestamp: string;
  user_id: UUID | null;
  user_name: string | null;
  action: string;
  changes: Record<string, { old: unknown; new: unknown }>;
}

/** Summary statistics */
export interface RiskBlockerSummary {
  total_count: number;
  risks_count: number;
  blockers_count: number;
  ai_suggested_count: number;
  open_count: number;
  in_progress_count: number;
  resolved_count: number;
  critical_count: number;
  high_count: number;
}

/** Paginated list response */
export interface RiskBlockerListResponse {
  deal_id: UUID;
  items: RiskBlockerListItem[];
  total_count: number;
  cursor: string | null;
  summary: RiskBlockerSummary;
}
```

---

## 1) List Risks & Blockers

Retrieve a paginated list of risks and blockers for a deal.

- **Endpoint**: `GET /{deal_id}/risks-blockers`
- **Tags**: `risks-blockers`

### Query Parameters

| Name | Type | Default | Description |
| ---- | ---- | ------- | ----------- |
| `limit` | int (1-100) | 20 | Page size |
| `cursor` | string | null | Opaque pagination cursor |
| `item_type` | enum | null | Filter: `RISK` or `BLOCKER` |
| `status` | string | null | Comma-separated statuses: `OPEN,IN_PROGRESS,AI_SUGGESTED` |
| `severity` | enum | null | Filter: `CRITICAL`, `HIGH`, `MEDIUM`, `LOW` |
| `category` | enum | null | Filter by category |
| `include_resolved` | boolean | false | Include resolved/dismissed items |
| `sort` | `asc`\|`desc` | `desc` | Sort by creation date |

### Sample Request

```http
GET /users/crm/deals/6c3f98d5-2e6b-4a1c-8f8c-2e8f0211b0b9/risks-blockers?limit=20&status=OPEN,AI_SUGGESTED
Authorization: Bearer <token>
```

### Sample Response

```json
{
  "status": "SUCCESS",
  "message": "Risks and blockers retrieved successfully",
  "data": {
    "deal_id": "6c3f98d5-2e6b-4a1c-8f8c-2e8f0211b0b9",
    "items": [
      {
        "risk_blocker_id": "f9a6c1e1-0d56-4e2b-9c2d-4d3fb3d6c9a1",
        "item_type": "BLOCKER",
        "status": "AI_SUGGESTED",
        "severity": "HIGH",
        "title": "Security audit required before procurement",
        "quote": "We can't proceed until the security team completes their review...",
        "source_type": "MEETING_GOOGLE",
        "source_context": "Discovery Call - Dec 15",
        "owner_name": null,
        "is_ai_generated": true
      },
      {
        "risk_blocker_id": "a1b2c3d4-5678-9abc-def0-123456789abc",
        "item_type": "RISK",
        "status": "OPEN",
        "severity": "MEDIUM",
        "title": "Budget approval pending Q2 review",
        "quote": null,
        "source_type": "MANUAL",
        "source_context": null,
        "owner_name": "John Smith",
        "is_ai_generated": false
      }
    ],
    "total_count": 5,
    "cursor": "eyJvZmZzZXQiOjIw...",
    "summary": {
      "total_count": 5,
      "risks_count": 3,
      "blockers_count": 2,
      "ai_suggested_count": 2,
      "open_count": 2,
      "in_progress_count": 1,
      "resolved_count": 0,
      "critical_count": 0,
      "high_count": 2
    }
  }
}
```

---

## 2) Get Risk/Blocker Detail

Get full details of a specific risk or blocker including audit trail.

- **Endpoint**: `GET /risks-blockers/{risk_blocker_id}`
- **Tags**: `risks-blockers`

### Path Parameters

| Name | Type | Description |
| ---- | ---- | ----------- |
| `risk_blocker_id` | UUID | Risk/blocker identifier |

### Sample Response

```json
{
  "status": "SUCCESS",
  "message": "Risk/blocker retrieved successfully",
  "data": {
    "risk_blocker_id": "f9a6c1e1-0d56-4e2b-9c2d-4d3fb3d6c9a1",
    "deal_id": "6c3f98d5-2e6b-4a1c-8f8c-2e8f0211b0b9",
    "item_type": "BLOCKER",
    "category": "SECURITY",
    "severity": "HIGH",
    "status": "OPEN",
    
    "title": "Security audit required before procurement",
    "description": "The customer's security team must complete a vendor security assessment before the deal can proceed to procurement.",
    "quote": "We can't proceed until the security team completes their review, which typically takes 4-6 weeks.",
    
    "source_type": "MEETING_GOOGLE",
    "source_record_id": "evt_abc123",
    "source_context": "Discovery Call - Dec 15",
    
    "is_ai_generated": true,
    "ai_confidence": 0.92,
    "ai_reasoning": "Security review mentioned as hard requirement by IT Director with specific timeline impact.",
    "extraction_run_id": "run_xyz789",
    
    "owner_name": "Sarah Chen",
    "owner_email": "sarah@company.com",
    "assigned_team": "Security",
    
    "resolution_notes": null,
    "resolved_at": null,
    "resolved_by": null,
    
    "created_by_user_id": null,
    "created_by_user_name": null,
    "last_modified_by_user_id": "user_123",
    "last_modified_by_user_name": "John Smith",
    "user_confirmed_at": "2025-12-16T10:30:00Z",
    
    "identified_at": "2025-12-15T14:00:00Z",
    "created_at": "2025-12-15T15:30:00Z",
    "updated_at": "2025-12-16T10:30:00Z",
    
    "audit_trail": [
      {
        "timestamp": "2025-12-16T10:30:00Z",
        "user_id": "user_123",
        "user_name": "John Smith",
        "action": "confirmed",
        "changes": {
          "status": { "old": "AI_SUGGESTED", "new": "OPEN" }
        }
      },
      {
        "timestamp": "2025-12-16T10:35:00Z",
        "user_id": "user_123",
        "user_name": "John Smith",
        "action": "updated",
        "changes": {
          "owner_name": { "old": null, "new": "Sarah Chen" },
          "assigned_team": { "old": null, "new": "Security" }
        }
      }
    ],
    "modification_count": 2,
    "unique_modifier_count": 1
  }
}
```

---

## 3) Create Risk/Blocker

Manually create a new risk or blocker for a deal.

- **Endpoint**: `POST /{deal_id}/risks-blockers`
- **Tags**: `risks-blockers`

### Request Body

```json
{
  "item_type": "RISK",
  "title": "Budget approval pending Q2 review",
  "description": "CFO indicated budget decisions are frozen until Q2 planning is complete.",
  "category": "BUDGET",
  "severity": "MEDIUM",
  "owner_name": "John Smith",
  "owner_email": "john@company.com",
  "assigned_team": "Sales"
}
```

| Field | Type | Required | Description |
| ----- | ---- | -------- | ----------- |
| `item_type` | enum | Yes | `RISK` or `BLOCKER` |
| `title` | string (max 500) | Yes | Short descriptive title |
| `description` | string | No | Detailed description |
| `category` | enum | No | Category (default: `OTHER`) |
| `severity` | enum | No | Severity (default: `MEDIUM`) |
| `owner_name` | string | No | Assigned owner name |
| `owner_email` | string | No | Owner email |
| `assigned_team` | string | No | Assigned team |

### Sample Response

```json
{
  "status": "SUCCESS",
  "message": "Risk/blocker created successfully",
  "data": {
    "risk_blocker_id": "new-uuid-here",
    "status": "OPEN",
    "is_ai_generated": false,
    "source_type": "MANUAL",
    ...
  }
}
```

---

## 4) Update Risk/Blocker

Update fields of a risk/blocker. All changes are tracked in the audit trail.

- **Endpoint**: `PATCH /risks-blockers/{risk_blocker_id}`
- **Tags**: `risks-blockers`

### Request Body

All fields are optional. Only provided fields are updated.

```json
{
  "title": "Updated title",
  "description": "Updated description",
  "category": "TECHNICAL",
  "severity": "HIGH",
  "status": "IN_PROGRESS",
  "owner_name": "Jane Doe",
  "owner_email": "jane@company.com",
  "assigned_team": "Engineering"
}
```

### Sample Response

```json
{
  "status": "SUCCESS",
  "message": "Risk/blocker updated successfully",
  "data": { ... }
}
```

---

## 5) Confirm AI Suggestion

Confirm an AI-suggested item, moving it from `AI_SUGGESTED` to `OPEN` status.

- **Endpoint**: `POST /risks-blockers/{risk_blocker_id}/confirm`
- **Tags**: `risks-blockers`

### Sample Response

```json
{
  "status": "SUCCESS",
  "message": "Risk/blocker confirmed successfully",
  "data": {
    "risk_blocker_id": "...",
    "status": "OPEN",
    "user_confirmed_at": "2025-12-21T10:30:00Z",
    ...
  }
}
```

### Error Cases

| Status | Condition |
| ------ | --------- |
| `400` | Item is not in `AI_SUGGESTED` status |
| `404` | Risk/blocker not found |

---

## 6) Dismiss Risk/Blocker

Dismiss a risk/blocker (not relevant or false positive).

- **Endpoint**: `POST /risks-blockers/{risk_blocker_id}/dismiss`
- **Tags**: `risks-blockers`

### Sample Response

```json
{
  "status": "SUCCESS",
  "message": "Risk/blocker dismissed successfully",
  "data": {
    "risk_blocker_id": "...",
    "status": "DISMISSED",
    ...
  }
}
```

---

## 7) Resolve Risk/Blocker

Mark a risk/blocker as resolved with resolution notes.

- **Endpoint**: `POST /risks-blockers/{risk_blocker_id}/resolve`
- **Tags**: `risks-blockers`

### Request Body

```json
{
  "resolution_notes": "Security audit completed and approved. Vendor assessment passed with no critical findings."
}
```

| Field | Type | Required | Description |
| ----- | ---- | -------- | ----------- |
| `resolution_notes` | string (min 10 chars) | Yes | How the issue was resolved |

### Sample Response

```json
{
  "status": "SUCCESS",
  "message": "Risk/blocker resolved successfully",
  "data": {
    "risk_blocker_id": "...",
    "status": "RESOLVED",
    "resolution_notes": "Security audit completed...",
    "resolved_at": "2025-12-21T15:00:00Z",
    "resolved_by": "John Smith",
    ...
  }
}
```

---

## 8) Reopen Risk/Blocker

Reopen a previously resolved or dismissed risk/blocker.

- **Endpoint**: `POST /risks-blockers/{risk_blocker_id}/reopen`
- **Tags**: `risks-blockers`

### Sample Response

```json
{
  "status": "SUCCESS",
  "message": "Risk/blocker reopened successfully",
  "data": {
    "risk_blocker_id": "...",
    "status": "OPEN",
    "resolution_notes": null,
    "resolved_at": null,
    "resolved_by": null,
    ...
  }
}
```

### Error Cases

| Status | Condition |
| ------ | --------- |
| `400` | Item is not in `RESOLVED` or `DISMISSED` status |
| `404` | Risk/blocker not found |

---

## 9) Delete Risk/Blocker

Permanently delete a risk/blocker.

- **Endpoint**: `DELETE /risks-blockers/{risk_blocker_id}`
- **Tags**: `risks-blockers`

> **Warning**: This permanently removes the item. Consider using `dismiss` or `resolve` instead to preserve the audit trail.

### Sample Response

```json
{
  "status": "SUCCESS",
  "message": "Risk/blocker deleted successfully",
  "data": null
}
```

---

## Error Codes (Common)

| HTTP | When |
| ---- | ---- |
| `400` | Invalid request body, invalid status transition |
| `401` | Missing/invalid bearer token |
| `403` | Not authorized for this org/deal |
| `404` | Deal or risk/blocker not found |
| `429` | Rate limit exceeded |
| `500` | Unexpected server error |

---

## Frontend Integration Example

```typescript
// Types
interface RiskBlockerListItem { ... }
interface RiskBlockerDetail { ... }
interface RiskBlockerSummary { ... }

// ============================================================================
// API Client
// ============================================================================

class RisksBlockersAPI {
  constructor(private baseUrl: string, private token: string) {}

  private async request<T>(path: string, options?: RequestInit): Promise<T> {
    const res = await fetch(`${this.baseUrl}${path}`, {
      ...options,
      headers: {
        'Authorization': `Bearer ${this.token}`,
        'Content-Type': 'application/json',
        ...options?.headers,
      },
    });
    const json = await res.json();
    if (!res.ok) throw new Error(json.message);
    return json.data;
  }

  // List with filters
  async list(dealId: string, params?: {
    limit?: number;
    cursor?: string;
    item_type?: 'RISK' | 'BLOCKER';
    status?: string;
    severity?: string;
    include_resolved?: boolean;
  }): Promise<RiskBlockerListResponse> {
    const query = new URLSearchParams(params as Record<string, string>);
    return this.request(`/deals/${dealId}/risks-blockers?${query}`);
  }

  // Get detail
  async get(id: string): Promise<RiskBlockerDetail> {
    return this.request(`/risks-blockers/${id}`);
  }

  // Create
  async create(dealId: string, data: {
    item_type: 'RISK' | 'BLOCKER';
    title: string;
    description?: string;
    category?: string;
    severity?: string;
    owner_name?: string;
  }): Promise<RiskBlockerDetail> {
    return this.request(`/deals/${dealId}/risks-blockers`, {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  // Update
  async update(id: string, data: Partial<{
    title: string;
    description: string;
    category: string;
    severity: string;
    status: string;
    owner_name: string;
  }>): Promise<RiskBlockerDetail> {
    return this.request(`/risks-blockers/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    });
  }

  // Status transitions
  async confirm(id: string): Promise<RiskBlockerDetail> {
    return this.request(`/risks-blockers/${id}/confirm`, { method: 'POST' });
  }

  async dismiss(id: string): Promise<RiskBlockerDetail> {
    return this.request(`/risks-blockers/${id}/dismiss`, { method: 'POST' });
  }

  async resolve(id: string, notes: string): Promise<RiskBlockerDetail> {
    return this.request(`/risks-blockers/${id}/resolve`, {
      method: 'POST',
      body: JSON.stringify({ resolution_notes: notes }),
    });
  }

  async reopen(id: string): Promise<RiskBlockerDetail> {
    return this.request(`/risks-blockers/${id}/reopen`, { method: 'POST' });
  }

  // Delete
  async delete(id: string): Promise<void> {
    return this.request(`/risks-blockers/${id}`, { method: 'DELETE' });
  }
}

// ============================================================================
// Usage Example
// ============================================================================

const api = new RisksBlockersAPI('/api/v1/users/crm', accessToken);

// List open risks and AI suggestions
const { items, summary } = await api.list(dealId, {
  status: 'OPEN,AI_SUGGESTED',
  include_resolved: false,
});

// Render AI suggested panel
const aiSuggested = items.filter(i => i.status === 'AI_SUGGESTED');
aiSuggested.forEach(item => {
  console.log(`🤖 ${item.title}`);
  console.log(`   "${item.quote}"`);
  console.log(`   Source: ${item.source_context}`);
});

// Confirm an AI suggestion
await api.confirm(aiSuggested[0].risk_blocker_id);

// Create manual risk
await api.create(dealId, {
  item_type: 'RISK',
  title: 'Competitor pricing pressure',
  severity: 'HIGH',
  category: 'COMPETITION',
});

// Resolve a blocker
await api.resolve(blockerId, 'Security audit completed successfully.');
```

---

## Source Type Icons

Frontend should map `source_type` to appropriate icons:

| Source Type | Icon Suggestion |
| ----------- | --------------- |
| `EMAIL_GMAIL` | Gmail logo |
| `EMAIL_OUTLOOK` | Outlook logo |
| `MEETING_GOOGLE` | Google Calendar logo |
| `MEETING_OUTLOOK` | Outlook Calendar logo |
| `TEAMS_MEETING` | Microsoft Teams logo |
| `ZOOM_MEETING` | Zoom logo |
| `SLACK` | Slack logo |
| `TEAMS_CHAT` | Microsoft Teams logo |
| `CRM_HUBSPOT` | HubSpot logo |
| `CRM_SALESFORCE` | Salesforce logo |
| `MANUAL` | Pencil/Edit icon |
| `SYSTEM` | Gear/Settings icon |
| `OTHER` | Generic document icon |

---

## Notes

- **AI Extraction**: Runs automatically in the background when new meetings/emails are synced. No manual trigger endpoint.
- **Audit Trail**: All changes are tracked with user attribution, timestamps, and old/new values.
- **Pagination**: Use `cursor` from response to fetch next page. When `null`, no more pages exist.
- **Default Sort**: Newest first (`desc`). Use `sort=asc` for oldest first.
- **Resolved Items**: Hidden by default. Use `include_resolved=true` to see all items.
