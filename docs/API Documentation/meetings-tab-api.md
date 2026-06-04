# Meetings Tab API

Top-level Meetings APIs are mounted at `/users/meetings`.

V1 is intentionally deal-first. The backend does not create new meeting-link,
bot-preference, task-link, account, lead, contact, internal, or none-linked tables for this
version. Meeting context comes from existing calendar sync rows and the existing
`CalendarEventDealAssociation` mapping.

Existing deal-scoped meeting APIs remain unchanged under `/users/crm/deals` for briefing,
summary, follow-up, transcript upload, and deal timeline behavior.

## V1 Scope

- Lists the authenticated user's synced calendar meetings across Today, Upcoming, Past, and Recordings.
- Supports links to `DEAL` only.
- Uses existing `CalendarEventDealAssociation` rows for the linked deal chip and drawer context.
- Creates meetings through the existing calendar invite service.
- Invites the Recall bot through the existing meeting-bot dispatch service.
- Returns Recall recording playback/transcript metadata when a Recall session exists.
- Keeps task APIs deal-only for now. Lead/contact/account/internal task workflows are not part of V1.

## Shared Types

```ts
type MeetingsView = "today" | "upcoming" | "past" | "recordings";
type MeetingLinkedRecordType = "DEAL";

type MeetingBotComputedStatus =
  | "recording_live"
  | "already_sent"
  | "auto_invite_eligible"
  | "invite_available";
```

```ts
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
  reason: string | null;
  is_external: boolean;
  provider_session_id: string | null;
};

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
```

## List Meetings

```http
GET /users/meetings
```

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

Default view windows:

- `today`: current user's local day.
- `upcoming`: after today through roughly the next 90 days.
- `past`: previous 180 days up to the start of today.
- `recordings`: previous 180 days through roughly the next 90 days, filtered to Recall sessions.

Example:

```http
GET /users/meetings?view=today&page=1&limit=50
```

## Get Meeting Detail

```http
GET /users/meetings/{event_id}
```

Use this when a row is selected and the drawer opens.

```ts
type MeetingDetail = MeetingListItem & {
  description: string | null;
  organiser_name: string | null;
  organiser_email: string | null;
  linked_record_detail: Record<string, unknown> | null;
  suggested_actions: Array<Record<string, unknown>>;
};
```

Security note: every lookup proves the event belongs to the current user's active Google or
Outlook calendar integration before returning data.

## Create Meeting

```http
POST /users/meetings
```

Creates a provider-backed calendar invite through `CalendarInviteService`. The frontend must
send attendees explicitly. V1 does not auto-prefill lead/contact participants.

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
```

The backend passes `link.record_id` as `deal_id` to the existing invite service and mirrors the
result into `CalendarEventDealAssociation` when the provider returns a synced event id.

## Link Meeting To Deal

```http
PATCH /users/meetings/{event_id}/link
```

```json
{
  "record_type": "DEAL",
  "record_id": "2ef6e7fc-23f5-4126-b8e6-4ca748afe840"
}
```

Behavior:

- Validates current-user ownership of the calendar event.
- Validates the deal belongs to the same organisation and is not deleted.
- Creates or reactivates a `CalendarEventDealAssociation` for the selected deal.
- Does not remove other existing deal associations; this keeps existing deal timelines and jobs intact.

## Invite Bot

```http
POST /users/meetings/{event_id}/bot/invite
```

Manually dispatches the existing Recall bot flow after proving current-user ownership of the
calendar event. If the existing dispatch service determines the bot cannot join late, the API
returns a conflict response.

There is no V1 `bot-settings` endpoint because per-meeting bot preferences would require new
storage. The computed default is:

- `recording_live` when Recall is joining or in call.
- `already_sent` when the Recall session has already been sent to the provider.
- `auto_invite_eligible` when the meeting is external and linked to a deal.
- `invite_available` otherwise.

## Recording Detail

```http
GET /users/meetings/{event_id}/recording
```

Returns Recall playback and transcript metadata for one current-user meeting. V1 supports
Recall bot recordings only.

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

If no playable Recall video artifact is available, the endpoint returns `409` with
`recording_not_ready` semantics.

## Frontend Notes

- Render account, lead, contact, internal, and none-linked drawers as future states or hide those
  workflows in V1.
- Use `linked_record` for the deal chip and drawer CTA.
- Use `has_prep`, `has_summary`, `recording_status`, and `transcript_status` to decide row actions.
- Use existing deal task endpoints for follow-up creation. Do not call lead/contact/account task
  workflows from the Meetings tab in V1.
