# Deal Integrations API Documentation

## Base URL

```
/users/integrations
```

---

## 1. List Available Integrations for Deal

### Endpoint

```http
GET /users/integrations/deals/{deal_id}/available
```

### Description

Returns available integrations specifically for deal context (Slack and Teams).
Shows which integrations are connected to the specified deal.
**Note:** This endpoint filters integrations based on the current user's access. It returns:

- Organization-wide integrations (e.g., Slack)
- User-specific integrations (e.g., Teams) belonging to the current user.

### Authentication

Required - Bearer token

### Request

**Path Parameters:**

- `deal_id` (UUID, required): Context deal ID.

### Response (200 OK)

```json
{
  "status": "success",
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
            "created_by_admin": true,
            "integration_email": null,
            "integration_scope": "organization",
            "is_active": true,
            "created_at": "2025-10-01T08:00:00Z",
            "updated_at": "2025-10-05T10:30:00Z"
          }
        ],
        "linked_channels": [
            {
                "channel_id": "uuid-of-channel",
                "external_channel_id": "C12345",
                "name": "general",
                "type": "public",
                "integration_id": "123e4567-e89b-12d3-a456-426614174000",
                "integration_name": "Acme Corp Slack",
                "integration_type": "slack",
                "parent_name": null
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

## 2. List Channels for Integration

### Endpoint

```http
GET /users/integrations/deals/{integration_id}/channels?deal_id={deal_id}
```

### Description

Fetch available channels/teams for a specific integration (e.g., Teams, Slack).
This is used to populate the channel selection dropdown when linking a channel to a deal.
If `deal_id` is provided, channels already linked to that deal will be excluded from the response.

### Authentication

Required - Bearer token

### Request

**Path Parameters:**

- `integration_id` (UUID, required): The ID of the integration to fetch channels from.

**Query Parameters:**

- `deal_id` (UUID, optional): ID of the deal to filter out already linked channels.

### Response (200 OK)

```json
{
  "status": "success",
  "message": "Channels retrieved successfully",
  "data": {
    "channels": [
      {
        "id": "team_1_channel_general",
        "name": "General",
        "type": "standard",
        "parent_id": "team_1",
        "parent_name": "Sales Team"
      }
    ],
    "total": 1
  }
}
```

---

## 3. Link Channel to Deal

### Endpoint

```http
POST /users/integrations/deals/{deal_id}/channels
```

### Description

Links a specific messaging channel (Slack/Teams) to a deal.
This enables message syncing for that channel in the context of the deal.

### Authentication

Required - Bearer token

### Request

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
|-------|------|-------------|
| `id` | string | Channel ID (from List Channels API) |
| `name` | string | Channel Name |
| `type` | string | Channel Type |
| `integration_id` | UUID | Integration ID this channel belongs to |
| `parent_id` | string\|null | Parent ID (e.g. Team ID) |
| `parent_name` | string\|null | Parent Name |

### Response (200 OK)

```json
{
  "status": "success",
  "message": "Channel linked successfully",
  "data": {
    "deal_id": "123e4567-e89b-12d3-a456-426614174000",
    "channel_id": "internal-channel-uuid",
    "external_channel_id": "team_1_channel_general",
    "name": "General"
  }
}
```

---

## 4. Remove Channel from Deal

### Endpoint

```http
DELETE /users/integrations/deals/{deal_id}/channels/{channel_id}
```

### Description

Unlink a messaging channel from a deal.

### Authentication

Required - Bearer token

### Request

**Path Parameters:**

- `deal_id` (UUID, required): The ID of the deal.
- `channel_id` (UUID, required): The internal ID of the channel to remove (not the external ID).

### Response (200 OK)

```json
{
  "status": "success",
  "message": "Channel removed successfully",
  "data": null
}
```
