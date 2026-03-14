# Transcript Upload — Frontend Integration Design

**Date:** 2026-03-12
**Scope:** Backend API extension + frontend upload UI in briefing room

## Context

Manual transcript upload backend is complete (upload endpoint, Celery task, extraction service). This spec covers:
1. Backend: Expose active `transcript_upload` jobs in GET meetings response
2. Frontend: Upload button, progress tracking, SSE reconnect on refresh

## Guiding Constraint

No new database columns. Transcript state is derived entirely from existing data:
- `active_jobs.transcript_upload` present → upload in progress
- `meeting_summary_data` present → transcript exists (summary requires transcript)
- Neither → no transcript, show upload button

---

## Backend Changes

### 1. Add `transcript_upload` to `MeetingJobsStatus` schema

**File:** `src/api/schemas/crm_models.py`

Add `transcript_upload: DealJobResponse | None = None` to `MeetingJobsStatus`.

### 2. Include TRANSCRIPT_UPLOAD in active jobs query

**File:** `src/service/crm/meetings/meetings_service.py`

In the method that builds `active_jobs` for each meeting, include `DealJobType.TRANSCRIPT_UPLOAD` in the job types queried. Map it to `transcript_upload` key in `MeetingJobsStatus`.

### Result

GET meetings response now includes:
```json
{
  "active_jobs": {
    "meeting_briefing": null,
    "meeting_summary": null,
    "transcript_upload": { "job_id": "...", "job_type": "transcript_upload", "status": "in_progress" }
  }
}
```

---

## Frontend Changes

All changes in `copilot-app-frontend-transcript-upload` worktree.

### 1. Types (`src/modules/deals/types/meetings.types.ts`)

- Add `transcript_upload` field to `MeetingJobsStatus` type

### 2. API Client (`src/modules/deals/api/meetings.api.ts`)

- Add `uploadMeetingTranscript(dealId, eventId, file)`:
  - `POST /deals/{dealId}/meetings/{eventId}/transcript/upload`
  - Multipart form data
  - Returns `GenericResponse<DealJobResponse>`

### 3. Zustand Store (`src/app/stores/use-meeting-jobs-store.ts`)

- Add `transcript_upload` job type alongside `briefing` and `summary`
- Same state shape: `jobId`, `isGenerating`, `progress`, `localStatus`, `error`

### 4. Hook (`src/modules/deals/hooks/use-transcript-upload.ts`)

Mirrors `use-meeting-summary.ts`:
- `startUpload(file)` → calls `uploadMeetingTranscript` API → gets `job_id` → subscribes to SSE
- SSE callbacks:
  - `onProgress`: update Zustand store with stage/progress/message
  - `onComplete`: invalidate meetings query, transition UI
  - `onError`: set error state, show message
- On mount: check `meeting.active_jobs?.transcript_upload?.job_id` → auto-reconnect to SSE
- On upload complete: backend auto-triggers meeting summary — frontend will pick up summary job via next meetings query invalidation

### 5. UI (`src/components/deals/briefing-room.tsx`)

**Upload button** — shown when:
- Meeting is completed (past meeting)
- No `meeting_summary_data` exists
- No `meeting_summary_status === "COMPLETED"`
- No active `transcript_upload` job
- Accepts: PDF, DOCX only, max 10MB
- File input or small drop zone

**Progress state** — shown when `active_jobs.transcript_upload` exists or upload just started:
- Progress bar with stage messages (extracting, saving, triggering summary)
- Same visual pattern as briefing/summary generation

**Transition** — on transcript upload complete:
- Meetings query invalidates
- Meeting summary generation starts automatically (backend)
- UI transitions to showing summary generation progress (existing flow handles this)

---

## Flow Diagram

```
User clicks "Upload Transcript" → selects PDF/DOCX
  ↓
POST /transcript/upload → 202 { job_id }
  ↓
Frontend subscribes to SSE: /jobs/{job_id}/stream
  ↓
Progress: extracting → saving → triggering_summary → complete
  ↓
Backend auto-creates MEETING_SUMMARY job
  ↓
Frontend invalidates meetings query → sees new summary job → reconnects to summary SSE
  ↓
Summary generation completes → UI shows summary
```

**On page refresh mid-upload:**
```
GET /meetings → active_jobs.transcript_upload = { job_id, status }
  ↓
Hook detects active job → auto-reconnects to SSE → resumes progress display
```

---

## Error Handling

| Error | Backend Response | Frontend Behavior |
|-------|-----------------|-------------------|
| Wrong file type | 400 | Show inline validation error |
| File too large | 413 | Show inline validation error |
| Transcript already exists | 409 | Hide upload button (refresh meetings) |
| Upload already in progress | 202 (existing job) | Reconnect to existing job SSE |
| Extraction fails (empty text) | SSE error frame | Show error, allow retry |
| Worker crash | SSE error or timeout | Show error, allow retry |

## Files Changed

**Backend (copilot-backend-transcript-upload):**
- `src/api/schemas/crm_models.py` — add `transcript_upload` to `MeetingJobsStatus`
- `src/service/crm/meetings/meetings_service.py` — include TRANSCRIPT_UPLOAD in active jobs query

**Frontend (copilot-app-frontend-transcript-upload):**
- `src/modules/deals/types/meetings.types.ts` — add type
- `src/modules/deals/api/meetings.api.ts` — add upload API call
- `src/app/stores/use-meeting-jobs-store.ts` — add transcript_upload state
- `src/modules/deals/hooks/use-transcript-upload.ts` — new hook (mirrors use-meeting-summary.ts)
- `src/components/deals/briefing-room.tsx` — upload button + progress UI
