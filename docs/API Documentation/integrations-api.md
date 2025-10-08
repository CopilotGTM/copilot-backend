# Integrations API Documentation

## Base URL
```
/users/integrations
```

---

## 1. List Available Integrations

### Endpoint
```http
GET /users/integrations/available
```

### Description
Returns all available integration types with their connection status. Shows which integrations the user has already connected.

### Authentication
Required - Bearer token

### Request
No request body or query parameters required.

### Response (200 OK)
```json
{
  "status": "success",
  "message": "Available integrations retrieved successfully",
  "data": {
    "integrations": [
      {
        "integration_type": "hubspot",
        "name": "HubSpot",
        "description": "Sync contacts, deals, and sales activities from HubSpot CRM",
        "logo_url": "https://www.hubspot.com/hubfs/HubSpot_Logos/HubSpot-Inversed-Favicon.png",
        "category": "crm",
        "category_name": "CRM & Sales",
        "availability_status": "available",
        "is_connected": true,
        "connected_integrations": [
          {
            "integration_id": "123e4567-e89b-12d3-a456-426614174000",
            "organisation_id": "org-uuid",
            "integration_type": "hubspot",
            "name": "My HubSpot Account",
            "sync_frequency": "daily",
            "auto_sync_enabled": true,
            "is_active": true,
            "last_synced_at": "2025-10-05T10:30:00Z",
            "last_sync_status": "success",
            "last_sync_error": null,
            "access_token_expires_at": "2025-11-05T10:30:00Z",
            "connection_metadata": {},
            "created_at": "2025-10-01T08:00:00Z",
            "updated_at": "2025-10-05T10:30:00Z"
          }
        ]
      },
      {
        "integration_type": "slack",
        "name": "Slack",
        "description": "Sync messages, files, and channel data from Slack workspaces",
        "logo_url": "https://a.slack-edge.com/80588/marketing/img/icons/icon_slack_hash_colored.png",
        "category": "collaboration",
        "category_name": "Collaboration & Communication",
        "availability_status": "available",
        "is_connected": false,
        "connected_integrations": []
      },
      {
        "integration_type": "google_drive",
        "name": "Google Drive",
        "description": "Sync documents, spreadsheets, and files from Google Drive",
        "logo_url": "https://ssl.gstatic.com/images/branding/product/2x/drive_2020q4_48dp.png",
        "category": "storage",
        "category_name": "Cloud Storage",
        "availability_status": "coming_soon",
        "is_connected": false,
        "connected_integrations": []
      }
    ],
    "total_available": 7,
    "total_connected": 1
  }
}
```

### Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `integration_type` | string | Unique identifier for the integration type |
| `name` | string | Display name of the integration |
| `description` | string | Brief description of what the integration does |
| `logo_url` | string | URL to integration provider's logo |
| `category` | string | Category slug (crm, collaboration, storage) |
| `category_name` | string | Human-readable category name |
| `availability_status` | string | `available`, `coming_soon`, `beta`, `deprecated` |
| `is_connected` | boolean | Whether user has connected this integration |
| `connected_integrations` | array | List of user's connected instances |
| `total_available` | integer | Total number of integration types |
| `total_connected` | integer | Total connected integrations |

### Availability Status Values

| Status | Description | Action |
|--------|-------------|--------|
| `available` | Integration is ready to use | Show "Connect" button |
| `coming_soon` | Integration not yet available | Show "Coming Soon" badge, disable button |
| `beta` | Integration in beta testing | Show "Beta" badge, allow connection |
| `deprecated` | Integration being phased out | Show warning message |

### Error Response (500)
```json
{
  "status": "error",
  "message": "Failed to retrieve available integrations",
  "data": null
}
```

### Implementation Notes

**Frontend Display:**
1. **Group by category** - Use `category_name` to group integrations
2. **Connection status** - Use `is_connected` to show badges or different UI states
3. **Connected instances** - Display list of connected accounts if `is_connected` is true
4. **Availability** - Disable connect button if `availability_status` is `coming_soon`

**Example UI Logic:**
```typescript
// Check if integration can be connected
const canConnect = integration.availability_status === 'available' || 
                   integration.availability_status === 'beta';

// Show connection badge
const showConnectedBadge = integration.is_connected;

// Show "Add Another" button
const showAddAnother = integration.is_connected && canConnect;

// Display coming soon badge
const showComingSoon = integration.availability_status === 'coming_soon';
```

**Performance:**
- Single API call gets all data (available types + connections)
- No need for separate requests
- Cache response for 5 minutes

**Error Handling:**
```typescript
try {
  const response = await fetch('/users/integrations/available', {
    headers: {
      'Authorization': `Bearer ${token}`
    }
  });
  
  if (!response.ok) {
    throw new Error('Failed to fetch integrations');
  }
  
  const data = await response.json();
  // Handle data.data.integrations
} catch (error) {
  // Show error message to user
  console.error('Integration fetch error:', error);
}
```

### Example cURL
```bash
curl -X GET "https://api.example.com/users/integrations/available" \
  -H "Authorization: Bearer YOUR_TOKEN_HERE" \
  -H "Content-Type: application/json"
```

---

## 2. Initiate OAuth Connection (Authorize)

### Endpoint
```http
POST /users/integrations/oauth/authorize
```

### Description
Initiates OAuth flow for connecting an integration. Returns an authorization URL that the user must visit to grant permissions. This is the **second step** after user clicks "Connect" on an integration from the available list.

### Authentication
Required - Bearer token

### Request Body
```json
{
  "integration_type": "hubspot",
  "integration_name": "Sales HubSpot Account"
}
```

### Request Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `integration_type` | string | Yes | Integration type (hubspot, slack, etc.) from available list |
| `integration_name` | string | No | Custom name for this connection (auto-generated if not provided) |

**Note:** OAuth scopes are automatically configured by the backend based on integration requirements. Frontend doesn't need to specify scopes.

### Response (200 OK)
```json
{
  "status": "success",
  "message": "OAuth authorization URL generated successfully",
  "data": {
    "authorization_url": "https://app.hubspot.com/oauth/authorize?client_id=xxx&redirect_uri=xxx&state=xxx",
    "connection_id": "org_123e4567_hubspot_20251005103000",
    "state": "random-state-string",
    "integration_type": "hubspot"
  }
}
```

### Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `authorization_url` | string | URL to redirect user to for OAuth authorization |
| `connection_id` | string | Unique connection identifier (format: `org_{org_id}_{type}_{timestamp}`) |
| `state` | string | OAuth state parameter for security validation |
| `integration_type` | string | Integration type being connected |

### Error Response (500)
```json
{
  "status": "error",
  "message": "Failed to initiate OAuth flow",
  "data": null
}
```

### Implementation Notes

**Frontend Flow:**
1. User clicks "Connect" button on an integration
2. Call this endpoint with integration type
3. Open returned `authorization_url` in popup or redirect
4. User authorizes on provider's site
5. Provider redirects to callback (handled by backend)
6. Listen for redirect back to success page
7. Refresh integrations list to show new connection

**OAuth Popup Pattern:**
```typescript
async function connectIntegration(integrationType: string) {
  try {
    // Step 1: Get authorization URL
    const response = await fetch('/users/integrations/oauth/authorize', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        integration_type: integrationType,
        integration_name: `My ${integrationType} Account`
      })
    });
    
    const data = await response.json();
    const authUrl = data.data.authorization_url;
    
    // Step 2: Open OAuth popup
    const popup = window.open(
      authUrl,
      'OAuth',
      'width=600,height=700,scrollbars=yes'
    );
    
    // Step 3: Listen for completion
    const checkPopup = setInterval(() => {
      if (popup?.closed) {
        clearInterval(checkPopup);
        // Refresh integrations list
        fetchIntegrations();
      }
    }, 500);
    
  } catch (error) {
    console.error('OAuth initiation failed:', error);
    showError('Failed to connect integration');
  }
}
```

**Alternative: Full Page Redirect:**
```typescript
async function connectIntegration(integrationType: string) {
  const response = await fetch('/users/integrations/oauth/authorize', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({ integration_type: integrationType })
  });
  
  const data = await response.json();
  
  // Redirect current page to authorization URL
  // Backend will redirect back after completion
  window.location.href = data.data.authorization_url;
}
```

### Example cURL
```bash
curl -X POST "https://api.example.com/users/integrations/oauth/authorize" \
  -H "Authorization: Bearer YOUR_TOKEN_HERE" \
  -H "Content-Type: application/json" \
  -d '{
    "integration_type": "hubspot",
    "integration_name": "Sales HubSpot"
  }'
```

### OAuth Scopes (Backend Managed)

OAuth scopes are **automatically configured by the backend** based on each integration's requirements. The frontend doesn't need to know or specify scopes.

**Why backend manages scopes:**
- ✅ **Security** - Backend knows exactly what permissions it needs
- ✅ **Consistency** - All users get same, tested permissions
- ✅ **Simplicity** - Frontend just says "connect HubSpot"
- ✅ **Maintenance** - Add new scopes without frontend changes

**Current scope configuration:**

**HubSpot:** (Backend requests)
- `crm.objects.contacts.read` - Read contacts
- `crm.objects.deals.read` - Read deals
- `crm.objects.companies.read` - Read companies
- `files` - Access files

**Slack:** (Backend requests)
- `channels:read` - View channels
- `channels:history` - Read messages
- `files:read` - Access files
- `users:read` - View users

---

## 3. OAuth Callback (Automatic - Provider Calls This)

### Endpoint
```http
GET /users/integrations/oauth/callback
```

### Description
**⚠️ This endpoint is NOT called by the frontend.** It's called automatically by the OAuth provider (HubSpot, Slack, etc.) after the user authorizes your application. This is part of the standard OAuth 2.0 flow.

### How It Works

```
1. User authorizes on HubSpot → HubSpot redirects to this callback
2. Callback receives authorization code
3. Backend exchanges code for access tokens (via Nango)
4. Backend saves integration to database
5. Backend redirects user back to your frontend
```

### Authentication
OAuth provider validates this request using registered callback URL and state parameter.

### Query Parameters (Sent by OAuth Provider)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `code` | string | Yes | Authorization code from OAuth provider |
| `state` | string | No | State parameter for CSRF protection |
| `connection_id` | string | Yes | Connection identifier (format: `org_{org_id}_{type}_{timestamp}`) |

### Example Request (Called by HubSpot)
```
GET /users/integrations/oauth/callback
  ?code=abc123xyz789
  &state=random-security-token
  &connection_id=org_12345_hubspot_20251005103000
```

### Response
Redirects user to frontend with status:

**Success Redirect:**
```
https://yourfrontend.com/settings/integrations
  ?status=success
  &integration_id=123e4567-e89b-12d3-a456-426614174000
```

**Error Redirect:**
```
https://yourfrontend.com/settings/integrations
  ?status=error
  &message=Error+description
```

### Frontend Implementation

**Listen for OAuth completion:**
```typescript
// In your integrations settings page
useEffect(() => {
  const params = new URLSearchParams(window.location.search);
  const status = params.get('status');
  const integrationId = params.get('integration_id');
  const errorMessage = params.get('message');
  
  if (status === 'success') {
    // Show success message
    showNotification('Integration connected successfully!');
    
    // Refresh integrations list
    fetchIntegrations();
    
    // Clean URL
    window.history.replaceState({}, '', '/settings/integrations');
  } else if (status === 'error') {
    // Show error message
    showError(`Failed to connect: ${errorMessage}`);
    
    // Clean URL
    window.history.replaceState({}, '', '/settings/integrations');
  }
}, []);
```

### What Happens Behind the Scenes

1. **Extract Integration Type**
   - Parses `connection_id` to identify which integration (HubSpot, Slack, etc.)

2. **Exchange Code for Tokens**
   - Backend calls Nango to exchange authorization code for access token
   - Nango stores encrypted tokens securely

3. **Save Integration**
   - Creates integration record in database with:
     - `integration_type`: Type of integration
     - `name`: Auto-generated name (user can change later)
     - `connection_id`: Unique identifier for this connection
     - `connection_metadata`: Provider-specific metadata
     - `scopes`: Granted OAuth scopes

4. **Redirect User**
   - Sends user back to frontend with success/error status
   - Frontend shows notification and refreshes integration list

### Configuration Required

**Register Callback URL with OAuth Provider:**

You must register this callback URL with each OAuth provider (HubSpot, Slack, etc.):

```
Production: https://api.yourapp.com/users/integrations/oauth/callback
Development: http://localhost:8000/users/integrations/oauth/callback
```

**Environment Variable:**
```bash
OAUTH_CALLBACK_BASE_URL=https://yourfrontend.com
```

This tells the backend where to redirect users after OAuth completion.

### Security Notes

- ✅ **HTTPS Required** - OAuth providers reject non-HTTPS callbacks in production
- ✅ **State Validation** - State parameter prevents CSRF attacks
- ✅ **Connection ID** - Unique per-user to prevent hijacking
- ✅ **Registered URL** - Only pre-registered callbacks are accepted by providers

### Error Scenarios

| Error | Cause | User Sees |
|-------|-------|-----------|
| Invalid `connection_id` | Malformed or missing | Redirected with error message |
| Code exchange failed | Invalid/expired code | Redirected with error message |
| Database error | Failed to save integration | Redirected with error message |
| Token expired | User took too long | Redirected with timeout message |

### Testing

**You cannot test this endpoint directly.** It must be called by the OAuth provider. To test:

1. Call `POST /users/integrations/oauth/authorize` to get authorization URL
2. Visit the authorization URL in browser
3. Complete OAuth flow on provider's site
4. Provider automatically calls callback endpoint
5. You're redirected back to frontend

---

## Common Error Codes

| Status Code | Message | Solution |
|-------------|---------|----------|
| 401 | Unauthorized | Check bearer token is valid |
| 500 | Internal Server Error | Retry request or contact support |

---

## Best Practices

1. **Cache the response** - Integration list changes rarely, cache for 5 minutes
2. **Handle empty states** - Check if `connected_integrations` array is empty
3. **Show loading states** - API call may take 1-2 seconds
4. **Group by category** - Better UX when integrations are grouped
5. **Check availability** - Don't show connect button for `coming_soon` integrations

---

## TypeScript Interface

```typescript
interface Integration {
  integration_type: string;
  name: string;
  description: string;
  logo_url: string;
  category: string;
  category_name: string;
  availability_status: 'available' | 'coming_soon' | 'beta' | 'deprecated';
  is_connected: boolean;
  connected_integrations: ConnectedIntegration[];
}

interface ConnectedIntegration {
  integration_id: string;
  organisation_id: string;
  integration_type: string;
  name: string;
  sync_frequency: string;
  auto_sync_enabled: boolean;
  is_active: boolean;
  last_synced_at: string | null;
  last_sync_status: string | null;
  last_sync_error: string | null;
  access_token_expires_at: string | null;
  connection_metadata: Record<string, any>;
  created_at: string;
  updated_at: string;
}

interface AvailableIntegrationsResponse {
  integrations: Integration[];
  total_available: number;
  total_connected: number;
}
```
