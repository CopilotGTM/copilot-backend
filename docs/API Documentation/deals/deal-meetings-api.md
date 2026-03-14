# Deal Meetings API

This API manages calendar meetings associated with CRM deals, including retrieving meeting lists, generating AI content (briefings, summaries, follow-ups), and streaming real-time job updates via SSE.

**Base URL**: `/users/crm/deals`

> **Note**: The deal `stage` is not included in meeting responses. Use the main Deal API (`GET /users/crm/deals/{deal_id}`) to get deal stage and other deal-level information.

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
- Organisation/user are inferred from the token; clients never pass these explicitly

---

## Rate Limiting

| Operation | Limit |
| --- | --- |
| Read (`GET`) | ~60/min, 1000/hour per user |
| Write (`POST`/`PATCH`) | ~30/min, 500/hour per user |

---

## Overview

### Job Types

The API supports three AI generation job types for meetings:

| Job Type | URL Param | Purpose | When to Use |
| --- | --- | --- | --- |
| **Meeting Briefing** | `briefing` | Pre-meeting preparation | Before a meeting starts |
| **Meeting Summary** | `summary` | Post-meeting recap | After a meeting ends |
| **Meeting Follow-up** | `followup` | Action items & next steps | After meeting summary |

### Workflow

1. **GET** `/deals/{deal_id}/meetings` - Get meetings list with active job status
2. **POST** `/deals/{deal_id}/meetings/{event_id}/jobs/{job_type}` - Start a job
3. **GET** `/deals/{deal_id}/meetings/{event_id}/jobs/{job_id}/stream` - Stream job updates via SSE

### Transcript Upload Workflow

1. **POST** `/deals/{deal_id}/meetings/{event_id}/transcript/presign` - Get presigned upload URL
2. Client uploads file directly to the returned SAS URL (PUT request to Azure Blob)
3. **POST** `/deals/{deal_id}/meetings/{event_id}/transcript/finalize` - Confirm upload and trigger extraction
4. **GET** `/deals/{deal_id}/meetings/{event_id}/jobs/{job_id}/stream` - Stream extraction + summary progress

---

## 1. Get Deal Meetings

Retrieve all meetings associated with a specific deal, including generation statuses and active jobs.

- **Endpoint**: `GET /{deal_id}/meetings`
- **Method**: `GET`
- **Tags**: `crm`, `meetings`

### Path Parameters

| Parameter | Type | Description |
| --- | --- | --- |
| `deal_id` | UUID | The unique identifier of the deal |

### Response

```json
{
  "status": "SUCCESS",
  "message": "Meetings retrieved successfully",
  "data": [
    {
      "event_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "subject": "Discovery Call with Acme Corp",
      "start_at": "2025-11-25T14:00:00Z",
      "status": "ACTIVE",
      "attendees": [
        {
          "name": "Jane Doe",
          "email": "jane@acme.com",
          "status": "accepted"
        }
      ],
      "meeting_prep_status": "COMPLETED",
      "meeting_prep_data": {
        "schema_version": "1.0",
        "generated_at": "2025-11-25T13:30:00Z",
        "data": {
          "briefing_overview": "Ahead of your call with Acme Corp, here are the key points to focus on..."
        }
      },
      "meeting_summary_status": "NOT_STARTED",
      "meeting_summary_data": null,
      "meeting_followup_status": "NOT_STARTED",
      "meeting_followup_data": null,
      "active_jobs": {
        "meeting_briefing": null,
        "meeting_summary": {
          "job_id": "abc123-...",
          "job_type": "DealJobType.MEETING_SUMMARY",
          "status": "JobStatus.IN_PROGRESS"
        },
        "meeting_followup": null
      }
    }
  ]
}
```

### Response Schema (`DealMeetingResponse`)

| Field | Type | Description |
| --- | --- | --- |
| `event_id` | UUID | Unique identifier for the calendar event |
| `subject` | string \| null | Subject or title of the meeting |
| `start_at` | datetime | Start time of the meeting (UTC) |
| `status` | string | Association status: `ACTIVE`, `REMOVED`, `CANCELLED` |
| `attendees` | list | List of attendees with name, email, and response status |
| `meeting_prep_status` | enum | Briefing status: `NOT_STARTED`, `GENERATING`, `COMPLETED`, `FAILED` |
| `meeting_prep_data` | object \| null | Generated briefing content wrapped in metadata envelope (when COMPLETED) |
| `meeting_summary_status` | enum | Summary status: `NOT_STARTED`, `GENERATING`, `COMPLETED`, `FAILED` |
| `meeting_summary_data` | object \| null | Generated summary content wrapped in metadata envelope (when COMPLETED) |
| `meeting_followup_status` | enum | Follow-up status: `NOT_STARTED`, `GENERATING`, `COMPLETED`, `FAILED` |
| `meeting_followup_data` | object \| null | Generated follow-up content wrapped in metadata envelope (when COMPLETED) |
| `active_jobs` | object \| null | Currently running jobs (see below) |

### Active Jobs Schema (`MeetingJobsStatus`)

| Field | Type | Description |
| --- | --- | --- |
| `meeting_briefing` | object \| null | Active briefing job (if running) |
| `meeting_summary` | object \| null | Active summary job (if running) |
| `meeting_followup` | object \| null | Active follow-up job (if running) |
| `transcript_upload` | object \| null | Active transcript upload/extraction job (if running) |

Each active job contains:

| Field | Type | Description |
| --- | --- | --- |
| `job_id` | UUID | Job ID for SSE streaming |
| `job_type` | string | `DealJobType.MEETING_BRIEFING`, `DealJobType.MEETING_SUMMARY`, `DealJobType.MEETING_FOLLOWUP` |
| `status` | string | `JobStatus.PENDING` or `JobStatus.IN_PROGRESS` |

---

## 2. Initiate Meeting Job (Generic)

Start an AI generation job for a meeting. This is the **recommended endpoint** for all job types.

- **Endpoint**: `POST /{deal_id}/meetings/{event_id}/jobs/{job_type}`
- **Method**: `POST`
- **Tags**: `crm`, `meetings`, `jobs`

**Path Parameters:**

| Parameter | Type | Description |
| --- | --- | --- |
| `deal_id` | UUID | The unique identifier of the deal |
| `event_id` | UUID | The unique identifier of the meeting event |
| `job_type` | string | One of: `briefing`, `summary`, `followup` |

**Behavior:**

- **Idempotent**: If a job of the same type is already running for this event, returns the existing job (same `202` status code)
- Returns immediately with `job_id` for SSE subscription
- Background worker processes the job asynchronously

### Response Codes

| Status | Condition | `data` | Frontend Action |
| --- | --- | --- | --- |
| **202** | Job created (new) OR existing job returned (idempotent) | `DealJobResponse` with `job_id` | Connect to SSE stream using `data.job_id` |
| **400** | Invalid `job_type` (must be briefing/summary/followup) | `null` | Show error message |
| **404** | Calendar event not found | `null` | Show error message |
| **409** | Transcript is currently being fetched or processed (summary only) | `null` | Show "Transcript is being fetched, please wait" and retry after a delay |
| **500** | Internal server error | `null` | Show error message |

> **Important**: The status code is **202** (Accepted), not 200 or 201. Frontend must check for `202`.

**Success Response (202 — new job):**

```json
{
  "status": "SUCCESS",
  "message": "Summary generation initiated",
  "data": {
    "job_id": "7a85f64-5717-4562-b3fc-2c963f66afa6",
    "job_type": "DealJobType.MEETING_SUMMARY",
    "status": "JobStatus.PENDING"
  }
}
```

**Success Response (202 — existing job, idempotent):**

```json
{
  "status": "SUCCESS",
  "message": "Summary generation already in progress",
  "data": {
    "job_id": "7a85f64-5717-4562-b3fc-2c963f66afa6",
    "job_type": "DealJobType.MEETING_SUMMARY",
    "status": "JobStatus.IN_PROGRESS"
  }
}
```

**Error Response (409 — transcript being fetched):**

```json
{
  "status": "ERROR",
  "message": "Transcript is currently being fetched. Please try again shortly.",
  "data": null
}
```

> **409 Handling**: This only applies to `summary` jobs. It means the backend is actively downloading the transcript from Teams/Google Meet. The frontend should show the `message` to the user and can retry after 10-15 seconds or poll `GET /{deal_id}/meetings` to check when the transcript becomes available.

---

## 3. Stream Job Updates (SSE)

Subscribe to real-time updates for a running job via Server-Sent Events.

- **Endpoint**: `GET /{deal_id}/meetings/{event_id}/jobs/{job_id}/stream`
- **Method**: `GET`
- **Content-Type**: `text/event-stream`
- **Tags**: `crm`, `meetings`, `jobs`, `sse`

**Path Parameters:**

| Parameter | Type | Description |
| --- | --- | --- |
| `deal_id` | UUID | The unique identifier of the deal |
| `event_id` | UUID | The unique identifier of the meeting event |
| `job_id` | UUID | The job ID (from initiate response or `active_jobs`) |

### SSE Event Types

The SSE stream sends structured JSON frames with different types:

| Frame Type | Description |
| --- | --- |
| `progress.update` | Progress update during processing stages |
| `answer.replace` | Final result data |
| `error` | Error details when job fails permanently |
| `finalize` | Completion signal (success or error) |

### SSE Frame Schema

All frames include these common fields:

| Field | Type | Description |
| --- | --- | --- |
| `type` | string | Frame type: `progress.update`, `answer.replace`, `error`, `finalize` |
| `sequence` | number | Monotonically increasing sequence number |
| `timestamp` | number | Unix timestamp of frame creation |
| `job_type` | string | `meeting_briefing`, `meeting_summary`, or `meeting_followup` |
| `job_id` | string | The job identifier |

---

### Progress Update Frames (`progress.update`)

Progress frames are sent during processing to show current stage and completion percentage.

**Frame Schema:**

| Field | Type | Description |
| --- | --- | --- |
| `message` | string | Human-readable status message |
| `stage` | string | Current processing stage identifier |
| `progress` | number | Progress percentage (0.0 to 1.0) |

**Example:**

```json
{
  "type": "progress.update",
  "message": "Generating summary...",
  "stage": "generation",
  "progress": 0.5,
  "sequence": 3,
  "timestamp": 1733012345.678,
  "job_type": "meeting_summary",
  "job_id": "abc123-def456"
}
```

#### Progress Stages by Job Type

**Meeting Briefing (`briefing`):**

| Stage | Progress | Message |
| --- | --- | --- |
| `context` | 0.1 | Loading context... |
| `analysis` | 0.3 | Analyzing deal context... |
| `generation` | 0.5 | Generating briefing... |
| `saving` | 0.9 | Saving results... |

**Meeting Summary (`summary`):**

| Stage | Progress | Message |
| --- | --- | --- |
| `context` | 0.1 | Loading meeting context... |
| `transcript` | 0.2 | Checking for transcript... |
| `transcript_missing` | 0.4 | No transcript found. *(error case)* |
| `connection_error` | 0.4 | Connection error - will retry *(error case)* |
| `stakeholders` | 0.3 | Loading stakeholders... |
| `analysis` | 0.4 | Analyzing transcript... |
| `generation` | 0.5 | Generating summary... |
| `saving` | 0.9 | Saving results... |

**Meeting Follow-up (`followup`):**

| Stage | Progress | Message |
| --- | --- | --- |
| `context` | 0.1 | Loading context... |
| `summary` | 0.2 | Loading meeting summary... |
| `generation` | 0.5 | Generating follow-up... |
| `saving` | 0.9 | Saving results... |

---

### Result Frame (`answer.replace`)

Sent when AI generation completes with the full result data. The content is wrapped in a metadata envelope.

**Frame Schema:**

| Field | Type | Description |
| --- | --- | --- |
| `content` | object | The generated data wrapped in metadata envelope (see below) |

**Content Envelope Schema:**

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | string | Schema version (e.g., `"1.0"`) for forward compatibility |
| `generated_at` | string | ISO 8601 timestamp when the content was generated |
| `data` | object | The actual generated payload (schema varies by job type) |

**Example:**

```json
{
  "type": "answer.replace",
  "content": {
    "schema_version": "1.0",
    "generated_at": "2025-12-01T10:15:00Z",
    "data": {
      "meeting_summary": "The discovery call with Acme Corp focused on compliance automation..."
    }
  },
  "sequence": 5,
  "timestamp": 1733012350.123,
  "job_type": "meeting_summary",
  "job_id": "abc123-def456"
}
```

---

### Error Frame (`error`)

Sent when a job fails permanently (non-retryable). Provides user-friendly error information.

**Frame Schema:**

| Field | Type | Description |
| --- | --- | --- |
| `message` | string | Human-readable error message to display to the user |
| `error_code` | string | Machine-readable error code for frontend handling |

**Common Error Codes:**

| Error Code | Description |
| --- | --- |
| `no_transcript` | No transcript available - meeting wasn't recorded or transcription disabled |
| `transcript_unavailable` | Transcript exists but cannot be retrieved |
| `invalid_data` | Missing or invalid meeting/deal data |
| `permanent_failure` | Generic permanent failure |

**Example:**

```json
{
  "type": "error",
  "message": "No transcript available for this meeting. The meeting may not have been recorded or transcription was not enabled.",
  "error_code": "no_transcript",
  "sequence": 3,
  "timestamp": 1733012340.789,
  "job_type": "meeting_summary",
  "job_id": "abc123-def456"
}
```

---

### Finalize Frame (`finalize`)

Signals job completion. Clients should close the SSE connection after receiving this.

**Frame Schema:**

| Field | Type | Description |
| --- | --- | --- |
| `reason` | string | `done` (success) or `error` (failure) |

**Example - Success:**

```json
{
  "type": "finalize",
  "reason": "done",
  "sequence": 6,
  "timestamp": 1733012351.456,
  "job_type": "meeting_summary",
  "job_id": "abc123-def456"
}
```

**Example - Error:**

```json
{
  "type": "finalize",
  "reason": "error",
  "sequence": 4,
  "timestamp": 1733012340.789,
  "job_type": "meeting_summary",
  "job_id": "abc123-def456"
}
```

---

## 4. Update Meeting Association Status

Update the status of a meeting-deal association (e.g., hide a meeting from the timeline or mark it cancelled).

- **Endpoint**: `PATCH /{deal_id}/meetings/{event_id}/status`
- **Method**: `PATCH`
- **Tags**: `crm`, `meetings`

**Path Parameters:**

| Parameter | Type | Description |
| --- | --- | --- |
| `deal_id` | UUID | The unique identifier of the deal |
| `event_id` | UUID | The unique identifier of the meeting event |

**Request Body:**

```json
{
  "status": "REMOVED"
}
```

| Field | Type | Description |
| --- | --- | --- |
| `status` | enum | New status: `ACTIVE`, `REMOVED`, `CANCELLED` |

**Success Response:**

```json
{
  "status": "SUCCESS",
  "message": "Meeting status updated to REMOVED",
  "data": null
}
```

---

## 5. Presign Transcript Upload

Generate a presigned SAS URL for direct transcript file upload to Azure Blob Storage.

- **Endpoint**: `POST /{deal_id}/meetings/{event_id}/transcript/presign`
- **Method**: `POST`
- **Tags**: `crm`, `meetings`, `transcript`

**Path Parameters:**

| Parameter | Type | Description |
| --- | --- | --- |
| `deal_id` | UUID | The unique identifier of the deal |
| `event_id` | UUID | The unique identifier of the meeting event |

**Request Body:**

```json
{
  "filename": "meeting-notes.pdf",
  "mimetype": "application/pdf",
  "size_bytes": 2048000
}
```

| Field | Type | Description |
| --- | --- | --- |
| `filename` | string | Original filename (1-255 chars) |
| `mimetype` | string | `application/pdf` or `application/vnd.openxmlformats-officedocument.wordprocessingml.document` |
| `size_bytes` | int | File size in bytes (max 25 MB) |

### Response Codes

| Status | Condition | Frontend Action |
| --- | --- | --- |
| **201** | Presign created, upload URL returned | Upload file to `upload_url` via PUT, then call finalize |
| **202** | Upload already in progress (idempotent) | Connect to existing job's SSE stream |
| **400** | Invalid mimetype or file too large | Show error message |
| **404** | Event not found or not associated with deal | Show error message |
| **409** | Meeting removed/cancelled, transcript already exists, or being processed | Show error message |

**Success Response (201):**

```json
{
  "status": "SUCCESS",
  "message": "Transcript upload presigned",
  "data": {
    "job_id": "7a85f64-5717-4562-b3fc-2c963f66afa6",
    "upload_url": "https://storageaccount.blob.core.windows.net/org-container/transcripts/deal-id/event-id/meeting-notes.pdf?sv=...",
    "expires_in_seconds": 900,
    "status": "JobStatus.PENDING"
  }
}
```

**Idempotent Response (202 — job already exists):**

```json
{
  "status": "SUCCESS",
  "message": "Transcript upload already in progress",
  "data": {
    "job_id": "7a85f64-5717-4562-b3fc-2c963f66afa6",
    "upload_url": "",
    "expires_in_seconds": 0,
    "status": "JobStatus.IN_PROGRESS"
  }
}
```

---

## 6. Finalize Transcript Upload

Confirm that the transcript file has been uploaded to blob storage. Triggers extraction and summary generation.

- **Endpoint**: `POST /{deal_id}/meetings/{event_id}/transcript/finalize`
- **Method**: `POST`
- **Tags**: `crm`, `meetings`, `transcript`

**Path Parameters:**

| Parameter | Type | Description |
| --- | --- | --- |
| `deal_id` | UUID | The unique identifier of the deal |
| `event_id` | UUID | The unique identifier of the meeting event |

**Request Body:**

```json
{
  "job_id": "7a85f64-5717-4562-b3fc-2c963f66afa6"
}
```

| Field | Type | Description |
| --- | --- | --- |
| `job_id` | UUID | The job ID returned by the presign endpoint |

### Response Codes

| Status | Condition | Frontend Action |
| --- | --- | --- |
| **202** | Finalized — extraction worker enqueued | Connect to SSE stream using `job_id` |
| **400** | Job does not match this meeting | Show error message |
| **404** | Job not found | Show error message |
| **409** | Job not in pending state | Show error message |

**Success Response (202):**

```json
{
  "status": "SUCCESS",
  "message": "Transcript upload finalized — extraction started",
  "data": {
    "job_id": "7a85f64-5717-4562-b3fc-2c963f66afa6",
    "job_type": "DealJobType.TRANSCRIPT_UPLOAD",
    "status": "JobStatus.IN_PROGRESS"
  }
}
```

> After finalize, subscribe to the SSE stream at `GET /{deal_id}/meetings/{event_id}/jobs/{job_id}/stream` to track extraction progress and receive the generated summary.

---

## Generated Content Schemas

All generated content is wrapped in a metadata envelope for versioning and tracking:

**Envelope Schema:**

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | string | Schema version (e.g., `"1.0"`) for forward compatibility |
| `generated_at` | string | ISO 8601 timestamp when the content was generated |
| `data` | object | The actual generated payload (schema varies by job type) |

### Briefing Data (`meeting_prep_data`)

Generated before a meeting to help with preparation.

**`data` Object Fields:**

| Field | Type | Description |
| --- | --- | --- |
| `briefing_overview` | string | Multi-paragraph prep brief that synthesizes the most important intel before the call |

```json
{
  "schema_version": "1.0",
  "generated_at": "2025-12-01T08:30:00Z",
  "data": {
    "briefing_overview": "Ahead of your call with Acme Corp, here are the key points to focus on...\n\n## Meeting Objective\nValidate integration feasibility and timeline for their compliance automation needs.\n\n## Key Stakeholders\n- **Lisa Wong** (Security Lead) - Focused on audit readiness and compliance visibility\n- **Tom Chen** (IT Director) - Concerned about integration complexity\n\n## Pain Points to Address\n- Manual compliance evidence collection (~40 hrs/mo)\n- Lack of real-time audit visibility\n\n## Suggested Questions\n- What triggers escalation to security leadership?\n- How do you currently track compliance evidence?\n\n## Potential Objections\n- \"We already have something similar\" - Be ready to differentiate\n\n## Pre-Meeting Checklist\n- [ ] Confirm definition of success metrics\n- [ ] Clarify integration ownership"
  }
}
```

### Summary Data (`meeting_summary_data`)

Generated after a meeting from the transcript.

**`data` Object Fields:**

| Field | Type | Description |
| --- | --- | --- |
| `meeting_summary` | string | Transcript-backed recap of what actually happened, including decisions and context |

```json
{
  "schema_version": "1.0",
  "generated_at": "2025-12-01T10:15:00Z",
  "data": {
    "meeting_summary": "The discovery call with Acme Corp focused on their compliance automation needs.\n\n# Overview\nDiscovery call to understand compliance pain points and evaluate fit for our solution.\n\n# Individuals\n| Name | Role | Company |\n| --- | --- | --- |\n| Lisa Wong | Security Lead | Acme Corp |\n| Tom Chen | IT Director | Acme Corp |\n\n## Current Challenges\nThe team spends 40+ hours monthly on manual compliance evidence collection. Their audit process lacks real-time visibility.\n\n## Requirements Identified\n- Integration with existing SIEM tools\n- Automated evidence collection for SOC 2\n- Executive dashboard for compliance posture\n\n# Decision\nAgreed to proceed with a 30-day POC starting January 15th.\n\n# Next Steps\n- Technical team to provide API documentation by Friday\n- Follow-up call scheduled for next Tuesday to discuss integration architecture"
  }
}
```

### Follow-up Data (`meeting_followup_data`)

Generated after the summary to create action items and follow-up communication.

**`data` Object Fields:**

| Field | Type | Description |
| --- | --- | --- |
| `follow_up_overview` | string | Long-form follow-up message with action items and next steps |

```json
{
  "schema_version": "1.0",
  "generated_at": "2025-12-01T10:20:00Z",
  "data": {
    "follow_up_overview": "Thank you for your time today. As discussed, we will proceed with a 30-day POC starting Jan 15.\n\n## Action Items\n- **John Smith** - Send API documentation and integration guide (Due: Dec 6)\n- **Sarah Chen** - Prepare POC environment with sample data (Due: Dec 10)\n\n## Next Steps\n1. Schedule technical deep-dive with their IT team\n2. Prepare ROI analysis based on 40 hours/month savings\n3. Draft POC success criteria document\n\n## Follow-up Email Draft\nHi Jane,\n\nThank you for taking the time to meet with us today. It was great learning more about Acme Corp's compliance automation needs.\n\nAs discussed, we'll send over the API documentation by Friday and schedule a technical deep-dive for next week.\n\nLooking forward to the POC kickoff on January 15th!\n\nBest regards,\nJohn"
  }
}
```

---

## Frontend Integration Example

```typescript
const baseUrl = '/users/crm/deals';

// =========================================================================
// 1. On page load — check for active jobs (reconnection support)
// =========================================================================
const meetingsRes = await fetch(`${baseUrl}/${dealId}/meetings`);
const meetings = meetingsRes.data;

for (const meeting of meetings) {
  // Check each job type for active (PENDING/IN_PROGRESS) jobs
  const activeJob = meeting.active_jobs?.meeting_summary
    ?? meeting.active_jobs?.meeting_briefing
    ?? meeting.active_jobs?.meeting_followup;

  if (activeJob) {
    // A job is running — reconnect to its SSE stream immediately
    connectToSSE(dealId, meeting.event_id, activeJob.job_id);
  } else if (meeting.meeting_summary_status === "COMPLETED") {
    // Show the previously generated content
    displayResult(meeting.meeting_summary_data.data);
  } else {
    // No active job, no completed content — show "Generate" button
  }
}

// =========================================================================
// 2. Start a new job — handle ALL response codes
// =========================================================================
async function startJob(
  dealId: string,
  eventId: string,
  jobType: "briefing" | "summary" | "followup"
) {
  const res = await fetch(
    `${baseUrl}/${dealId}/meetings/${eventId}/jobs/${jobType}`,
    { method: "POST" }
  );

  switch (res.status) {
    case 202: {
      // SUCCESS — job created (or existing job returned)
      // ALWAYS connect to SSE using the job_id from response
      const { job_id } = res.data;
      connectToSSE(dealId, eventId, job_id);
      break;
    }
    case 409:
      // Transcript is being fetched — show message, retry later
      showInfo(res.message); // "Transcript is currently being fetched..."
      // Optionally: poll GET /meetings after 10-15s to check status
      setTimeout(() => startJob(dealId, eventId, jobType), 15000);
      break;
    case 400:
    case 404:
    case 500:
    default:
      // Show error to user
      showError(res.message);
      break;
  }
}

// =========================================================================
// 3. SSE stream — handle all frame types
// =========================================================================
function connectToSSE(dealId: string, eventId: string, jobId: string) {
  const es = new EventSource(
    `${baseUrl}/${dealId}/meetings/${eventId}/jobs/${jobId}/stream`
  );

  es.onmessage = (event) => {
    const frame = JSON.parse(event.data);

    switch (frame.type) {
      case "progress.update":
        updateProgressBar(frame.progress);
        showStatusMessage(frame.message);
        break;

      case "answer.replace":
        // Final result — display the generated content
        displayResult(frame.content.data);
        break;

      case "error":
        showError(frame.message);
        // Handle specific error codes if needed
        if (frame.error_code === "no_transcript") {
          showNoTranscriptMessage();
        }
        break;

      case "finalize":
        es.close();
        if (frame.reason === "done") {
          showSuccess("Generation complete!");
        }
        // If reason === "error", the error frame was already shown above
        break;
    }
  };

  es.onerror = () => {
    es.close();
    // SSE disconnected — can reconnect using Last-Event-ID header
    // or poll GET /meetings to check active_jobs
  };
}
```

// =========================================================================
// 4. Transcript upload — presign → upload → finalize → stream
// =========================================================================
async function uploadTranscript(dealId: string, eventId: string, file: File) {
  // Step 1: Presign
  const presignRes = await fetch(
    `${baseUrl}/${dealId}/meetings/${eventId}/transcript/presign`,
    {
      method: "POST",
      body: JSON.stringify({
        filename: file.name,
        mimetype: file.type,
        size_bytes: file.size,
      }),
    }
  );

  if (presignRes.status === 202) {
    // Upload already in progress — reconnect to SSE
    connectToSSE(dealId, eventId, presignRes.data.job_id);
    return;
  }
  if (presignRes.status !== 201) {
    showError(presignRes.message);
    return;
  }

  const { job_id, upload_url } = presignRes.data;

  // Step 2: Upload file directly to Azure Blob via SAS URL
  await fetch(upload_url, {
    method: "PUT",
    headers: {
      "x-ms-blob-type": "BlockBlob",
      "Content-Type": file.type,
    },
    body: file,
  });

  // Step 3: Finalize
  const finalizeRes = await fetch(
    `${baseUrl}/${dealId}/meetings/${eventId}/transcript/finalize`,
    {
      method: "POST",
      body: JSON.stringify({ job_id }),
    }
  );

  if (finalizeRes.status === 202) {
    connectToSSE(dealId, eventId, job_id);
  } else {
    showError(finalizeRes.message);
  }
}
```

### Typical SSE Frame Sequence

**Meeting Summary Job:**

```json
1. {"type": "progress.update", "stage": "context", "progress": 0.1, "message": "Loading meeting context..."}
2. {"type": "progress.update", "stage": "transcript", "progress": 0.2, "message": "Checking for transcript..."}
3. {"type": "progress.update", "stage": "stakeholders", "progress": 0.3, "message": "Loading stakeholders..."}
4. {"type": "progress.update", "stage": "analysis", "progress": 0.4, "message": "Analyzing transcript..."}
5. {"type": "progress.update", "stage": "generation", "progress": 0.5, "message": "Generating summary..."}
6. {"type": "progress.update", "stage": "saving", "progress": 0.9, "message": "Saving results..."}
7. {"type": "answer.replace", "content": {"schema_version": "1.0", "generated_at": "2025-12-01T10:15:00Z", "data": {"meeting_summary": "..."}}}
8. {"type": "finalize", "reason": "done"}
```

**Error Case (No Transcript):**

```json
1. {"type": "progress.update", "stage": "context", "progress": 0.1, "message": "Loading meeting context..."}
2. {"type": "progress.update", "stage": "transcript", "progress": 0.2, "message": "Checking for transcript..."}
3. {"type": "error", "message": "No transcript available for this meeting. The meeting may not have been recorded or transcription was not enabled.", "error_code": "no_transcript"}
4. {"type": "finalize", "reason": "error"}
```

---

## Legacy Endpoints (Deprecated)

These endpoints still work but will be removed in a future version:

| Old Endpoint | New Endpoint |
| --- | --- |
| `POST /{deal_id}/meetings/{event_id}/briefing` | `POST /{deal_id}/meetings/{event_id}/jobs/briefing` |
| `GET /{deal_id}/meetings/{event_id}/briefing/stream` | `GET /{deal_id}/meetings/{event_id}/jobs/{job_id}/stream` |

**Migration Note**: The new SSE endpoint requires `job_id` instead of inferring it. Get the `job_id` from the POST response or from `active_jobs` in the meetings list.


