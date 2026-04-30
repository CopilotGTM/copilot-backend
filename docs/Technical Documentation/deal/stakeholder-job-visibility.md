# Stakeholder Background Job Visibility

## Status

Design proposal. No runtime behavior is changed by this document.

## Goal

Make stakeholder-related background work visible, reconnectable, and user-owned without
turning stakeholder creation into automatic research.

The backend should reuse the existing `DealJobs` table, Redis Stream publishing, and
`DealJobSSEService`. The change is a contract and orchestration cleanup, not a new job
infrastructure.

## Current Behavior

Manual stakeholder creation currently does three things in one request path:

1. Creates the `DealStakeholder` row and timeline event.
2. Calls `trigger_deal_mapping_if_ready()`, which fire-and-forgets:
   - `deal.match_calendar_to_deals`
   - `deal.match_email_to_deals`
   - `stakeholder.sync_deal_stakeholders`
3. Dispatches `stakeholder_research.process_job` after creation.

The current behavior has these gaps:

- The mapping tasks are invisible to the user even though they may update meetings,
  emails, stakeholder sync state, timeline, and deal activity.
- The create response returns only the stakeholder, so the frontend cannot show
  progress or reconnect after navigation/refresh.
- Research is mixed into stakeholder creation even though it is a separate,
  user-visible operation with different semantics.
- Existing job streaming is reusable, but the only deal-level stream route is tied to
  deal summary naming.
- Reconnect is partial: stakeholder research jobs already use `DealJobs`, but the
  deterministic post-add work is not represented as jobs.

## Decisions

1. Stakeholder creation must not automatically start stakeholder research.
2. Deterministic post-add work must create user-visible `DealJobs` rows.
3. Each visible operation gets its own `DealJobs.job_id`.
4. Jobs created by one stakeholder-add action are correlated with
   `job_metadata.operation_group_id`.
5. `stakeholder_research` remains separate from deterministic rematch/sync work and
   should be surfaced at stakeholder level as well as in the global activity surface.
6. Scheduled/background research retry jobs remain automated pipeline work and are not
   shown as user-triggered activity unless they are attached to a user-triggered job.
7. Existing `DealJobSSEService` remains the streaming mechanism for all deal job types.

## Target Architecture

```mermaid
flowchart TD
    A[POST /users/crm/deals/{deal_id}/stakeholders] --> B[StakeholdersService.create_stakeholder]
    B --> C[StakeholderJobVisibilityService]
    C --> D1[DealJobs: deal_email_rematch]
    C --> D2[DealJobs: deal_calendar_rematch]
    C --> D3[DealJobs: stakeholder_sync]
    D1 --> E1[deal.match_email_to_deals]
    E1 --> D2
    D2 --> E2[deal.match_calendar_to_deals]
    E2 --> D3
    D3 --> E3[stakeholder.sync_deal_stakeholders]
    E1 --> F[DealJobRedisPublisher]
    E2 --> F
    E3 --> F
    F --> G[Redis Stream per job]
    G --> H[GET /users/crm/deals/{deal_id}/jobs/{job_id}/stream]
    H --> I[DealJobSSEService]

    J[POST /users/crm/deals/{deal_id}/stakeholders/{stakeholder_id}/research] --> K[DealJobs: stakeholder_research]
    K --> L[stakeholder_research.process_job]
    L --> F
```

## Job Types

Add these `DealJobType` values:

| Job type | Trigger | Scope | User-visible purpose |
| --- | --- | --- | --- |
| `deal_email_rematch` | Stakeholder created | Deal | Re-evaluate related emails for the deal |
| `deal_calendar_rematch` | Stakeholder created | Deal | Re-evaluate related meetings for the deal |
| `stakeholder_sync` | Stakeholder created | Deal/stakeholder | Refresh stakeholder set and derived stakeholder state |
| `stakeholder_research` | Stakeholder created or manual research trigger | Stakeholder | Research and save a stakeholder profile |

Existing `stakeholder_research` remains valid. Stakeholder add/re-add creates a
research job for the added/restored stakeholder, while email/calendar/sync
processing may dispatch research for additional newly discovered stakeholders via
the existing dedupe/cooldown path.

## Job Metadata Contract

All jobs created from a manual stakeholder-add operation should store:

```json
{
  "trigger": "stakeholder_created",
  "operation_group_id": "a7b9f3c8-2d4c-4b15-9e0c-d69e95bbf211",
  "stakeholder_id": "9d8156e3-0bd1-4d24-9b9c-0cddae9a56ac",
  "deal_id": "2fe3f805-5371-4cf2-a6e4-964cbfdd4644"
}
```

Manual research jobs should store:

```json
{
  "trigger": "manual_research",
  "stakeholder_id": "9d8156e3-0bd1-4d24-9b9c-0cddae9a56ac",
  "deal_id": "2fe3f805-5371-4cf2-a6e4-964cbfdd4644"
}
```

`DealJobs.user_id` must be the current authenticated user for user-triggered work.
Automated retry/scheduled research keeps using the automated pipeline sentinel user.

## API Contracts

### Create stakeholder

Endpoint:

`POST /users/crm/deals/{deal_id}/stakeholders`

Target response:

```json
{
  "success": true,
  "message": "Stakeholder created successfully",
  "data": {
    "stakeholder": {
      "stakeholder_id": "9d8156e3-0bd1-4d24-9b9c-0cddae9a56ac",
      "deal_id": "2fe3f805-5371-4cf2-a6e4-964cbfdd4644",
      "email": "buyer@example.com",
      "name": "Buyer Name"
    },
    "processing_jobs": [
      {
        "job_id": "1d2f4c0c-15df-4cc7-93a3-097246d6be11",
        "job_type": "deal_email_rematch",
        "status": "pending"
      },
      {
        "job_id": "2f9b2b51-c2f4-49b3-8fe3-ad121b343002",
        "job_type": "deal_calendar_rematch",
        "status": "pending"
      },
      {
        "job_id": "d83c03bb-5ebe-4c78-9584-4e827819f1c8",
        "job_type": "stakeholder_sync",
        "status": "pending"
      }
    ]
  }
}
```

Response model shape:

- Add a new response DTO such as `CreateStakeholderResponse`.
- Keep the existing stakeholder DTO nested as `stakeholder`.
- Add `processing_jobs: list[DealJobResponse]`.
- Do not include a `stakeholder_research` job in this response.

### Trigger stakeholder research

Endpoint:

`POST /users/crm/deals/{deal_id}/stakeholders/{stakeholder_id}/research`

Target response:

```json
{
  "success": true,
  "message": "Research job dispatched",
  "data": {
    "job_id": "0885e74d-0e38-45a0-9456-462e11f9d45b",
    "job_type": "stakeholder_research",
    "status": "pending"
  }
}
```

If the service reuses an active research job, return that `DealJobResponse`. If the
request is skipped because the stakeholder was recently researched and no active job is
needed, return a success response with `job_id: null`, `job_type:
"stakeholder_research"`, and `status: "completed"` or add a dedicated skipped field in
a separate DTO. Do not overload a raw `dict` response.

### Generic deal job stream

Endpoint:

`GET /users/crm/deals/{deal_id}/jobs/{job_id}/stream`

Behavior:

- Reuse `DealJobSSEService`.
- Verify the job exists, belongs to the current organisation, belongs to the current
  user, and is associated with the requested deal.
- Return `404` when the job cannot be loaded and `403` when the job belongs to a
  different user or deal.
- Support `Last-Event-ID` resume exactly like the existing deal summary stream.
- Emit `ready`, Redis stream `frame` events, `error`, `timeout`, and `complete`.

The existing deal summary stream can remain for compatibility, but new frontend work
should use the generic route.

## Active Job Discovery

### Deal detail

Deal detail should expose active deal-level processing jobs so the frontend can restore
the deal banner after refresh/navigation.

Add a field to the deal detail response, for example:

```json
{
  "active_deal_jobs": {
    "deal_summary": null,
    "stakeholder_processing": [
      {
        "job_id": "1d2f4c0c-15df-4cc7-93a3-097246d6be11",
        "job_type": "deal_email_rematch",
        "status": "in_progress"
      },
      {
        "job_id": "2f9b2b51-c2f4-49b3-8fe3-ad121b343002",
        "job_type": "deal_calendar_rematch",
        "status": "pending"
      }
    ]
  }
}
```

Implementation should preserve the existing `deal_summary` reconnect contract and add
stakeholder processing without breaking current clients.

### Stakeholder detail

Stakeholder detail should expose active `stakeholder_research` only:

```json
{
  "active_research_job": {
    "job_id": "0885e74d-0e38-45a0-9456-462e11f9d45b",
    "job_type": "stakeholder_research",
    "status": "in_progress"
  }
}
```

Do not surface email/calendar/stakeholder-sync jobs at stakeholder row/detail level.
Those are deal-level processing jobs.

## Worker Progress Contract

All visible worker tasks must accept `job_id` and publish frames with
`DealJobRedisPublisher`.

| Job type | Stages | Suggested UI label |
| --- | --- | --- |
| `deal_email_rematch` | `queued`, `matching_emails`, `saving_email_links`, `complete` | Updating related emails |
| `deal_calendar_rematch` | `queued`, `matching_meetings`, `saving_meeting_links`, `complete` | Finding meetings |
| `stakeholder_sync` | `queued`, `searching_related_stakeholders`, `saving_stakeholders`, `complete` | Searching related stakeholders |
| `stakeholder_research` | `queued`, `researching`, `saving_research`, `complete` | Researching stakeholder |

Frame shape:

```json
{
  "type": "progress.update",
  "stage": "matching_emails",
  "progress": 0.5,
  "message": "Updating related emails",
  "job_type": "deal_email_rematch",
  "job_id": "1d2f4c0c-15df-4cc7-93a3-097246d6be11"
}
```

Completion must update `DealJobs.status`, write `result_data` where useful, publish
`answer.replace` if the frontend needs summary data, and always publish `finalize`.
Failures must set `DealJobs.status = failed`, write `error_message`, publish `error`,
and publish `finalize` with an error reason.

## Service Boundary

Introduce a small orchestration service rather than putting job creation directly into
the API handler.

Suggested responsibility:

`StakeholderJobVisibilityService`

- Create the three post-add jobs in one transaction after the stakeholder exists.
- Attach `operation_group_id` and `stakeholder_id` metadata.
- Enqueue the matching worker task for each job.
- Return `list[DealJobResponse]`.

This service should be called by user-visible create flows. The initial API scope is
`POST /{deal_id}/stakeholders`. Bot and agent create paths should be audited before
rollout so they do not silently keep the old auto-research behavior.

## Research Boundary

Stakeholder research remains separate from deterministic post-add processing.

Required implementation guardrails:

- Remove direct research dispatch from `create_stakeholder`.
- Manual research trigger creates/returns `DealJobType.STAKEHOLDER_RESEARCH`.
- Mapping and sync tasks triggered by manual stakeholder creation must not dispatch
  additional research as a side effect.
- Scheduled retry jobs may continue to dispatch automated research jobs with the
  automated pipeline user ID.

## Reconnect Behavior

1. Mutation returns job IDs immediately.
2. Frontend subscribes to the generic stream endpoint for each returned job.
3. If the user refreshes or navigates away:
   - Deal detail returns active post-add jobs for deal-level banner recovery.
   - Stakeholder detail returns active research job for stakeholder-level recovery.
4. `DealJobSSEService` resumes from `Last-Event-ID` when present.
5. Completed/failed jobs return terminal events so the UI stops reconnecting.

## Failure Behavior

- Stakeholder creation succeeds or fails independently from post-add job execution.
- If stakeholder creation succeeds but job registration fails, the API should return a
  clear partial-processing error only if no jobs could be created. Prefer making job
  registration part of the post-create transaction before enqueueing tasks.
- If task enqueue fails after a job row is created, mark that job `failed` with
  `error_message` instead of leaving it permanently `pending`.
- If one post-add job fails, the other jobs continue independently.
- The UI should invalidate affected deal/stakeholder/activity queries after each job
  completes, and show failed rows in the activity center with retry guidance.

## Rollout Plan

1. Add the architecture/API DTOs and tests behind the existing routes.
2. Add new `DealJobType` enum values and focused job discovery helpers.
3. Add the generic deal job stream route.
4. Implement visible job registration for stakeholder create.
5. Update worker tasks to accept optional `job_id`, publish progress, and finalize.
6. Remove automatic research dispatch from stakeholder create and suppress research side
   effects for the manual-create operation group.
7. Update API documentation under `docs/API Documentation/deals/`.
8. Wire frontend to the returned jobs and active job discovery contract.

## Verification Plan

Backend tests:

- Stakeholder create returns the created stakeholder plus three processing jobs.
- Stakeholder create does not dispatch `stakeholder_research`.
- Manual research returns one `DealJobResponse` for `stakeholder_research`.
- Generic job stream rejects jobs from another user, another organisation, or another deal.
- Deal detail exposes active `deal_email_rematch`, `deal_calendar_rematch`, and
  `stakeholder_sync` jobs.
- Stakeholder detail exposes active `stakeholder_research` only.
- Each visible worker updates `DealJobs.status` and publishes `progress.update` plus
  `finalize`.

Frontend tests:

- Adding a stakeholder registers three background jobs in the activity center.
- Manual research registers one stakeholder research job.
- Deal page restores the deal-level banner from active job discovery.
- Stakeholder modal/row restores research state only from active research jobs.

## Open Implementation Notes

- Current mapper services contain direct research dispatch calls. Implementation must
  either gate those calls by trigger metadata or add an explicit suppress-research flag
  for manual stakeholder-create rematch jobs.
- `DealJobResponse` currently contains `job_id`, `job_type`, and `status`. That is enough
  for streaming, but UI correlation may require exposing `operation_group_id` in a new
  response DTO or reading it from active job discovery.
- Existing `deal_summary` streaming route can be retained as a compatibility alias while
  new clients move to the generic job stream endpoint.
