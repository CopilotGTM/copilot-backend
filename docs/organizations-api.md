### Organizations API

Base path: `/admin/organisations`

All endpoints return the standard wrapper:

```json
{
  "status": "SUCCESS" | "ERROR",
  "message": "string",
  "data": any | null
}
```

Authentication: Requires admin authentication. Role requirements are noted per endpoint.

---

### Create organization

- Method: POST
- Path: `/admin/organisations/create_organisation`
- Roles: Super admin only

Request body:

```json
{
  "organisation_name": "string",
  "country": "string",
  "sales_team_size": 123,
  "company_address": "string",
  "billing_address": "string",
  "linked_urls": ["https://example.com/link1", "https://example.com/link2"],
  "website_urls": ["https://example.com"],
  "summary": "string",
  "logo_url": "https://..."
}
```

Response data:

```json
{
  "organisation_id": "uuid",
  "organisation_name": "string",
  "schema_name": "string",
  "created_at": "2024-01-01T00:00:00Z",
  "updated_at": "2024-01-01T00:00:00Z"
}
```

---

### List organizations (paginated)

- Method: GET
- Path: `/admin/organisations/list_organisations`
- Query params:
  - `limit` (int, 1-100, default 20)
  - `cursor` (string, optional)
- Roles: Super admin or Admin

Response data:

```json
{
  "organizations": [
    {
      "organisation_id": "uuid",
      "organisation_name": "string",
      "is_active": true,
      "country": "string",
      "sales_team_size": 123,
      "company_address": "string",
      "billing_address": "string",
      "linked_urls": ["https://..."],
      "website_urls": ["https://..."],
      "summary": "string",
      "logo_url": "https://...",
      "status": "pending|approved|rejected|suspended|deactivated",
      "created_at": "2024-01-01T00:00:00Z",
      "updated_at": "2024-01-01T00:00:00Z"
    }
  ],
  "cursor": "opaque-string-or-null"
}
```

---

### Get organization

- Method: GET
- Path: `/admin/organisations/{organisation_id}`
- Roles: Super admin or Admin

Path params:
- `organisation_id` (uuid)

Response data:

```json
{
  "organisation_id": "uuid",
  "organisation_name": "string",
  "is_active": true,
  "country": "string",
  "sales_team_size": 123,
  "company_address": "string",
  "billing_address": "string",
  "linked_urls": ["https://..."],
  "website_urls": ["https://..."],
  "summary": "string",
  "logo_url": "https://...",
  "status": "pending|approved|rejected|suspended|deactivated",
  "created_at": "2024-01-01T00:00:00Z",
  "updated_at": "2024-01-01T00:00:00Z"
}
```

Errors:
- 404 if organization not found

---

### Update organization

- Method: PUT
- Path: `/admin/organisations/{organisation_id}/update`
- Roles: Super admin or Admin

Path params:
- `organisation_id` (uuid)

Request body (send only fields to update; all optional):

```json
{
  "organisation_name": "string",
  "country": "string",
  "sales_team_size": 123,
  "company_address": "string",
  "billing_address": "string",
  "linked_urls": ["https://..."],
  "website_urls": ["https://..."],
  "summary": "string",
  "logo_url": "https://...",
  "is_active": true
}
```

Response data:

```json
{
  "organisation_id": "uuid",
  "organisation_name": "string",
  "is_active": true,
  "country": "string",
  "sales_team_size": 123,
  "company_address": "string",
  "billing_address": "string",
  "linked_urls": ["https://..."],
  "website_urls": ["https://..."],
  "summary": "string",
  "logo_url": "https://...",
  "status": "pending|approved|rejected|suspended|deactivated",
  "created_at": "2024-01-01T00:00:00Z",
  "updated_at": "2024-01-01T00:00:00Z"
}
```

Errors:
- 404 if organization not found
- 400 if organization cannot be updated due to status

---

### Delete organization

Note: The delete endpoint is currently commented out in the router. The service supports deletion with the constraint that the organization must be in `suspended` status.

- Method: DELETE
- Path: `/admin/organisations/{organisation_id}/delete`
- Roles: Super admin only

Path params:
- `organisation_id` (uuid)

Response data:

```json
{
  "organisation_id": "uuid",
  "organisation_name": "string",
  "schema_name": "string|null"
}
```

Errors:
- 404 if organization not found
- 400 if organization is not in `suspended` status




