# Meetings Tab API

Top-level Meetings APIs are mounted at `/users/meetings`.

V1 is deal-only. The backend does not create meeting-link, bot-preference, task-link,
account, lead, contact, internal, or none-linked tables. Meeting context comes from existing
calendar sync tables and the existing `CalendarEventDealAssociation` table.

Existing deal-scoped meeting APIs remain unchanged under `/users/crm/deals` for meeting prep,
summary, follow-up, transcript upload, and deal timeline workflows.

## Frontend Scope

Build these V1 tabs:

| Tab | Backend view | Purpose |
| --- | --- | --- |
| Today | `view=today` | Meetings in the current user's local day. |
| Upcoming | `view=upcoming` | Future synced meetings. |
| Past | `view=past` | Completed historical meetings. |
| Recordings | `view=recordings` | Meetings with Recall recording sessions. |

Supported V1 linked record:

```ts
type MeetingLinkedRecordType = "DEAL";
```

Hide or disable account, lead, contact, internal, and none-linked business workflows in V1.
Those drawers can exist as frontend placeholders, but they should not call backend link/task
workflows yet.

## Response Envelope

All endpoints return the shared API envelope:

```ts
type ApiResponse<T> = {
  status: "SUCCESS" | "ERROR";
  message: string;
  data: T | null;
};
```

## Shared Contracts

```ts
type MeetingsView = "today" | "upcoming" | "past" | "recordings";

type MeetingBotComputedStatus =
  | "recording_live"
  | "already_sent"
  | "failed"
  | "cancelled"
  | "auto_invite_eligible"
  | "invite_available";

type MeetingBotDisplayStatus =
  | "invite_available"
  | "auto_invite_eligible"
  | "not_requested"
  | "bot_queued"
  | "bot_invited"
  | "recording_live"
  | "recording_available"
  | "bot_did_not_join"
  | "recording_permission_denied"
  | "recording_failed"
  | "transcript_failed"
  | "bot_failed"
  | "bot_removed"
  | "not_available";

type MeetingAttendeeItem = {
  email: string;
  name: string | null;
  response_status: string | null;
  is_organiser: boolean;
};

type MeetingLinkedRecord = {
  record_type: "DEAL";
  record_id: string;
  name: string | null;
  metadata: {
    stage?: string | null;
    amount?: number;
    [key: string]: unknown;
  };
};

type MeetingBotStatus = {
  status: MeetingBotComputedStatus | string;
  display_status: MeetingBotDisplayStatus | string | null;
  reason: string | null;
  is_external: boolean;
  provider_session_id: string | null;
  can_invite: boolean;
  can_remove: boolean;
};

type MeetingListItem = {
  event_id: string;
  title: string | null;
  start_at: string;
  end_at: string;
  duration_minutes: number | null;
  platform: string | null;
  platform_label: "Google Meet" | "Microsoft Teams" | "Zoom" | "Webex" | "Not available";
  conference_link: string | null;
  status: string | null;
  attendees: MeetingAttendeeItem[];
  attendee_count: number;
  linked_record: MeetingLinkedRecord | null;
  bot: MeetingBotStatus;
  recording_status: string | null;
  recording_availability: "Not available" | "Processing" | "Available";
  transcript_status: string | null;
  has_recording: boolean;
  has_summary: boolean;
  has_prep: boolean;
  next_action: "prepare" | "view_recording" | string | null;
};

type MeetingsPage = {
  items: MeetingListItem[];
  total_count: number;
  page: number;
  limit: number;
};
```

## List Meetings

```http
GET /users/meetings
```

Use this endpoint for all four list tabs.

Query parameters:

| Name | Type | Required | Notes |
| --- | --- | --- | --- |
| `view` | `today | upcoming | past | recordings` | No | Defaults to `today`. |
| `start_at` | ISO datetime | No | Optional explicit window start. |
| `end_at` | ISO datetime | No | Optional explicit window end. |
| `q` | string | No | Searches title, organiser, attendee email, and attendee name. |
| `linked_type` | `DEAL` | No | Filters to meetings with an active deal association. |
| `bot_status` | string | No | Filters by computed bot status. |
| `recording_status` | string | No | Filters Recall recording session status. |
| `page` | number | No | Default `1`. |
| `limit` | number | No | Default `50`, max `100`. |

Default backend windows:

- `today`: current user's local day.
- `upcoming`: after today through roughly the next 90 days.
- `past`: previous 180 days up to the start of today.
- `recordings`: previous 180 days through roughly the next 90 days, filtered to Recall sessions.

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
        "platform_label": "Microsoft Teams",
        "conference_link": "https://teams.microsoft.com/l/meetup-join/...",
        "status": "confirmed",
        "attendees": [
          {
            "email": "priya@example.com",
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
          "status": "recording_live",
          "display_status": "recording_live",
          "reason": null,
          "is_external": true,
          "provider_session_id": "bot_abc123",
          "can_invite": false,
          "can_remove": true
        },
        "recording_status": "IN_CALL",
        "recording_availability": "Processing",
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

Frontend rendering notes:

- Use `linked_record` for the "Linked to" deal chip.
- If `linked_record` is `null`, show an unlinked or "Link deal" state.
- Use `platform_label` for the platform column; raw `platform` remains for compatibility.
- Use `bot.display_status` for user-facing bot and recording copy. Use `bot.status` only for
  coarse grouping and existing filters.
- Use `bot.can_invite` / `bot.can_remove` for action availability.
- Use `recording_availability` for the recording column; raw recording fields remain for detail flows.
- Use `next_action` as a backend hint; frontend can still choose tab-specific button labels.

## Get Meeting Detail

```http
GET /users/meetings/{event_id}
```

Use this when a row is selected and the side drawer opens.

```ts
type MeetingDetail = MeetingListItem & {
  description: string | null;
  organiser_name: string | null;
  organiser_email: string | null;
  linked_record_detail: Record<string, unknown> | null;
  suggested_actions: Array<Record<string, unknown>>;
};
```

Example:

```http
GET /users/meetings/4fd09b96-9f31-4dd4-9328-8f9b9f1bb227
```

Drawer behavior:

- If `linked_record?.record_type === "DEAL"`, render the deal drawer variant.
- Use `linked_record_detail` for compact deal context.
- Use `attendees` and `attendee_count` for attendee stack/list.
- Use `suggested_actions` as optional hints. In V1, follow-up creation should still use existing deal task endpoints.

Security behavior:

- The backend only returns meetings visible through the authenticated user's active Google or Outlook calendar integration.
- A raw `event_id` from another user's calendar returns `404`.

## Create Meeting

```http
POST /users/meetings
```

Creates a provider-backed calendar invite through `CalendarInviteService`.

```ts
type CreateMeetingRequest = {
  title: string;
  start_at: string;
  end_at: string;
  timezone: string;
  attendees: Array<{
    email: string;
    name?: string | null;
    optional?: boolean;
  }>;
  body?: string | null;
  location?: string | null;
  sender_email?: string | null;
  idempotency_key?: string | null;
  link?: {
    record_type: "DEAL";
    record_id: string;
  } | null;
};

type CreateMeetingResponse = {
  event_id: string;
  provider_event_id: string;
  conference_link: string | null;
  meeting: MeetingDetail;
  bot_dispatch: Record<string, unknown> | null;
};
```

Important frontend rules:

- Send at least one attendee explicitly. V1 does not auto-prefill lead/contact participants.
- `start_at` and `end_at` must include timezone.
- `end_at` must be after `start_at`.
- If linking on create, only send `link.record_type = "DEAL"`.
- `idempotency_key` is optional but recommended for retry-safe create flows.

Example request:

```json
{
  "title": "Discovery with Acme leadership",
  "start_at": "2026-06-04T10:30:00+05:30",
  "end_at": "2026-06-04T11:00:00+05:30",
  "timezone": "Asia/Kolkata",
  "attendees": [
    {
      "email": "john@acme.com",
      "name": "John Davis"
    }
  ],
  "body": "Discovery agenda",
  "link": {
    "record_type": "DEAL",
    "record_id": "2ef6e7fc-23f5-4126-b8e6-4ca748afe840"
  },
  "idempotency_key": "meeting-create-4fd09b96"
}
```

After creation:

- The backend passes `link.record_id` as `deal_id` to the existing invite service.
- The resulting synced calendar event is linked through `CalendarEventDealAssociation`.
- If the meeting is external and linked to a deal, backend attempts safe auto-invite of the bot after save.
- If auto-invite fails, the meeting is still created and `bot_dispatch` contains a failed status.

## Link Meeting To Deal

```http
PATCH /users/meetings/{event_id}/link
```

Request:

```json
{
  "record_type": "DEAL",
  "record_id": "2ef6e7fc-23f5-4126-b8e6-4ca748afe840"
}
```

Response data: `MeetingDetail`.

Behavior:

- Validates current-user ownership of the calendar event.
- Validates the deal belongs to the same organisation and is not deleted.
- Creates or reactivates a `CalendarEventDealAssociation` for the selected deal.
- Does not remove other existing deal associations; this keeps existing deal timelines and jobs intact.
- Attempts safe bot dispatch after save when the synced calendar event has a join link.

Frontend behavior:

- Use this for "Link deal" and "Change link" in V1.
- Do not send `LEAD`, `CONTACT`, `ACCOUNT`, `INTERNAL`, or `NONE`.
- Search UI can show only deals for this version.

## Invite Bot

```http
POST /users/meetings/{event_id}/bot/invite
```

Manually dispatches the existing Recall bot flow after proving current-user ownership of the
calendar event.

Possible success payload examples:

```json
{
  "status": "sent",
  "calendar_event_id": "4fd09b96-9f31-4dd4-9328-8f9b9f1bb227",
  "provider_session_id": "bot_abc123"
}
```

```json
{
  "status": "already_sent",
  "calendar_event_id": "4fd09b96-9f31-4dd4-9328-8f9b9f1bb227"
}
```

If the existing dispatch service determines the bot cannot join late, the API returns `409`.

Bot status mapping for the list and drawer:

| `bot.status` | Suggested UI |
| --- | --- |
| `recording_live` | Green "Recording live" chip; primary action can open meeting. |
| `already_sent` | "Bot invited" or "Recording scheduled" chip. |
| `failed` | Use `bot.display_status` for the exact failure copy. |
| `cancelled` | Bot removed/cancelled state. |
| `auto_invite_eligible` | "Bot auto-invite eligible" or "Invite bot" action. |
| `invite_available` | Show "Invite bot" if meeting has a join link and is not past. |

Display status mapping:

| `bot.display_status` | Suggested UI copy |
| --- | --- |
| `invite_available` | Invite bot |
| `auto_invite_eligible` | Bot auto-invite eligible |
| `not_requested` | Bot not invited |
| `bot_queued` | Bot invite queued |
| `bot_invited` | Bot invited |
| `recording_live` | Recording live |
| `recording_available` | Recording available |
| `bot_did_not_join` | Bot did not join |
| `recording_permission_denied` | Host denied recording |
| `recording_failed` | Recording failed |
| `transcript_failed` | Transcript failed |
| `bot_failed` | Bot failed |
| `bot_removed` | Bot removed |
| `not_available` | Not available |

Do not show "Transcript failed" just because `transcript_status=FAILED` when
`bot.display_status` is `bot_did_not_join`, `recording_permission_denied`, or
`recording_failed`. In those cases the transcript failed because the bot or recording
flow failed first.

There is no V1 `bot-settings` endpoint because per-meeting bot preferences would require new storage.

## Remove Bot

```http
DELETE /users/meetings/{event_id}/bot/invite
```

Cancels a scheduled Recall bot for a current-user meeting. Removal is only available before
the bot has joined or completed the call.

Frontend behavior:

- Show the hover remove affordance only when `bot.can_remove === true`.
- Past meetings should not show invite or remove actions.
- If a user cancels and later clicks invite again, the backend reuses the same recording row and
  creates at most one replacement provider bot.

## Recording Detail

```http
GET /users/meetings/{event_id}/recording
```

Returns Recall playback and transcript metadata for one current-user meeting. V1 supports Recall
bot recordings only.

```ts
type MeetingRecordingResponse = {
  event_id: string;
  recording_status: string | null;
  transcript_status: string | null;
  playback_url: string | null;
  download_url: string | null;
  video_blob_path: string | null;
  video_blob_url: string | null;
  video_content_type: string | null;
  video_size_bytes: number | null;
  video_filename: string | null;
  provider_session_id: string | null;
  transcript_artifact: Record<string, unknown> | null;
  summary: Record<string, unknown> | null;
  action_items: Array<Record<string, unknown>>;
  transcript_segments: Array<{
    start_time: number | null;
    speaker_id: string | null;
    speaker_name: string | null;
    text: string;
  }>;
};
```

Example:

```http
GET /users/meetings/4fd09b96-9f31-4dd4-9328-8f9b9f1bb227/recording
```

Frontend behavior:

- Use `playback_url` as a fresh inline playback URL.
- Use `download_url` for the download action. It points at the same archived video when
  available, but includes download disposition metadata.
- `playback_url` and `download_url` are short-lived URLs. Fetch this endpoint again when
  opening playback or starting a download.
- `video_blob_path` is the stable organisation-scoped blob key. `video_blob_url` is the stable
  non-SAS blob URL for diagnostics and metadata display only; do not use it for playback.
- `video_content_type`, `video_size_bytes`, and `video_filename` are optional display metadata.
- Older rows may still fall back to Recall's signed URL in `playback_url`; those rows can have
  `download_url = null` until the video is archived into organisation storage.
- Use `summary` and structured `action_items` for the intelligence panel when present.
- Use `transcript_segments` for transcript rows. The backend returns `[]` when no segment rows
  are available and does not expose the raw full transcript text.
- Use `transcript_artifact` for transcript availability metadata.

If no playable archived or Recall video artifact is available, backend returns `409`.

## Meeting Summary Notification Deep Link

When a deal-linked meeting summary is generated, backend notifications link to the existing deal
briefing-room route:

```text
{APP_URL}/deals/{deal_id}/briefing-room?meeting={event_id}&summary=1
```

Notification content is link-only. Email, Slack, and Teams notifications must not include the
generated summary body, transcript snippets, or deal notes.

Email recipients are limited to active approved internal users in the same organisation whose email
appears on the target calendar meeting attendee list. External attendees are not notified.

The frontend auth flow must preserve both the path and query string through login, then select the
`meeting` id and focus/open the summary UI when `summary=1` is present.

## Error Handling

Common frontend cases:

| HTTP status | Likely reason | UI recommendation |
| --- | --- | --- |
| `400` | Invalid query/body, unsupported link type, bad datetime. | Show validation error. |
| `403` | CRM write scope disabled by org preferences. | Disable write action or show permission message. |
| `404` | Meeting not visible to current user's calendar, or deal not found. | Refresh list and show not-found toast. |
| `409` | Recording not ready, idempotency conflict, or bot cannot join late. | Show contextual conflict message. |
| `500/502` | Provider/backend failure. | Keep existing UI state and show retry option. |

## V1 Exclusions

Do not build backend-dependent workflows for these yet:

- Lead-linked meetings.
- Contact-linked meetings.
- Account-linked meetings.
- Internal meeting drawers/actions.
- None-linked business workflows.
- Per-meeting bot preference settings.
- Lead/contact/account/internal task creation from Meetings.

For follow-up creation in V1, use the existing deal task endpoints after confirming the meeting
is linked to a deal.
