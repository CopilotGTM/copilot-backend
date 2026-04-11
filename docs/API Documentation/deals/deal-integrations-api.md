# Deal Integrations API Documentation

## Base URL

```text
/users/integrations
```

---

## 1. List Available Integrations for Deal

### List Deal Integrations Endpoint

```http
GET /deals/{deal_id}/available
```

### List Deal Integrations Description

Returns available integrations specifically for deal context (Slack and Teams).
Shows which integrations are connected for the current user and which canonical
channels are already linked to the specified deal.

**Note:** Messaging integrations are **user-scoped connections** with **org-scoped canonical
channels**. This endpoint filters integrations based on the current user's access. It returns:

- Active Slack/Teams integrations created by the current user
- Deal-linked canonical channels regardless of which user originally linked them

### List Deal Integrations Authentication

Required - Bearer token

### List Deal Integrations Request

**Path Parameters:**

- `deal_id` (UUID, required): Context deal ID.

### List Deal Integrations Response (200 OK)

```json
{
  "status": "SUCCESS",
  "message": "Available deal integrations retrieved successfully",
  "data": {
    "integrations": [
      {
        "integration_type": "slack",
        "name": "Slack",
        "description": "Sync messages, files, and channel data from Slack workspaces",
        "logo_url": "https://a.slack-edge.com/80588/marketing/img/icons/icon_slack_hash_colored.png",
        "category": "collaboration",
        "category_name": "Collaboration & Communication",
        "availability_status": "available",
        "is_connected": true,
        "connected_integrations": [
          {
            "integration_id": "123e4567-e89b-12d3-a456-426614174000",
            "organisation_id": "org-uuid",
            "integration_type": "slack",
            "name": "Acme Corp Slack",
            "created_by_user_id": "admin-uuid",
            "integration_email": null,
            "integration_scope": "user",
            "is_active": true,
            "created_at": "2025-10-01T08:00:00Z",
            "updated_at": "2025-10-05T10:30:00Z"
          }
        ],
        "linked_channels": [
          {
            "channel_id": "uuid-of-channel",
            "external_channel_id": "T12345:C12345",
            "name": "general",
            "type": "public",
            "integration_id": "123e4567-e89b-12d3-a456-426614174000",
            "integration_name": "Acme Corp Slack",
            "parent_name": "Acme Corp Workspace"
          }
        ]
      }
    ],
    "total_available": 1,
    "total_connected": 1
  }
}
```

---

## 2. List Channels for Integration Type

### List Integration Channels Endpoint

```http
GET /deals/{deal_id}/channels/{integration_type}
```

### List Integration Channels Description

Fetch channels visible through the current user's connected integrations of a specific
provider type (`teams` or `slack`). This is used to populate the channel selection dropdown
when linking a channel to a deal. Channels already linked to that deal are excluded automatically.

### List Integration Channels Authentication

Required - Bearer token

### List Integration Channels Request

**Path Parameters:**

- `deal_id` (UUID, required): Deal context used to filter out already linked channels.
- `integration_type` (string, required): `teams` or `slack`.

### List Integration Channels Response (200 OK)

```json
{
  "status": "SUCCESS",
  "message": "Channels retrieved successfully",
  "data": {
    "channels": [
      {
        "id": "team_1_channel_general",
        "name": "General",
        "integration_id": "123e4567-e89b-12d3-a456-426614174000",
        "type": "public",
        "type_label": "Public Channel",
        "parent_id": "team_1",
        "parent_name": "Sales Team",
        "description": "General discussion channel for the sales team",
        "member_count": 12,
        "members": [
          {
            "name": "John Smith",
            "email": "john.smith@acme.com",
            "role": "owner"
          },
          {
            "name": "Jane Doe",
            "email": "jane.doe@acme.com",
            "role": "member"
          }
        ],
        "last_activity_at": "2025-01-20T15:30:00Z"
      },
      {
        "id": "meeting_chat_abc123",
        "name": "Acme Deal Review Meeting",
        "integration_id": "223e4567-e89b-12d3-a456-426614174000",
        "type": "meeting",
        "type_label": "Meeting Chat",
        "parent_id": null,
        "parent_name": null,
        "description": null,
        "member_count": 5,
        "members": [
          {
            "name": "Alice Johnson",
            "email": "alice@acme.com",
            "role": "member"
          }
        ],
        "last_activity_at": "2025-01-19T10:00:00Z"
      }
    ],
    "total": 2
  }
}
```

### Response Fields

| Field | Type | Description |
| --- | --- | --- |
| `id` | string | External channel ID from the provider |
| `name` | string | Channel display name |
| `integration_id` | UUID | Exact user integration to use when linking this channel |
| `type` | string | Channel type: `public`, `private`, `direct_message`, `group_message`, `meeting`, `thread` |
| `type_label` | string | Human-readable label (e.g., "Public Channel", "Meeting Chat", "Private Channel") |
| `parent_id` | string\|null | Parent ID (e.g., Team ID for Teams channels) |
| `parent_name` | string\|null | Parent name (e.g., Team name) |
| `description` | string\|null | Channel description or topic |
| `member_count` | integer | Total number of members in the channel |
| `members` | array | List of channel members (up to 10 for display) |
| `members[].name` | string\|null | Member display name |
| `members[].email` | string\|null | Member email address |
| `members[].role` | string\|null | Role in channel (owner, admin, member, guest) |
| `last_activity_at` | string\|null | ISO 8601 timestamp of the most recent message |

---

## 3. Link Channel to Deal

### Link Channel Endpoint

```http
POST /deals/{deal_id}/channels
```

### Link Channel Description

Links a specific messaging channel (Slack/Teams) to a deal.
This enables message syncing for that channel in the context of the deal.
The request must include the `integration_id` returned by the list-channels API so the backend
can bind the canonical channel to the exact user integration that can access it.

### Link Channel Authentication

Required - Bearer token

### Link Channel Request

**Path Parameters:**

- `deal_id` (UUID, required): The ID of the deal to link.

**Body Parameters:**

```json
{
  "id": "string",
  "name": "string",
  "type": "string",
  "integration_id": "uuid",
  "parent_id": "string | null",
  "parent_name": "string | null"
}
```

| Field | Type | Description |
| --- | --- | --- |
| `id` | string | Channel ID (from List Channels API) |
| `name` | string | Channel Name |
| `type` | string | Channel Type |
| `integration_id` | UUID | Integration ID this channel belongs to |
| `parent_id` | string\|null | Parent ID (e.g. Team ID) |
| `parent_name` | string\|null | Parent Name |

### Link Channel Response (200 OK)

```json
{
  "status": "SUCCESS",
  "message": "Channel linked successfully",
  "data": {
    "deal_id": "123e4567-e89b-12d3-a456-426614174000",
    "channel_id": "internal-channel-uuid",
    "external_channel_id": "team_1_channel_general",
    "name": "General",
    "type": "public",
    "integration_id": "123e4567-e89b-12d3-a456-426614174000"
  }
}
```

---

## 4. Remove Channel from Deal

### Remove Channel Endpoint

```http
DELETE /deals/{deal_id}/channels/{channel_id}
```

### Remove Channel Description

Unlink a messaging channel from a deal.

### Remove Channel Authentication

Required - Bearer token

### Remove Channel Request

**Path Parameters:**

- `deal_id` (UUID, required): The ID of the deal.
- `channel_id` (UUID, required): The internal ID of the channel to remove (not the external ID).

### Remove Channel Response (200 OK)

```json
{
  "status": "SUCCESS",
  "message": "Channel removed successfully",
  "data": null
}
```
