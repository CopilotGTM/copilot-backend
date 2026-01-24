# CRM Users API Documentation

> Version: 1.0
> Base URL: `/users/crm`
> Last Updated: 2026-01-22

---

## Overview

The Users API provides search functionality for app users (organization members) who can be assigned as deal owners or team members.

**Important:** This API searches `public.users` table (app users), not CRM contacts.

---

## 1. Search Users

Search organization members for owner/team selection dropdowns.

### Endpoint

```
GET /users/crm/users
```

### Request Headers

| Header | Required | Value |
|--------|----------|-------|
| Authorization | Yes | Bearer {token} |

### Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `q` | string | - | Search query (name or email) |
| `limit` | integer | 50 | Items per page (1-100) |
| `cursor` | string | - | Pagination cursor for next page |

### Example Request

```
GET /users/crm/users?q=john&limit=20
```

### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Users retrieved successfully",
  "data": {
    "items": [
      {
        "user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
        "name": "John Doe",
        "email": "john.doe@company.com",
        "role": "sales_rep",
        "job_title": "Account Executive",
        "organisation_role": "Sales",
        "status": "active"
      },
      {
        "user_id": "8d0f7780-8536-51ef-b5c0-00d05ec1f2e8",
        "name": "John Smith",
        "email": "john.smith@company.com",
        "role": "manager",
        "job_title": "Sales Manager",
        "organisation_role": "Sales",
        "status": "active"
      }
    ],
    "cursor": null
  }
}
```

### Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `items` | array | List of matching users |
| `items[].user_id` | UUID | User ID (use this for `owner_user_id`) |
| `items[].name` | string | Display name (first + last name) |
| `items[].email` | string | User email address |
| `items[].role` | string | User role in the system |
| `items[].job_title` | string | Job title |
| `items[].organisation_role` | string | Department/team |
| `items[].status` | string | User status (active, inactive) |
| `cursor` | string | Pagination cursor (null if no more pages) |

### Error Responses

| Status | Description |
|--------|-------------|
| 401 | Unauthorized - Invalid or missing token |
| 429 | Rate limited - Too many requests |
| 500 | Internal server error |

---

## Frontend Usage

### React Query Hook Example

```typescript
import { useQuery } from '@tanstack/react-query';
import { crmApiClient } from './crm.client';

interface CRMUserItem {
  user_id: string;
  name: string | null;
  email: string | null;
  role: string | null;
  job_title: string | null;
  organisation_role: string | null;
  status: string | null;
}

interface CRMUsersPage {
  items: CRMUserItem[];
  cursor: string | null;
}

export function useUserSearch(searchQuery: string, options?: { enabled?: boolean; limit?: number }) {
  const { enabled = true, limit = 20 } = options ?? {};

  return useQuery({
    queryKey: ['crm_users', 'search', searchQuery],
    queryFn: async () => {
      const params = new URLSearchParams();
      if (searchQuery) params.set('q', searchQuery);
      params.set('limit', String(limit));

      const { data } = await crmApiClient.get<{ data: CRMUsersPage }>('/users', { params });
      return data.data?.items ?? [];
    },
    enabled: enabled && searchQuery.length >= 1,
    staleTime: 2 * 60 * 1000,  // 2 minutes
    gcTime: 5 * 60 * 1000,     // 5 minutes
  });
}
```

### Dropdown Component Example

```tsx
function OwnerSelect({ value, onChange }) {
  const [search, setSearch] = useState('');
  const [showDropdown, setShowDropdown] = useState(false);

  const { data: users = [], isLoading } = useUserSearch(search, {
    enabled: showDropdown && search.length >= 1
  });

  return (
    <div>
      <input
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        onFocus={() => setShowDropdown(true)}
        placeholder="Search for owner..."
      />
      {showDropdown && (
        <ul>
          {isLoading && <li>Searching...</li>}
          {users.map(user => (
            <li key={user.user_id} onClick={() => onChange(user.user_id)}>
              {user.name || user.email}
              {user.name && <small>{user.email}</small>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
```

---

## TypeScript Types

```typescript
interface SearchUsersParams {
  q?: string;
  limit?: number;
  cursor?: string;
}

interface CRMUserItem {
  user_id: string;
  name: string | null;
  email: string | null;
  role: string | null;
  job_title: string | null;
  organisation_role: string | null;
  status: string | null;
}

interface CRMUsersPage {
  items: CRMUserItem[];
  cursor: string | null;
}

type CRMUsersResponse = ApiEnvelope<CRMUsersPage>;
```

---

## Best Practices

1. **Debounce search**: Wait 300ms after user stops typing before making API call
2. **Minimum characters**: Only search when query is 1+ characters
3. **Cache results**: Use 2-5 minute staleTime for frequently searched names
4. **Display format**: Show `name` if available, fallback to `email`
5. **Clear selection**: Provide a way to clear the selected owner

---

## Related Documents

- `crm-deals-api.md` - Deals CRUD (uses `owner_user_id` from this API)
- `crm-settings-api.md` - Settings/Options API
