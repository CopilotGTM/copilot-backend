# CRM Pipelines API Documentation

> Version: 1.0
> Base URL: `/users/crm`
> Last Updated: 2026-01-22

---

## Overview

The Pipelines API provides access to CRM pipelines and stages. Pipelines define the sales process structure with ordered stages that deals move through.

**Important:** This API returns internal CRM pipelines only. External CRM integrations (HubSpot, Salesforce) have their own pipeline data.

---

## 1. List Pipelines

Returns all internal CRM pipelines for the organization.

### Endpoint

```
GET /users/crm/pipelines
```

### Request Headers

| Header | Required | Value |
|--------|----------|-------|
| Authorization | Yes | Bearer {token} |

### Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `include_stages` | boolean | false | Include stages in each pipeline response |

### Example Request

```
GET /users/crm/pipelines?include_stages=true
```

### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Pipelines retrieved",
  "data": [
    {
      "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
      "external_id": "internal-default",
      "name": "Default Pipeline",
      "label": "Default Pipeline",
      "is_default": true,
      "is_active": true,
      "display_order": 1,
      "stages": [
        {
          "stage_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
          "external_id": "qualification",
          "name": "Qualification",
          "label": "Qualification",
          "stage_state": "open",
          "probability": 10,
          "is_closed": false,
          "display_order": 1
        },
        {
          "stage_id": "6ba7b811-9dad-11d1-80b4-00c04fd430c8",
          "external_id": "discovery",
          "name": "Discovery",
          "label": "Discovery",
          "stage_state": "open",
          "probability": 25,
          "is_closed": false,
          "display_order": 2
        },
        {
          "stage_id": "6ba7b812-9dad-11d1-80b4-00c04fd430c8",
          "external_id": "proposal",
          "name": "Proposal",
          "label": "Proposal",
          "stage_state": "open",
          "probability": 50,
          "is_closed": false,
          "display_order": 3
        },
        {
          "stage_id": "6ba7b813-9dad-11d1-80b4-00c04fd430c8",
          "external_id": "negotiation",
          "name": "Negotiation",
          "label": "Negotiation",
          "stage_state": "open",
          "probability": 75,
          "is_closed": false,
          "display_order": 4
        },
        {
          "stage_id": "6ba7b814-9dad-11d1-80b4-00c04fd430c8",
          "external_id": "closed_won",
          "name": "Closed Won",
          "label": "Closed Won",
          "stage_state": "won",
          "probability": 100,
          "is_closed": true,
          "display_order": 5
        },
        {
          "stage_id": "6ba7b815-9dad-11d1-80b4-00c04fd430c8",
          "external_id": "closed_lost",
          "name": "Closed Lost",
          "label": "Closed Lost",
          "stage_state": "lost",
          "probability": 0,
          "is_closed": true,
          "display_order": 6
        }
      ]
    }
  ]
}
```

### Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `data` | array | List of pipelines |
| `data[].pipeline_id` | UUID | Pipeline ID (use for deal creation) |
| `data[].external_id` | string | External reference ID |
| `data[].name` | string | Pipeline name |
| `data[].label` | string | Display label |
| `data[].is_default` | boolean | Whether this is the default pipeline |
| `data[].is_active` | boolean | Whether pipeline is active |
| `data[].display_order` | integer | Sort order for display |
| `data[].stages` | array | Stages (only if `include_stages=true`) |

### Stage Fields

| Field | Type | Description |
|-------|------|-------------|
| `stage_id` | UUID | Stage ID (use for deal creation/move) |
| `external_id` | string | External reference ID |
| `name` | string | Stage name |
| `label` | string | Display label |
| `stage_state` | string | State: `open`, `won`, or `lost` |
| `probability` | number | Win probability (0-100) |
| `is_closed` | boolean | Whether this is a closed stage |
| `display_order` | integer | Sort order for display |

### Error Responses

| Status | Description |
|--------|-------------|
| 401 | Unauthorized - Invalid or missing token |
| 429 | Rate limited - Too many requests |
| 500 | Internal server error |

---

## 2. Get Pipeline Stages

Returns stages for a specific pipeline.

### Endpoint

```
GET /users/crm/pipelines/{pipeline_id}/stages
```

### Path Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `pipeline_id` | UUID | Pipeline UUID |

### Request Headers

| Header | Required | Value |
|--------|----------|-------|
| Authorization | Yes | Bearer {token} |

### Example Request

```
GET /users/crm/pipelines/550e8400-e29b-41d4-a716-446655440000/stages
```

### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Stages retrieved",
  "data": [
    {
      "stage_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
      "external_id": "qualification",
      "name": "Qualification",
      "label": "Qualification",
      "stage_state": "open",
      "probability": 10,
      "is_closed": false,
      "display_order": 1
    },
    {
      "stage_id": "6ba7b811-9dad-11d1-80b4-00c04fd430c8",
      "external_id": "discovery",
      "name": "Discovery",
      "label": "Discovery",
      "stage_state": "open",
      "probability": 25,
      "is_closed": false,
      "display_order": 2
    }
  ]
}
```

### Response Fields

Same as stage fields in List Pipelines response.

### Error Responses

| Status | Description |
|--------|-------------|
| 401 | Unauthorized - Invalid or missing token |
| 404 | Pipeline not found |
| 429 | Rate limited - Too many requests |
| 500 | Internal server error |

---

## Frontend Usage

### React Query Hook Example

```typescript
import { useQuery } from '@tanstack/react-query';
import { crmApiClient } from './crm.client';

interface PipelineStageItem {
  stage_id: string;
  external_id: string;
  name: string;
  label: string | null;
  stage_state: string;
  probability: number | null;
  is_closed: boolean;
  display_order: number | null;
}

interface PipelineItem {
  pipeline_id: string;
  external_id: string;
  name: string;
  label: string | null;
  is_default: boolean;
  is_active: boolean;
  display_order: number | null;
  stages: PipelineStageItem[];
}

export function usePipelines(options?: { enabled?: boolean; includeStages?: boolean }) {
  const { enabled = true, includeStages = true } = options ?? {};

  return useQuery({
    queryKey: ['crm_pipelines', { includeStages }],
    queryFn: async () => {
      const params = new URLSearchParams();
      if (includeStages) params.set('include_stages', 'true');

      const { data } = await crmApiClient.get<{ data: PipelineItem[] }>('/pipelines', { params });
      return data.data ?? [];
    },
    enabled,
    staleTime: 5 * 60 * 1000,  // 5 minutes
    gcTime: 10 * 60 * 1000,    // 10 minutes
  });
}

// Helper to get default pipeline
export function getDefaultPipeline(pipelines: PipelineItem[]): PipelineItem | undefined {
  return pipelines.find(p => p.is_default) ?? pipelines[0];
}

// Helper to get first stage of a pipeline
export function getFirstStage(pipeline: PipelineItem): PipelineStageItem | undefined {
  if (!pipeline.stages?.length) return undefined;
  return [...pipeline.stages].sort((a, b) =>
    (a.display_order ?? 0) - (b.display_order ?? 0)
  )[0];
}
```

### Stage Dropdown Example

```tsx
function StageSelect({ stages, value, onChange }) {
  const sortedStages = [...stages].sort((a, b) =>
    (a.display_order ?? 0) - (b.display_order ?? 0)
  );

  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}>
      {sortedStages.map(stage => (
        <option key={stage.stage_id} value={stage.stage_id}>
          {stage.label || stage.name} ({stage.probability}%)
        </option>
      ))}
    </select>
  );
}
```

---

## TypeScript Types

```typescript
interface PipelineStageItem {
  stage_id: string;
  external_id: string;
  name: string;
  label: string | null;
  stage_state: 'open' | 'won' | 'lost';
  probability: number | null;
  is_closed: boolean;
  display_order: number | null;
}

interface PipelineItem {
  pipeline_id: string;
  external_id: string;
  name: string;
  label: string | null;
  is_default: boolean;
  is_active: boolean;
  display_order: number | null;
  stages: PipelineStageItem[];
}

interface PipelinesResponse {
  status: 'SUCCESS' | 'ERROR';
  message: string;
  data: PipelineItem[];
}

interface StagesResponse {
  status: 'SUCCESS' | 'ERROR';
  message: string;
  data: PipelineStageItem[];
}
```

---

## Best Practices

1. **Use `include_stages=true`**: Fetch stages with pipelines in a single call to reduce network requests
2. **Cache aggressively**: Pipelines rarely change; use 5+ minute staleTime
3. **Sort by display_order**: Always sort stages by `display_order` for correct sequence
4. **Default pipeline**: Use `is_default: true` to find the default pipeline, fallback to first item
5. **Stage states**: Use `stage_state` to color-code closed stages (won = green, lost = red)

---

## Related Documents

- `crm-deals-api.md` - Deals CRUD (uses `pipeline_id` and `stage_id` from this API)
- `crm-settings-api.md` - Settings/Options API
