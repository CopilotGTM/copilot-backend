# Scheduled Agents API

`/users/agents`

Use this API for user-owned scheduled agents: time-triggered AI prompts delivered through
Teams, Slack, or WhatsApp. The backing storage, scheduler, execution worker, history rows, and
ownership checks remain the existing `UserReminder` implementation with
`reminder_type=agentic`.

## Current Scope

- This API supports time-triggered scheduled agents only.
- Future email-triggered agents and workflow attachment are not part of this change.
- Static fixed-message reminders live under `/users/reminders`.
- Responses still include `reminder_type` because the stored model includes it.

## Channels

Check which delivery channels are ready for scheduled-agent delivery.

```http
GET /users/agents/channels
```

The response shape matches `GET /users/reminders/channels`.

## Create Scheduled Agent

```http
POST /users/agents
```

Request body:

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `title` | string | Yes | Short label, max 255 chars |
| `agent_prompt` | string | Yes | Self-contained prompt to run at trigger time |
| `deal_id` | UUID | Yes | Deal scope for the agent |
| `trigger_time` | datetime | Yes | Future ISO 8601 timestamp with timezone |
| `timezone` | string | Yes | IANA timezone, for example `Asia/Kolkata` |
| `recurrence` | string | No | `none`, `daily`, `weekdays`, `weekly`, `biweekly`, `monthly` |
| `recurrence_end_at` | datetime \| null | No | Optional recurrence end |
| `delivery_channel` | string | Yes | `teams`, `slack`, or `whatsapp` |

`reminder_type` and `message_content` are not accepted. `deal_id` is required.

Example:

```json
{
  "title": "Weekly deal health check",
  "agent_prompt": "Review this deal and summarize risks, blockers, and next actions.",
  "deal_id": "7b82229a-1088-4fb4-9cb2-839f423829f7",
  "trigger_time": "2026-06-05T09:00:00+05:30",
  "timezone": "Asia/Kolkata",
  "recurrence": "weekly",
  "delivery_channel": "teams"
}
```

## List Scheduled Agents

```http
GET /users/agents?status=active,paused&page=1&limit=20&sort=desc
```

Query parameters:

| Field | Type | Notes |
| --- | --- | --- |
| `status` | string | Optional comma-separated status filter |
| `page` | integer | 1-indexed page, default `1` |
| `limit` | integer | Default `20`, max `100` |
| `sort` | string | `asc` or `desc` by next trigger time |

The endpoint always applies an agentic-reminder filter. There is no caller-provided
`reminder_type` filter.

## Get Scheduled Agent

```http
GET /users/agents/{reminder_id}
```

Returns `404` if the record is missing, owned by another user/org, or is a static reminder.

## History

```http
GET /users/agents/{reminder_id}/history?page=1&limit=20
```

Returns execution attempts for a scheduled agent only.

## Update Scheduled Agent

```http
PATCH /users/agents/{reminder_id}
```

Editable fields:

- `title`
- `agent_prompt`
- `deal_id`
- `trigger_time`
- `timezone`
- `recurrence`
- `recurrence_end_at`
- `delivery_channel`

`message_content` is not accepted.

## Lifecycle

```http
POST /users/agents/{reminder_id}/pause
POST /users/agents/{reminder_id}/resume
POST /users/agents/{reminder_id}/cancel
DELETE /users/agents/{reminder_id}
```

All lifecycle endpoints operate on scheduled agents only and return `404` for static reminders.
