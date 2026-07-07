# Meeting Recording Preferences API

Authenticated tenant users can read and update the organisation-wide meeting bot auto-join policy.

Base URL:

```text
/users/preference
```

The setting is organisation-wide for now: when any authenticated organisation user updates it, the value changes for all users in that organisation.

## Get Meeting Recording Policy

```http
GET /meeting-recording
```

Response:

```json
{
  "status": "SUCCESS",
  "message": "Meeting recording policy retrieved successfully",
  "data": {
    "bot_join_policy": "external_only",
    "bot_join_policy_options": [
      {"value": "external_only", "label": "External meetings only"},
      {"value": "all", "label": "All meetings"},
      {"value": "never", "label": "Never join"}
    ]
  }
}
```

## Update Meeting Recording Policy

```http
PATCH /meeting-recording
```

Request:

```json
{
  "bot_join_policy": "external_only"
}
```

Allowed values:

| Value | Description |
| --- | --- |
| `external_only` | Join only otherwise eligible meetings with at least one attendee outside the organisation domains. |
| `all` | Join all otherwise eligible meetings. |
| `never` | Do not join automatically or through user-facing manual invite surfaces. |

Missing organisation preferences default to `external_only`.
