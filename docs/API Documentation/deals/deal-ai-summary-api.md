# Deal AI Summary API Documentation

> Version: 1.0
> Base URL: `/users/crm`
> Last Updated: 2026-01-22

---

## Overview

This document covers AI-powered deal insights and analytics endpoints including:
- Deal statistics and aggregations
- AI-generated deal summaries
- Real-time SSE streaming for summary generation

---

## Response Envelope

All endpoints return the shared `GenericResponse` envelope.

```json
{
  "status": "SUCCESS | ERROR",
  "message": "Human-readable message",
  "data": { ... } | null
}
```

---

## Authentication

- Required: Bearer token (`Authorization: Bearer <access_token>`)
- User identity (organisation, user, role) resolved from the token; clients never pass these explicitly

---

## Rate Limiting

- Write endpoints (`POST`): ~15/min, 100/hour per user
- Read endpoints (`GET`): ~60/min, 1000/hour per user
- On limit breach: `429 Too Many Requests` with `Retry-After`

---

## 1. Get Deal Statistics

### Endpoint

```http
GET /deals/stats
```

### Description

Returns aggregate statistics for all CRM deals in the organisation. Useful for dashboard widgets and overview cards.

### Authentication

Required

### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Statistics retrieved successfully",
  "data": {
    "total": 1247,
    "by_status": {
      "open": 823,
      "won": 312,
      "lost": 98,
      "closed": 14
    },
    "by_pipeline": {
      "Sales Pipeline": 945,
      "Partner Pipeline": 234,
      "Enterprise Pipeline": 68
    },
    "total_amount": 45678900.50,
    "average_amount": 36621.34
  }
}
```

### Response Fields

| Field            | Type           | Description                                     |
| ---              | ---            | ---                                               |
| `total`          | integer        | Total count of active deals (excludes soft-deleted)  |
| `by_status`      | object         | Count grouped by deal status                         |
| `by_pipeline`    | object         | Count grouped by pipeline name                       |
| `total_amount`   | number \| null | Sum of all non-null deal amounts (`0` is preserved; `null` only when no deal has an amount) |
| `average_amount` | number \| null | Average of non-null deal amounts (`0` is preserved; `null` only when no deal has an amount) |

### Notes

- Amounts are calculated from deals with non-null `amount` values
- Pipelines without names are excluded from `by_pipeline`
- Statistics exclude soft-deleted deals
- Currency is not normalized; `total_amount` and `average_amount` mix currencies

### Error Responses

- `429 Too Many Requests` — rate limit exceeded
- `500 Internal Server Error` — unexpected error

### Example cURL

```bash
curl -X GET "$BASE_URL/deals/stats" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json"
```

### Example TypeScript

```typescript
async function getDealsStats(): Promise<DealsStats> {
  const response = await fetch(`${BASE_URL}/deals/stats`, {
    headers: {
      'Authorization': `Bearer ${token}`,
      'Accept': 'application/json'
    }
  });

  const envelope: ApiEnvelope<DealsStats> = await response.json();
  if (envelope.status === 'ERROR') throw new Error(envelope.message);
  return envelope.data!;
}
```

---

## 2. Generate Deal Summary (AI)

### Endpoint

```http
POST /deals/{deal_id}/deal-summary/generate
```

### Description

Initiates an AI-powered summary generation for a deal. This analyzes ALL meeting transcripts associated with the deal to produce a comprehensive deal summary including:

- Overall deal narrative and progression
- Key themes across meetings
- Relationship timeline
- Critical decisions made
- Outstanding items and risks
- Recommended next steps

The operation is **idempotent** - if a summary job is already running for this deal, the existing job is returned instead of creating a new one.

### Authentication

Required

### Path Parameters

| Parameter | Type | Required | Description                 |
| --- | --- | --- | --- |
| `deal_id` | UUID | Yes      | Internal deal UUID          |

### Success Response (202 Accepted)

```json
{
  "status": "SUCCESS",
  "message": "Deal summary generation initiated",
  "data": {
    "job_id": "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d",
    "job_type": "deal_summary",
    "status": "PENDING"
  }
}
```

### Response When Job Already Running

```json
{
  "status": "SUCCESS",
  "message": "Deal summary generation already in progress",
  "data": {
    "job_id": "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d",
    "job_type": "deal_summary",
    "status": "IN_PROGRESS"
  }
}
```

### Response Fields

| Field      | Type   | Description                                                |
| --- | --- | --- |
| `job_id`   | UUID   | Job ID to use for SSE stream subscription                  |
| `job_type` | string | Type of job (`deal_summary`)                               |
| `status`   | string | Job status: `PENDING`, `IN_PROGRESS`, `COMPLETED`, `FAILED` |

### Error Responses

- `404 Not Found` — deal does not exist
- `429 Too Many Requests` — rate limit exceeded
- `500 Internal Server Error` — failed to schedule job

### Example cURL

```bash
curl -X POST "$BASE_URL/deals/f47ac10b-58cc-4372-a567-0e02b2c3d479/deal-summary/generate" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json"
```

### Example TypeScript

```typescript
interface DealJobResponse {
  job_id: string;
  job_type: string;
  status: string;
}

async function generateDealSummary(dealId: string): Promise<DealJobResponse> {
  const response = await fetch(
    `${BASE_URL}/deals/${dealId}/deal-summary/generate`,
    {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json'
      }
    }
  );

  const envelope: ApiEnvelope<DealJobResponse> = await response.json();
  if (envelope.status === 'ERROR') throw new Error(envelope.message);
  return envelope.data!;
}
```

---

## 3. Deal Summary SSE Stream

### Endpoint

```http
GET /deals/{deal_id}/deal-summary/stream/{job_id}
```

### Description

Server-Sent Events (SSE) stream for real-time deal summary job progress and results.

Subscribe to this endpoint with the `job_id` returned from the generate endpoint. The stream emits progress events during generation and the final result when complete.

**Reconnection Support**: If the user disconnects and reconnects, the stream resumes from the current state or delivers the cached result if already completed.

### Authentication

Required

### Path Parameters

| Parameter      | Type | Required | Description                                     |
| ---            | ---  | ---      | ---                                              |
| `deal_id`      | UUID | Yes      | Internal deal UUID                                    |
| `job_id`       | UUID | Yes      | Job ID from POST `/deal-summary/generate` response    |

### SSE Event Types

| Event Type  | Description                                              |
| --- | --- |
| `ready`     | Initial event when stream connects, contains job_id      |
| `ping`      | Heartbeat (every ~25s) to keep connection alive          |
| `frame`     | Progress updates, content chunks, or finalize signal     |
| `complete`  | Job finished successfully - stream will close            |
| `error`     | Error occurred - check `reconnect` flag                  |
| `timeout`   | Stream timeout (5 min default) - reconnect if needed     |

### Frame Types (within `frame` event)

| Frame Type       | Description                                         |
| --- | --- |
| `progress.update` | Progress updates with message, stage, and percentage |
| `answer.replace` | Final result content (full summary data)            |
| `error`          | Error during processing                             |
| `finalize`       | Signals job completion, stream will close           |

### SSE Event Format

```text
event: ready
data: {"job_id": "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"}

event: frame
data: {"type": "progress.update", "message": "Loading deal context...", "stage": "context", "progress": 0.1, ...}

event: frame
data: {"type": "progress.update", "message": "Found 5 meetings. Fetching transcripts...", "stage": "transcripts", "progress": 0.25, ...}

event: frame
data: {"type": "progress.update", "message": "Analyzing 5 meeting transcripts...", "stage": "analysis", "progress": 0.6, ...}

event: frame
data: {"type": "answer.replace", "content": {...summaryData}, "job_type": "deal_summary", "job_id": "...", ...}

event: frame
data: {"type": "finalize", "reason": "done", ...}

event: complete
data: {"job_id": "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"}
```

### Progress Update Frame Structure

```json
{
  "type": "progress.update",
  "message": "Loading deal context...",
  "stage": "context",
  "progress": 0.1,
  "sequence": 0,
  "timestamp": 1733234567.123,
  "job_type": "deal_summary",
  "job_id": "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"
}
```

### Progress Stages

| Stage            | Progress | Description                    |
| ---              | ---      | ---                              |
| `context`        | 0.10     | Loading deal context                |
| `stakeholders`   | 0.15     | Loading stakeholders                |
| `meetings`       | 0.20     | Loading meeting history             |
| `transcripts`    | 0.25     | Starting transcript fetch           |
| `transcript_fetch` | 0.25-0.60 | Fetching individual transcripts    |
| `analysis`       | 0.60     | Analyzing transcripts               |
| `generation`     | 0.70     | Generating AI summary               |
| `saving`         | 0.90     | Saving results to database          |

### Answer Replace Frame Structure (Final Result)

The `answer.replace` frame contains the full summary data in the `content` field:

```json
{
  "type": "answer.replace",
  "content": {
    "schema_version": "1.0",
    "generated_at": "2025-12-03T10:30:00Z",
    "data": {
      "deal_summary": "Enterprise software license deal with Acme Corp. Strong engagement across 5 meetings with clear technical fit; security review pending.",
      "deal_health": "HEALTHY",
      "deal_health_reason": "Three meetings in the last two weeks with decision-maker present. Pending security questionnaire is the only open item; no negative sentiment detected."
    }
  },
  "sequence": 8,
  "timestamp": 1733234567.123,
  "job_type": "deal_summary",
  "job_id": "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"
}
```

**Notes:**

- The `content` field contains the same structure as `deal_summary_data` in the Deal Detail response. Frontend should extract this from `frame.content` when `frame.type === "answer.replace"`.
- `deal_health_reason` is captured but **not displayed in the UI** for the current release.
- For deals with older summaries (or no summary generated), `deal_health` and `deal_health_reason` will be `null`.

### Example TypeScript

```typescript
function subscribeToDealSummaryStream(
  dealId: string,
  jobId: string,
  callbacks: {
    onProgress?: (progress: number, stage: string) => void;
    onChunk?: (content: string) => void;
    onComplete?: (result: DealSummaryResult) => void;
    onError?: (error: string) => void;
  }
): EventSource {
  const eventSource = new EventSource(
    `${BASE_URL}/deals/${dealId}/deal-summary/stream/${jobId}`,
    {
      // Note: For auth, you may need to use fetch with ReadableStream instead
      // or pass token via query param if your backend supports it
    }
  );

  eventSource.addEventListener('progress', (event) => {
    const data = JSON.parse(event.data);
    callbacks.onProgress?.(data.progress, data.stage);
  });

  eventSource.addEventListener('chunk', (event) => {
    const data = JSON.parse(event.data);
    callbacks.onChunk?.(data.content);
  });

  eventSource.addEventListener('complete', (event) => {
    const data = JSON.parse(event.data);
    callbacks.onComplete?.(data.result);
    eventSource.close();
  });

  eventSource.addEventListener('error', (event) => {
    if (event.data) {
      const data = JSON.parse(event.data);
      callbacks.onError?.(data.error);
    }
    eventSource.close();
  });

  return eventSource;
}

// Usage example
const job = await generateDealSummary(dealId);

const stream = subscribeToDealSummaryStream(dealId, job.job_id, {
  onProgress: (progress, stage) => {
    console.log(`${progress}% - ${stage}`);
    updateProgressBar(progress);
  },
  onChunk: (content) => {
    appendToSummaryPreview(content);
  },
  onComplete: (result) => {
    displayFinalSummary(result);
  },
  onError: (error) => {
    showErrorMessage(error);
  }
});
```

### Reconnection Pattern

If the user navigates away and returns, check `active_deal_jobs` in the deal detail response:

```typescript
async function checkAndReconnectToSummaryJob(dealId: string) {
  const deal = await getDealById(dealId);

  // Check if there's an active summary job with a valid job_id
  const activeJob = deal.active_deal_jobs?.deal_summary;
  if (activeJob?.job_id) {
    // Job is running - reconnect to the SSE stream
    return subscribeToDealSummaryStream(dealId, activeJob.job_id, callbacks);
  }

  // No active job - check if summary already exists
  if (deal.deal_summary_data) {
    displayFinalSummary(deal.deal_summary_data);
  }

  return null;
}
```

### Notes

- The stream automatically closes after sending the `complete` or `error` event
- If the job completes before the client connects, the `complete` event is sent immediately
- Progress percentages are approximate and may not be strictly linear
- For authenticated SSE, consider using fetch with ReadableStream or EventSource polyfill

---
