# Meetings Tab API

Top-level Meetings APIs are mounted at `/users/meetings`.

These endpoints power the frontend Meetings section across Today, Upcoming, Past, and
Recordings. Reads are scoped to the authenticated user's active Google/Outlook calendar
integrations. Existing deal-scoped meeting APIs remain under `/users/crm/deals` for
briefing, summary, follow-up, transcript upload, and deal timeline behavior.

## Frontend Scope

V1 supports meetings linked to:

- `DEAL`
- `LEAD`
- `CONTACT`

Account, internal, and unlinked business workflows are v2. The list can still show meetings
with no V1 link, but link/search/action workflows should only target deal, lead, or contact
for v1.

V1 recordings are Recall bot recordings only. Playback URLs are fetched fresh from Recall
when available; the frontend should not cache playback URLs as permanent assets.

## Response Wrapper

All endpoints return the shared response envelope:

```json
{
  "status": "SUCCESS",
  "message": "Meetings retrieved",
  "data": {}
}
```

Errors use the same envelope shape where possible, with a non-2xx HTTP status and `data`
usually `null`.

## Shared Types

### MeetingsView

```ts
type MeetingsView = "today" | "upcoming" | "past" | "recordings";
```

Default windows when the frontend does not pass `start_at` and `end_at`:

- `today`: current user's local day.
- `upcoming`: after today's local day through roughly the next 90 days.
- `past`: previous 180 days up to the start of today.
- `recordings`: previous 180 days through roughly the next 90 days, filtered to Recall sessions.

### MeetingLinkedRecordType

```ts
type MeetingLinkedRecordType = "DEAL" | "LEAD" | "CONTACT";
```

### MeetingBotPreferenceValue

```ts
type MeetingBotPreferenceValue = "AUTO_INVITE" | "ASK_BEFORE" | "DO_NOT_INVITE";
```

### MeetingBotStatus.status

Current backend values:

```ts
type MeetingBotComputedStatus =
  | "disabled"
  | "recording_live"
  | "already_sent"
  | "auto_invite_eligible"
  | "invite_available";
```

Suggested frontend mapping:

- `disabled`: show disabled/not invited state.
- `recording_live`: show recording live state.
- `already_sent`: show invited/scheduled state.
- `auto_invite_eligible`: show auto-invite eligible or auto-invited state depending on UX copy.
- `invite_available`: show manual invite action.

### MeetingListItem

```ts
type MeetingListItem = {
  event_id: string;
  title: string | null;
  start_at: string;
  end_at: string;
  duration_minutes: number | null;
  platform: string | null;
  conference_link: string | null;
  status: string | null;
  attendees: MeetingAttendeeItem[];
  attendee_count: number;
  linked_record: MeetingLinkedRecord | null;
  bot: MeetingBotStatus;
  recording_status: string | null;
  transcript_status: string | null;
  has_recording: boolean;
  has_summary: boolean;
  has_prep: boolean;
  next_action: string | null;
};

type MeetingAttendeeItem = {
  email: string;
  name: string | null;
  response_status: string | null;
  is_organiser: boolean;
};

type MeetingLinkedRecord = {
  record_type: "DEAL" | "LEAD" | "CONTACT";
  record_id: string;
  name: string | null;
  metadata: Record<string, unknown>;
};

type MeetingBotStatus = {
  preference: MeetingBotPreferenceValue | null;
  effective_preference: MeetingBotPreferenceValue | null;
  status: MeetingBotComputedStatus | string;
  reason: string | null;
  is_external: boolean;
  provider_session_id: string | null;
};
```

## List Meetings

```http
GET /users/meetings
```

Use this endpoint for the list/table in all four tabs.

Query parameters:

| Name | Type | Required | Notes |
| --- | --- | --- | --- |
| `view` | `today | upcoming | past | recordings` | No | Defaults to `today`. |
| `start_at` | ISO datetime | No | Optional explicit UTC or timezone-aware window start. |
| `end_at` | ISO datetime | No | Optional explicit UTC or timezone-aware window end. |
| `q` | string | No | Searches title, organiser email, attendee email, attendee display name. |
| `linked_type` | `DEAL | LEAD | CONTACT` | No | `DEAL` includes existing deal associations even if no Meetings-tab link row exists. |
| `bot_status` | string | No | Filters after computed bot status. Use values from `MeetingBotStatus.status`. |
| `recording_status` | string | No | Filters Recall recording session status. |
| `page` | number | No | Default `1`. |
| `limit` | number | No | Default `50`, max `100`. |

Example:

```http
GET /users/meetings?view=today&page=1&limit=50
```

Example response:

```json
{
  "status": "SUCCESS",
  "message": "Meetings retrieved",
  "data": {
    "items": [
      {
        "event_id": "4fd09b96-9f31-4dd4-9328-8f9b9f1bb227",
        "title": "Discovery with Acme leadership",
        "start_at": "2026-06-04T10:30:00Z",
        "end_at": "2026-06-04T11:00:00Z",
        "duration_minutes": 30,
        "platform": "microsoft_teams",
        "conference_link": "https://teams.microsoft.com/l/meetup-join/...",
        "status": "confirmed",
        "attendees": [
          {
            "email": "priya@company.com",
            "name": "Priya Shah",
            "response_status": "accepted",
            "is_organiser": true
          }
        ],
        "attendee_count": 7,
        "linked_record": {
          "record_type": "DEAL",
          "record_id": "2ef6e7fc-23f5-4126-b8e6-4ca748afe840",
          "name": "Acme Corp",
          "metadata": {
            "stage": "Proposal",
            "amount": 80000
          }
        },
        "bot": {
          "preference": null,
          "effective_preference": "AUTO_INVITE",
          "status": "recording_live",
          "reason": null,
          "is_external": true,
          "provider_session_id": "bot_abc123"
        },
        "recording_status": "IN_CALL",
        "transcript_status": null,
        "has_recording": true,
        "has_summary": false,
        "has_prep": true,
        "next_action": "prepare"
      }
    ],
    "total_count": 1,
    "page": 1,
    "limit": 50
  }
}
```

Frontend notes:

- Use `linked_record` to render the "Linked to" chip.
- Use `bot.status` and `bot.effective_preference` for bot chips/actions.
- Use `has_recording`, `has_summary`, `recording_status`, and `transcript_status` for the Past and Recordings action column.
- Use `next_action` as a hint only; the frontend can still choose button labels by tab.

## Get Meeting Detail

```http
GET /users/meetings/{event_id}
```

Use this endpoint when a row is selected and the drawer opens.

Response data extends `MeetingListItem`:

```ts
type MeetingDetail = MeetingListItem & {
  description: string | null;
  organiser_name: string | null;
  organiser_email: string | null;
  linked_record_detail: Record<string, unknown> | null;
  suggested_actions: Array<Record<string, unknown>>;
};
```

Example response:

```json
{
  "status": "SUCCESS",
  "message": "Meeting retrieved",
  "data": {
    "event_id": "4fd09b96-9f31-4dd4-9328-8f9b9f1bb227",
    "title": "Discovery with Acme leadership",
    "start_at": "2026-06-04T10:30:00Z",
    "end_at": "2026-06-04T11:00:00Z",
    "duration_minutes": 30,
    "platform": "microsoft_teams",
    "conference_link": "https://teams.microsoft.com/l/meetup-join/...",
    "status": "confirmed",
    "attendees": [],
    "attendee_count": 7,
    "linked_record": {
      "record_type": "DEAL",
      "record_id": "2ef6e7fc-23f5-4126-b8e6-4ca748afe840",
      "name": "Acme Corp",
      "metadata": {
        "stage": "Proposal",
        "amount": 80000
      }
    },
    "bot": {
      "preference": null,
      "effective_preference": "AUTO_INVITE",
      "status": "recording_live",
      "reason": null,
      "is_external": true,
      "provider_session_id": "bot_abc123"
    },
    "recording_status": "IN_CALL",
    "transcript_status": null,
    "has_recording": true,
    "has_summary": false,
    "has_prep": true,
    "next_action": "prepare",
    "description": "Discuss Acme rollout.",
    "organiser_name": "Priya Shah",
    "organiser_email": "priya@company.com",
    "linked_record_detail": {
      "record_type": "DEAL",
      "record_id": "2ef6e7fc-23f5-4126-b8e6-4ca748afe840",
      "name": "Acme Corp",
      "metadata": {
        "stage": "Proposal",
        "amount": 80000
      }
    },
    "suggested_actions": [
      {
        "label": "Create follow-up",
        "record_type": "DEAL",
        "record_id": "2ef6e7fc-23f5-4126-b8e6-4ca748afe840"
      }
    ]
  }
}
```

Frontend notes:

- This endpoint does not return full deal/lead/contact objects. Use existing CRM detail APIs
  if the drawer needs a richer record-specific panel.
- `linked_record_detail` currently mirrors compact linked-record fields and may expand later.
- Existing deal associations are shown as deal links even if no explicit `meeting_record_links`
  row exists yet.

## Create Meeting

```http
POST /users/meetings
```

Creates a real provider calendar invite through `CalendarInviteService`. Use this from the
"New Meeting" modal.

Request body:

```ts
type CreateMeetingRequest = {
  title: string;
  start_at: string;
  end_at: string;
  timezone: string;
  attendees?: CalendarInviteAttendee[];
  body?: string | null;
  location?: string | null;
  sender_email?: string | null;
  idempotency_key?: string | null;
  link?: {
    record_type: "DEAL" | "LEAD" | "CONTACT";
    record_id: string;
  } | null;
  bot_preference?: "AUTO_INVITE" | "ASK_BEFORE" | "DO_NOT_INVITE" | null;
};

type CalendarInviteAttendee = {
  email: string;
  name?: string | null;
  optional?: boolean;
};
```

Validation:

- `start_at` and `end_at` must include timezone info.
- `end_at` must be after `start_at`.
- `title` is required.
- At least one attendee must exist after backend prefill.
- Max attendees: `100`.

Lead/contact prefill:

- If `link.record_type = "LEAD"`, backend adds the lead primary email and lead participants.
- If `link.record_type = "CONTACT"`, backend adds the contact email.
- Frontend can still send attendees selected by the user; backend deduplicates by email.

Example request:

```json
{
  "title": "Product demo with Acme Corp",
  "start_at": "2026-06-05T15:00:00+05:30",
  "end_at": "2026-06-05T15:30:00+05:30",
  "timezone": "Asia/Kolkata",
  "attendees": [
    {
      "email": "john.davis@acme.com",
      "name": "John Davis",
      "optional": false
    }
  ],
  "body": "Agenda: demo and pricing discussion.",
  "location": null,
  "sender_email": null,
  "idempotency_key": "new-meeting-4fd09b96-2026-06-05T15:00",
  "link": {
    "record_type": "DEAL",
    "record_id": "2ef6e7fc-23f5-4126-b8e6-4ca748afe840"
  },
  "bot_preference": "AUTO_INVITE"
}
```

Example response:

```json
{
  "status": "SUCCESS",
  "message": "Meeting created",
  "data": {
    "event_id": "4fd09b96-9f31-4dd4-9328-8f9b9f1bb227",
    "provider_event_id": "provider-event-id",
    "conference_link": "https://meet.google.com/abc-defg-hij",
    "meeting": {
      "event_id": "4fd09b96-9f31-4dd4-9328-8f9b9f1bb227",
      "title": "Product demo with Acme Corp",
      "start_at": "2026-06-05T09:30:00Z",
      "end_at": "2026-06-05T10:00:00Z",
      "duration_minutes": 30,
      "platform": "google_meet",
      "conference_link": "https://meet.google.com/abc-defg-hij",
      "status": "confirmed",
      "attendees": [],
      "attendee_count": 1,
      "linked_record": {
        "record_type": "DEAL",
        "record_id": "2ef6e7fc-23f5-4126-b8e6-4ca748afe840",
        "name": "Acme Corp",
        "metadata": {
          "stage": "Proposal",
          "amount": 80000
        }
      },
      "bot": {
        "preference": "AUTO_INVITE",
        "effective_preference": "AUTO_INVITE",
        "status": "auto_invite_eligible",
        "reason": null,
        "is_external": true,
        "provider_session_id": null
      },
      "recording_status": null,
      "transcript_status": null,
      "has_recording": false,
      "has_summary": false,
      "has_prep": false,
      "next_action": "prepare",
      "description": "Agenda: demo and pricing discussion.",
      "organiser_name": "Priya Shah",
      "organiser_email": "priya@company.com",
      "linked_record_detail": {
        "record_type": "DEAL",
        "record_id": "2ef6e7fc-23f5-4126-b8e6-4ca748afe840",
        "name": "Acme Corp",
        "metadata": {
          "stage": "Proposal",
          "amount": 80000
        }
      },
      "suggested_actions": []
    },
    "bot_dispatch": {
      "status": "sent",
      "provider_session_id": "bot_abc123"
    }
  }
}
```

Frontend notes:

- Use a stable `idempotency_key` for retry-safe create flows.
- `bot_dispatch` may be `null`. If auto-invite fails after the meeting is created, the response
  may contain `{ "status": "failed", "reason": "auto_invite_failed", "message": "..." }`.
  Treat that as "meeting created, bot needs attention", not as meeting creation failure.
- If the frontend wants to let users pick a sender mailbox, set `sender_email`; otherwise pass
  `null` and let the backend choose the default integration sender.

## Link Meeting

```http
PATCH /users/meetings/{event_id}/link
```

Sets the active V1 CRM link for one meeting.

Request:

```json
{
  "record_type": "CONTACT",
  "record_id": "8dbfe5d8-75a7-45a2-9934-95ca04a981af"
}
```

Response data: `MeetingDetail`.

Rules:

- Only `DEAL`, `LEAD`, and `CONTACT` are accepted.
- One active Meetings-tab link exists per org/event.
- Deal links are mirrored into `calendar_event_deal_associations` so existing deal jobs keep working.
- Lead/contact links remove stale active deal associations from the deal pipeline.
- After linking, backend may auto-invite the bot if the meeting is external and eligible.

Frontend notes:

- Use this from the "Change link" or search box in the drawer.
- After success, invalidate/reload list and drawer data for that event.
- For v1, do not offer account/internal/none as final link targets.

## Set Bot Settings

```http
PATCH /users/meetings/{event_id}/bot-settings
```

Sets the current user's per-meeting bot preference override.

Request:

```json
{
  "preference": "DO_NOT_INVITE"
}
```

Response data: `MeetingDetail`.

Behavior:

- `AUTO_INVITE`: backend may attempt auto-dispatch after saving the preference.
- `ASK_BEFORE`: frontend should show manual invite affordance.
- `DO_NOT_INVITE`: frontend should show disabled/not invited state.

Frontend notes:

- This is per meeting and user scoped.
- Save the preference from the drawer or create modal bot settings.

## Invite Bot Manually

```http
POST /users/meetings/{event_id}/bot/invite
```

Manually invites the Recall bot after proving that the event belongs to the authenticated
user's connected calendar.

Request body: none.

Example success:

```json
{
  "status": "SUCCESS",
  "message": "Bot invite processed",
  "data": {
    "status": "sent",
    "provider_session_id": "bot_abc123"
  }
}
```

Important errors:

- `404`: meeting not found or not owned by current user's calendar integration.
- `409`: meeting already started and Recall cannot join late.
- `500`: provider or dispatch failure.

Frontend notes:

- Use this for "Invite bot" buttons in list rows and drawers.
- After success, refresh the meeting row/detail to update bot state.
- If `409`, show a clear message that the bot cannot join this meeting late.

## Get Recording

```http
GET /users/meetings/{event_id}/recording
```

Returns Recall playback metadata for the recording intelligence page.

Response data:

```ts
type MeetingRecordingResponse = {
  event_id: string;
  recording_status: string | null;
  transcript_status: string | null;
  playback_url: string | null;
  provider_session_id: string | null;
  transcript_artifact: Record<string, unknown> | null;
  summary: Record<string, unknown> | null;
  action_items: Array<Record<string, unknown>>;
};
```

Example response:

```json
{
  "status": "SUCCESS",
  "message": "Recording retrieved",
  "data": {
    "event_id": "4fd09b96-9f31-4dd4-9328-8f9b9f1bb227",
    "recording_status": "DONE",
    "transcript_status": "DONE",
    "playback_url": "https://recall-provider.example/signed-video-url",
    "provider_session_id": "bot_abc123",
    "transcript_artifact": {
      "download_url": "https://recall-provider.example/signed-transcript-url"
    },
    "summary": {
      "overview": "Acme discussed pricing and implementation."
    },
    "action_items": [
      {
        "title": "Send revised proposal",
        "owner": "Priya Shah"
      }
    ]
  }
}
```

Important errors:

- `409`: `Recording is not ready`. This means no playable Recall mixed-video artifact is
  currently available.
- `404`: meeting not found or not owned by current user's calendar integration.

Frontend notes:

- Use `playback_url` directly for the media player, but treat it as short-lived.
- If `409`, show processing/not-ready state and allow retry.
- Transcript text is not returned inline by this endpoint. Use `transcript_artifact` if/when
  frontend has a download/render path for it, or integrate with the existing transcript flow.
- `summary` and `action_items` are currently deal-summary derived when the recording is deal-linked.
  Lead/contact recordings do not expose stale deal summary data.

## Recommended Frontend Flow

### Today Tab

1. Call `GET /users/meetings?view=today`.
2. Split rows by current time if the UI wants "Live now" and "Later today".
3. Use `POST /bot/invite` for invite buttons.
4. Use `GET /{event_id}` when opening the drawer.

### Upcoming Tab

1. Call `GET /users/meetings?view=upcoming`.
2. Group by day/week on the frontend using `start_at`.
3. Show create/link/bot actions using `linked_record` and `bot`.

### Past Tab

1. Call `GET /users/meetings?view=past`.
2. Use `has_recording`, `has_summary`, and `transcript_status` to choose between "View recording",
   "View summary", "Create follow-up", or "Open".

### Recordings Tab

1. Call `GET /users/meetings?view=recordings`.
2. Use `GET /users/meetings/{event_id}/recording` only when the user opens a recording.
3. Handle `409` as "recording not ready" rather than a fatal error.

## Existing APIs To Reuse From Frontend

Do not duplicate these workflows in the Meetings frontend:

- Deal prep/summary/follow-up jobs:
  - `/users/crm/deals/{deal_id}/meetings/{event_id}/jobs/{briefing|summary|followup}`
- Deal meeting list:
  - `/users/crm/deals/{deal_id}/meetings`
- Manual transcript upload for deal meetings:
  - existing deal transcript presign/finalize endpoints
- Task creation/list/update:
  - `/users/crm/tasks`
  - `/users/crm/deals/{deal_id}/tasks`

For follow-up tasks from Meetings, use the existing tasks API. The task API now supports:

```json
{
  "subject": "Send revised proposal",
  "related_object_type": "LEAD",
  "related_local_id": "0d0bb5a3-7238-4a1c-9989-010f310671dd"
}
```

For deal tasks, the existing `deal_id` field and deal-specific task endpoint remain supported.

## Security And Product Assumptions

- Every meeting read/write proves current-user calendar ownership.
- Every record link validates the target record in the current organisation.
- Account, internal, and none-linked workflows are intentionally v2.
- Auto bot invite applies to external meetings linked to deal, lead, or contact unless disabled
  by per-meeting preference.
- Existing deal meeting routes and deal task routes are unchanged.
