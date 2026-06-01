# CRM Meetings API

Top-level user-owned CRM meeting section. These endpoints are distinct from the
deal-scoped meeting intelligence APIs under `/users/crm/deals/{deal_id}/meetings`.

Base path: `/users/crm/meetings`

## Security Model

Every endpoint is scoped to the authenticated user's organisation and calendar
ownership. The server does not trust a raw `event_id` by itself. Meeting access is
resolved through:

- `calendar_events.organisation_id`
- `calendar_event_user_states.event_id`
- `knowledge_integrations.integration_id`
- `knowledge_integrations.organisation_id`
- `knowledge_integrations.created_by_user_id`
- active Google Calendar or Outlook Calendar integration
- non-deleted calendar user-state row

Responses intentionally exclude raw calendar payloads, provider payloads, and
full attendee payloads.

## List Meetings

```http
GET /users/crm/meetings
```

Query parameters:

| Parameter | Type | Notes |
| --- | --- | --- |
| `page` | integer | Default `1`, minimum `1` |
| `limit` | integer | Default `50`, maximum `100` |
| `start_from` | datetime | Optional overlap-window start |
| `start_to` | datetime | Optional overlap-window end |
| `time_scope` | string | `upcoming`, `past`, or `all` |
| `recording_status` | string | `NOT_REQUESTED` or a recording status |
| `linked_deal` | boolean | `true` for meetings with active deal links, `false` for standalone meetings |

Example response:

```json
{
  "status": "SUCCESS",
  "message": "Meetings retrieved successfully",
  "data": {
    "items": [
      {
        "event_id": "5ad3f8c4-7f2a-4b1e-bc6c-9e2a78fbf3cf",
        "title": "Customer kickoff",
        "start_at": "2026-06-03T15:00:00Z",
        "end_at": "2026-06-03T15:30:00Z",
        "status": "confirmed",
        "location": null,
        "conference_link": "https://meet.google.com/abc-defg-hij",
        "online_meeting_provider": "google_meet",
        "organiser_name": "Alex Morgan",
        "organiser_email": "alex@example.com",
        "attendee_count": 4,
        "linked_deals": [
          {
            "deal_id": "76a4b79d-6bc2-4ab2-b703-8e1627168b6d",
            "name": "Acme expansion",
            "company_name": "Acme",
            "amount": 25000,
            "currency": "USD",
            "stage_name": "Proposal",
            "stage_label": "Proposal"
          }
        ],
        "recording": {
          "session_id": null,
          "status": "NOT_REQUESTED",
          "transcript_status": null,
          "provider": null,
          "provider_status": null,
          "join_at": null,
          "sent_to_provider_at": null
        },
        "can_invite_bot": true,
        "can_remove_bot": false
      }
    ],
    "page": 1,
    "limit": 50,
    "total_count": 1,
    "total_pages": 1
  }
}
```

## Invite Recording Bot

```http
POST /users/crm/meetings/{event_id}/recording-bot
```

The endpoint first proves the event belongs to the authenticated user's active
calendar integration, then releases that DB request scope before invoking the
Recall-backed dispatch service.

Typical response:

```json
{
  "status": "SUCCESS",
  "message": "Recording bot invite processed",
  "data": {
    "status": "sent",
    "calendar_event_id": "5ad3f8c4-7f2a-4b1e-bc6c-9e2a78fbf3cf",
    "provider_session_id": "bot_123"
  }
}
```

Frontend note: render the invite action when `can_invite_bot=true`. Provider
dispatch may still return safe non-send states such as
`missing_meeting_url`, `cancelled`, `meeting_already_started`, or
`recording_cancelled`.

## Remove Recording Bot

```http
DELETE /users/crm/meetings/{event_id}/recording-bot
```

This is v1 scheduled-cancel behavior only:

- unsent sessions are marked `CANCELLED`
- sent scheduled sessions call Recall delete, then mark `CANCELLED`
- provider delete failures still persist the user opt-out as `CANCELLED` with
  compact failure metadata; a later remove request retries provider cleanup
- `JOINING`, `IN_CALL`, `COMPLETED`, and transcript-ready sessions return `409`
- this endpoint does not call Recall `leave_call`

Typical response:

```json
{
  "status": "SUCCESS",
  "message": "Recording bot cancelled",
  "data": {
    "status": "cancelled",
    "calendar_event_id": "5ad3f8c4-7f2a-4b1e-bc6c-9e2a78fbf3cf"
  }
}
```

Frontend note: render the remove action only when `can_remove_bot=true`. For
active or completed recordings, show the status as non-removable.
