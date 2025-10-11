# OAuth Integration Data Flow - Complete Guide

## Overview

This document explains how user and organization data flows through the OAuth integration process, from frontend to Nango to backend webhook.

---

## 🔄 Complete Data Flow

### Step 1: Frontend Initiates OAuth

**Frontend sends** (via JWT Bearer Token):
```
POST /users/integrations/oauth/authorize
Authorization: Bearer <jwt_token>

Body:
{
  "integration_type": "hubspot",
  "integration_name": "My HubSpot Connection"  // Optional
}
```

**Backend extracts from JWT** (automatically via authentication):
- `user_id` - From JWT token
- `organisation_id` - From JWT token  
- `email` - From User model
- `first_name` - From User model
- `last_name` - From User model

**Backend sends TO Nango**:
```json
{
  "end_user": {
    "id": "user_id",                           // From JWT
    "email": "user@example.com",               // From User model
    "display_name": "John Doe",                // From User model
    "tags": {
      "organizationId": "org_id"               // From JWT
    }
  },
  "allowed_integrations": ["hubspot"]
}
```

**Backend returns to Frontend**:
```json
{
  "status": "success",
  "data": {
    "session_token": "ngc_sess_abc123...",
    "integration_type": "hubspot",
    "expires_at": "2025-10-10T12:30:00Z"
  }
}
```

---

### Step 2: Frontend Opens Nango Connect UI

**Frontend code**:
```typescript
import Nango from '@nangohq/frontend';

const nango = new Nango({ publicKey: 'YOUR_PUBLIC_KEY' });

nango.auth(sessionToken)
  .then((result) => {
    console.log('✅ Authorization successful');
    // Refresh integrations list
  })
  .catch((error) => {
    console.error('❌ Authorization failed:', error);
  });
```

**What Nango does**:
1. Opens OAuth modal with provider (HubSpot, Slack, etc.)
2. User authorizes the application
3. Nango receives OAuth tokens from provider
4. Nango stores tokens securely
5. **Nango sends webhook to your backend**

---

### Step 3: Nango Sends Webhook to Backend

**Nango sends** (without any authentication):
```
POST /users/integrations/oauth/webhooks/nango
X-Nango-Signature: <sha256_hash>
Content-Type: application/json

Body:
{
  "type": "auth",
  "operation": "creation",
  "connectionId": "abc-123-def",
  "authMode": "OAUTH2",
  "providerConfigKey": "hubspot",
  "provider": "hubspot",
  "environment": "DEV",
  "success": true,
  "endUser": {
    "id": "user_id",                    // ← Same as we sent in Step 1
    "email": "user@example.com",        // ← Same as we sent in Step 1
    "display_name": "John Doe",         // ← Same as we sent in Step 1
    "tags": {
      "organizationId": "org_id"        // ← Same as we sent in Step 1
    }
  }
}
```

**Backend receives and extracts**:
```python
# Extract from webhook payload
user_id = payload["endUser"]["id"]
organization_id = payload["endUser"]["tags"]["organizationId"]
connection_id = payload["connectionId"]
provider = payload["providerConfigKey"]

# Backend creates database session using organization_id
# Backend creates integration record in database
```

**Backend returns to Nango**:
```json
{
  "status": "success",
  "message": "Webhook processed successfully",
  "data": {
    "integration_id": "uuid-of-integration",
    "integration_type": "hubspot",
    "connection_id": "abc-123-def"
  }
}
```

---

## 🎯 Key Points

### 1. Frontend Does NOT Send Organization ID

❌ **Frontend does NOT explicitly send**:
- `user_id`
- `organisation_id`
- User email or name

✅ **Backend automatically extracts from**:
- **JWT Token** → `user_id`, `organisation_id`
- **User Model** → `email`, `first_name`, `last_name`

### 2. Backend Sends Data TO Nango

✅ **Backend sends to Nango** (in Connect Session):
```json
{
  "end_user": {
    "id": "user_id",
    "email": "user@example.com",
    "display_name": "John Doe",
    "tags": {
      "organizationId": "org_id"
    }
  }
}
```

### 3. Nango Sends Data BACK to Backend

✅ **Nango returns in webhook** (exact same data):
```json
{
  "endUser": {
    "id": "user_id",           // ← We get back what we sent
    "tags": {
      "organizationId": "org_id"  // ← We get back what we sent
    }
  }
}
```

### 4. Why This Design?

**Round-trip verification**:
1. Backend **sends** `user_id` and `organization_id` to Nango
2. Nango **stores** this in the session
3. User completes OAuth
4. Nango **returns** the same `user_id` and `organization_id` in webhook
5. Backend **verifies** and creates integration for the correct user/org

**Security**:
- Webhook has no JWT authentication (Nango can't get our JWT)
- Instead uses **signature verification** (`X-Nango-Signature`)
- Backend trusts `user_id` and `organization_id` because:
  - We originally sent them (not user input)
  - Webhook signature is verified
  - Data hasn't been tampered with

---

## 📊 Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                     STEP 1: Frontend → Backend                   │
└─────────────────────────────────────────────────────────────────┘

Frontend                          Backend
   │                                 │
   │  POST /oauth/authorize         │
   │  Authorization: Bearer <JWT>   │
   │  Body: { integration_type }    │
   │ ──────────────────────────────>│
   │                                 │  ┌─ Extract from JWT ────┐
   │                                 │  │ - user_id             │
   │                                 │  │ - organisation_id     │
   │                                 │  └───────────────────────┘
   │                                 │
   │                                 │  ┌─ Extract from User ───┐
   │                                 │  │ - email               │
   │                                 │  │ - first_name          │
   │                                 │  │ - last_name           │
   │                                 │  └───────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                     STEP 2: Backend → Nango                      │
└─────────────────────────────────────────────────────────────────┘

Backend                           Nango
   │                                 │
   │  POST /connect/sessions         │
   │  {                              │
   │    end_user: {                  │
   │      id: "user_id",             │  ← Backend sends
   │      email: "user@example.com", │
   │      display_name: "John Doe",  │
   │      tags: {                    │
   │        organizationId: "org_id" │  ← Backend sends
   │      }                           │
   │    },                            │
   │    allowed_integrations: [...]  │
   │  }                               │
   │ ──────────────────────────────>│
   │                                 │
   │  { session_token, expires_at }  │  ← Nango returns session
   │ <──────────────────────────────│
   │                                 │
   │                                 │  ┌─ Nango stores ────────┐
   │                                 │  │ - user_id             │
   │                                 │  │ - organization_id     │
   │                                 │  │ - session_token       │
   │                                 │  └───────────────────────┘

Backend                          Frontend
   │                                 │
   │  { session_token }              │
   │ <──────────────────────────────│

┌─────────────────────────────────────────────────────────────────┐
│                  STEP 3: Frontend → Nango (OAuth)                │
└─────────────────────────────────────────────────────────────────┘

Frontend                          Nango                   Provider
   │                                 │                        │
   │  nango.auth(session_token)      │                        │
   │ ──────────────────────────────>│                        │
   │                                 │                        │
   │         OAuth Modal Opens       │                        │
   │ <──────────────────────────────│                        │
   │                                 │                        │
   │    User clicks "Authorize"      │                        │
   │ ──────────────────────────────>│                        │
   │                                 │  Redirect to Provider  │
   │                                 │ ─────────────────────>│
   │                                 │                        │
   │                                 │  User authorizes app   │
   │                                 │                        │
   │                                 │  OAuth callback        │
   │                                 │ <─────────────────────│
   │                                 │  (with auth code)      │
   │                                 │                        │
   │                                 │  Exchange code for     │
   │                                 │  access tokens         │
   │                                 │ ─────────────────────>│
   │                                 │                        │
   │                                 │  Access token          │
   │                                 │ <─────────────────────│
   │                                 │                        │
   │                                 │  ┌─ Nango stores ────┐
   │                                 │  │ - access_token    │
   │                                 │  │ - refresh_token   │
   │                                 │  │ - expires_at      │
   │                                 │  └───────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                     STEP 4: Nango → Backend                      │
└─────────────────────────────────────────────────────────────────┘

Nango                            Backend
   │                                 │
   │  POST /webhooks/nango           │
   │  X-Nango-Signature: <hash>      │
   │  {                              │
   │    type: "auth",                │
   │    operation: "creation",       │
   │    connectionId: "abc-123",     │
   │    success: true,               │
   │    endUser: {                   │
   │      id: "user_id",             │  ← Nango returns what we sent
   │      tags: {                    │
   │        organizationId: "org_id" │  ← Nango returns what we sent
   │      }                           │
   │    }                             │
   │  }                               │
   │ ──────────────────────────────>│
   │                                 │
   │                                 │  ┌─ Backend verifies ────┐
   │                                 │  │ - Signature valid?    │
   │                                 │  │ - Extract user_id     │
   │                                 │  │ - Extract org_id      │
   │                                 │  └───────────────────────┘
   │                                 │
   │                                 │  ┌─ Backend creates ─────┐
   │                                 │  │ - Integration record  │
   │                                 │  │ - In correct org DB   │
   │                                 │  │ - For correct user    │
   │                                 │  └───────────────────────┘
   │                                 │
   │  { status: "success" }          │
   │ <──────────────────────────────│
   │                                 │

┌─────────────────────────────────────────────────────────────────┐
│                  STEP 5: Frontend Polls/Refreshes                │
└─────────────────────────────────────────────────────────────────┘

Frontend                          Backend
   │                                 │
   │  GET /integrations/available    │
   │  Authorization: Bearer <JWT>    │
   │ ──────────────────────────────>│
   │                                 │
   │  {                              │
   │    integrations: [              │
   │      {                           │
   │        id: "uuid",              │
   │        type: "hubspot",         │
   │        name: "My HubSpot",      │
   │        status: "active",        │
   │        created_at: "..."        │
   │      }                           │
   │    ]                             │
   │  }                               │
   │ <──────────────────────────────│
   │                                 │
   │  ✅ Show "Connected" status     │
```

---

## 🔐 Security Considerations

### 1. No JWT in Webhook

**Why?**
- Webhooks come from Nango's servers (not from user's browser)
- Nango doesn't have access to user's JWT tokens
- Can't use traditional JWT authentication

**Solution:**
- Use **signature verification** (`X-Nango-Signature`)
- Signature proves webhook comes from Nango
- Formula: `SHA256(NANGO_SECRET_KEY + JSON_payload)`

### 2. Trust the Webhook Data

**Why can we trust `user_id` and `organization_id` in webhook?**

✅ **Safe because**:
1. **We sent it originally**: Backend extracted from authenticated JWT
2. **Signature verified**: Webhook is proven to come from Nango
3. **No user tampering**: User never sees or controls this data
4. **Round-trip validation**: We get back exactly what we sent

❌ **Not safe if**:
- Webhook signature verification is disabled
- User could modify the session creation request
- Organization ID came from frontend (not JWT)

### 3. Organization Database Isolation

```python
# Backend webhook handler
async for session in acquire_org_async_session(UUID(organization_id)):
    # This session is scoped to the correct organization
    # Integration is created in the right organization's database
    integration = await integration_service.create_integration(...)
```

**Security**:
- Each organization has isolated database access
- Organization ID from webhook ensures correct database
- No cross-organization data leakage

---

## 📝 Implementation Checklist

### Backend Requirements

- [x] **Extract user/org from JWT** in `/oauth/authorize` endpoint
- [x] **Send to Nango** in `create_connect_session()`
  ```python
  end_user = {
      "id": str(user_ids.user_id),
      "tags": {"organizationId": str(user_ids.organisation_id)},
      "email": user.email,
      "display_name": f"{user.first_name} {user.last_name}"
  }
  ```
- [x] **Receive from Nango** in webhook handler
  ```python
  user_id = payload["endUser"]["id"]
  organization_id = payload["endUser"]["tags"]["organizationId"]
  ```
- [x] **Verify webhook signature** before processing
- [x] **Create integration** in correct organization database

### Frontend Requirements

- [ ] **Send JWT token** in Authorization header
  ```typescript
  fetch('/users/integrations/oauth/authorize', {
    headers: {
      'Authorization': `Bearer ${token}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({ integration_type: 'hubspot' })
  })
  ```
- [ ] **Use session token** with Nango Connect UI
  ```typescript
  const nango = new Nango({ publicKey: 'YOUR_PUBLIC_KEY' });
  nango.auth(sessionToken);
  ```
- [ ] **Poll or refresh** integrations list after OAuth completion
- [ ] **Do NOT send** `user_id` or `organization_id` explicitly

### Nango Configuration

- [x] Webhook URL configured in Nango Dashboard
- [x] `NANGO_SECRET_KEY` configured in backend `.env`
- [x] Webhook signature verification enabled

---

## 🧪 Testing & Debugging

### Log What We Send to Nango

```python
# In create_connect_session()
logger.info(
    f"Creating session: user_id={user_id}, org_id={organization_id}, "
    f"integration={integration_id}, email={end_user_email}"
)
```

### Log What We Receive from Nango

```python
# In webhook handler
logger.info(
    f"Received webhook: user_id={user_id}, org_id={organization_id}, "
    f"connection_id={connection_id}, provider={provider}"
)
```

### Verify Round-Trip

```python
# After webhook processing
assert payload["endUser"]["id"] == original_user_id
assert payload["endUser"]["tags"]["organizationId"] == original_org_id
```

### Common Issues

| Issue | Cause | Solution |
|-------|-------|----------|
| Webhook receives wrong `user_id` | Backend sent wrong ID to Nango | Check JWT extraction in `/oauth/authorize` |
| Webhook receives `null` organization ID | Missing `tags` in session creation | Ensure `tags: { organizationId }` is sent |
| Integration created for wrong org | Using wrong org ID from webhook | Extract from `endUser.tags.organizationId` |
| Webhook fails signature check | Wrong secret key | Use `NANGO_SECRET_KEY`, not a separate secret |

---

## 📖 API Reference

### POST /users/integrations/oauth/authorize

**Frontend Request**:
```http
POST /users/integrations/oauth/authorize
Authorization: Bearer <jwt_token>
Content-Type: application/json

{
  "integration_type": "hubspot",
  "integration_name": "My HubSpot"  // Optional
}
```

**What Backend Extracts Automatically**:
- From JWT: `user_id`, `organisation_id`
- From User model: `email`, `first_name`, `last_name`

**Backend Response**:
```json
{
  "status": "success",
  "message": "Connect session created successfully",
  "data": {
    "session_token": "ngc_sess_abc123...",
    "integration_type": "hubspot",
    "expires_at": "2025-10-10T12:30:00Z"
  }
}
```

### POST /users/integrations/oauth/webhooks/nango

**Nango Request**:
```http
POST /users/integrations/oauth/webhooks/nango
X-Nango-Signature: abc123def456...
Content-Type: application/json

{
  "type": "auth",
  "operation": "creation",
  "connectionId": "abc-123-def",
  "authMode": "OAUTH2",
  "providerConfigKey": "hubspot",
  "provider": "hubspot",
  "environment": "DEV",
  "success": true,
  "endUser": {
    "id": "user_id",
    "email": "user@example.com",
    "display_name": "John Doe",
    "tags": {
      "organizationId": "org_id"
    }
  }
}
```

**What Backend Extracts**:
```python
user_id = payload["endUser"]["id"]
organization_id = payload["endUser"]["tags"]["organizationId"]
connection_id = payload["connectionId"]
provider = payload["providerConfigKey"]
```

**Backend Response**:
```json
{
  "status": "success",
  "message": "Webhook processed successfully",
  "data": {
    "integration_id": "uuid",
    "integration_type": "hubspot",
    "connection_id": "abc-123-def"
  }
}
```

---

## 💡 Summary

### Data Flow in One Sentence

**Backend extracts `user_id` and `organization_id` from JWT → sends to Nango → Nango returns the same data in webhook → backend creates integration for that user/org.**

### Key Takeaways

1. ✅ **Frontend ONLY sends**: `integration_type` + JWT token
2. ✅ **Backend extracts from JWT**: `user_id`, `organisation_id`
3. ✅ **Backend sends to Nango**: All user/org information
4. ✅ **Nango returns in webhook**: Same user/org information
5. ✅ **Backend verifies and creates**: Integration in correct org database

### No Additional Frontend Work Required

Frontend developers **do not need** to:
- ❌ Send `user_id` explicitly
- ❌ Send `organization_id` explicitly
- ❌ Extract these from JWT themselves
- ❌ Handle webhook responses

Frontend developers **only need** to:
- ✅ Send JWT token in Authorization header
- ✅ Send `integration_type` in request body
- ✅ Use returned `session_token` with Nango Connect UI
- ✅ Refresh integrations list after OAuth completion

---

**Last Updated**: October 10, 2025  
**Status**: ✅ Complete and Verified
