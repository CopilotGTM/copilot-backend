# Static Reminders API

`/users/reminders`

Use this API for user-owned static reminders: scheduled messages that deliver fixed
`message_content` through Teams, Slack, or WhatsApp. Scheduled AI agents now live under
`/users/agents`.

## Scope

- Reminders are scoped to the authenticated user and organization.
- All endpoints operate on static reminders only (`reminder_type=static` internally).
- Agent prompts and deal-scoped agent configuration are not accepted on this API.
- Responses still include `reminder_type` for compatibility with existing clients.

## Channels

Check which delivery channels are ready for proactive delivery.

```http
GET /users/reminders/channels
```

Returns:

```json
{
  "success": true,
  "message": "Available channels retrieved successfully",
  "data": {
    "channels": [
      {
        "channel": "teams",
        "available": true,
        "label": "Microsoft Teams",
        "mapping_id": "..."
      }
    ]
  }
}
```

## Create Static Reminder

```http
POST /users/reminders
```

Request body:

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `title` | string | Yes | Short label, max 255 chars |
| `message_content` | string | Yes | Fixed message to send |
| `trigger_time` | datetime | Yes | Future ISO 8601 timestamp with timezone |
| `timezone` | string | Yes | IANA timezone, for example `Asia/Kolkata` |
| `recurrence` | string | No | `none`, `daily`, `weekdays`, `weekly`, `biweekly`, `monthly` |
| `recurrence_end_at` | datetime \| null | No | Optional recurrence end |
| `delivery_channel` | string | Yes | `teams`, `slack`, or `whatsapp` |

`reminder_type`, `agent_prompt`, and `deal_id` are not accepted.

Example:

```json
{
  "title": "Follow up with Acme",
  "message_content": "Send Acme the pricing update.",
  "trigger_time": "2026-06-03T09:00:00+05:30",
  "timezone": "Asia/Kolkata",
  "recurrence": "none",
  "delivery_channel": "teams"
}
```

## List Static Reminders

```http
GET /users/reminders?status=active,paused&page=1&limit=20&sort=desc
```

Query parameters:

| Field | Type | Notes |
| --- | --- | --- |
| `status` | string | Optional comma-separated status filter |
| `page` | integer | 1-indexed page, default `1` |
| `limit` | integer | Default `20`, max `100` |
| `sort` | string | `asc` or `desc` by next trigger time |

The endpoint always applies a static-reminder filter. There is no caller-provided
`reminder_type` filter.

## Get Static Reminder

```http
GET /users/reminders/{reminder_id}
```

Returns `404` if the record is missing, owned by another user/org, or is a scheduled agent.

## History

```http
GET /users/reminders/{reminder_id}/history?page=1&limit=20
```

Returns execution attempts for a static reminder only. Scheduled agent history is available at
`/users/agents/{reminder_id}/history`.

## Update Static Reminder

```http
PATCH /users/reminders/{reminder_id}
```

Editable fields:

- `title`
- `message_content`
- `trigger_time`
- `timezone`
- `recurrence`
- `recurrence_end_at`
- `delivery_channel`

`agent_prompt` and `deal_id` are not accepted.

## Lifecycle

```http
POST /users/reminders/{reminder_id}/pause
POST /users/reminders/{reminder_id}/resume
POST /users/reminders/{reminder_id}/cancel
DELETE /users/reminders/{reminder_id}
```

All lifecycle endpoints operate on static reminders only and return `404` for scheduled agents.
