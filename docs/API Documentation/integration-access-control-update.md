# Integration Access Control Update

## Overview
This document describes the new access control fields added to the integrations system to properly scope integration visibility and track integration ownership.

## Problem Statement
Previously, the `knowledge_integrations` table only had `organisation_id`, meaning:
- ❌ All users in an organization could see ALL integrations
- ❌ No way to distinguish between personal (Google Drive) vs org-wide (HubSpot) integrations
- ❌ No tracking of who created each integration
- ❌ No visibility into which external account was connected

## Solution: Access Control Fields

### New Database Fields

| Field | Type | Nullable | Index | Description |
|-------|------|----------|-------|-------------|
| `created_by_user_id` | UUID | No | Yes | User who created/connected this integration |
| `created_by_admin` | Boolean | No | Yes | Whether created by an admin user |
| `integration_email` | String(255) | Yes | Yes | Email/username of connected external account |
| `integration_scope` | String(20) | No | Yes | Access scope: organization, user, team, role_based |

### Migration Details

**File:** `3c69a1ff9b62_004_add_integration_access_control.py`

**Upgrade Actions:**
1. Add `created_by_user_id` column (UUID, NOT NULL with temp default)
2. Add `created_by_admin` column (Boolean, default false)
3. Add `integration_email` column (String 255, nullable)
4. Add `integration_scope` column (String 20, default 'organization')
5. Create indexes on all new fields
6. Create composite index on `(organisation_id, integration_scope, created_by_user_id)`

**Downgrade Actions:**
- Drops all indexes and columns in reverse order

## Access Scope Types

### 1. Organization Scope (`organization`)
**Use case:** Company-wide integrations like HubSpot, Salesforce, Slack
- ✅ All users in organization can see the data
- ✅ Usually one connection per organization
- ✅ Typically created by admins
- **Examples:** HubSpot CRM, Salesforce, Company Slack workspace

### 2. User Scope (`user`)
**Use case:** Personal integrations like Google Drive, personal Dropbox
- ✅ Only the creator can see the data
- ✅ Each user has their own connection
- ✅ Can be created by any member
- **Examples:** Personal Google Drive, individual Dropbox

### 3. Team Scope (`team`) - Future
**Use case:** Shared by specific team members
- 🔜 Coming in future release

### 4. Role-Based Scope (`role_based`) - Future
**Use case:** Access determined by user role
- 🔜 Coming in future release

## Integration Email Field

### Purpose
Stores the **external account email/username** that was connected during OAuth, NOT the internal user's email.

### Examples by Provider

| Provider | Example `integration_email` | Where It's Extracted From |
|----------|---------------------------|---------------------------|
| Google Drive | `john@company.com` | OAuth `email` field |
| HubSpot | `sales@acme.com` | HubSpot account email |
| Slack | `workspace@company.slack.com` | Slack workspace email |
| Salesforce | `admin@acme.com` | Salesforce username |
| Microsoft 365 | `user@company.com` | Microsoft account email |

### Use Cases
1. **UI Display**: "Connected as: sales@acme.com"
2. **Debugging**: Identify which account has auth issues
3. **Multiple Accounts**: Distinguish between 2+ Google Drive connections
4. **Audit Trail**: Track which external accounts are connected

## API Schema Changes

### IntegrationResponse Schema

**Before:**
```json
{
  "integration_id": "uuid",
  "organisation_id": "uuid",
  "integration_type": "hubspot",
  "name": "My Integration",
  "is_active": true,
  ...
}
```

**After:**
```json
{
  "integration_id": "uuid",
  "organisation_id": "uuid",
  "integration_type": "hubspot",
  "name": "My Integration",
  
  // NEW ACCESS CONTROL FIELDS
  "created_by_user_id": "user-uuid",
  "created_by_admin": true,
  "integration_email": "sales@acme.com",
  "integration_scope": "organization",
  
  "is_active": true,
  ...
}
```

### IntegrationStatusResponse Schema
Same additions as `IntegrationResponse`.

## Service Layer Changes

### `IntegrationService.create_integration()`

**New Parameters:**
```python
async def create_integration(
    self,
    integration_type: IntegrationType,
    name: str,
    connection_id: str,
    created_by_user_id: UUID,           # NEW
    created_by_admin: bool,             # NEW
    integration_scope: IntegrationScope, # NEW
    integration_email: Optional[str] = None,  # NEW
    ...
) -> KnowledgeIntegration:
```

**How Values Are Determined:**

1. **`created_by_user_id`**
   - Extracted from JWT token in request
   - Represents the internal user who connected the integration

2. **`created_by_admin`**
   - Derived from `user.user_role == UserAppRole.ADMIN`
   - Boolean flag for UI display and filtering

3. **`integration_email`**
   - Extracted from Nango connection metadata
   - Provider-specific: `metadata.email`, `metadata.user`, etc.
   - Can be `null` if provider doesn't provide email

4. **`integration_scope`**
   - Determined from `IntegrationMetadata.default_scope`
   - HubSpot/Salesforce → `ORGANIZATION`
   - Google Drive/Dropbox → `USER`

## Frontend Implementation

### Displaying Access Control Info

```typescript
interface Integration {
  integration_id: string;
  name: string;
  integration_type: string;
  
  // Access Control Fields
  created_by_user_id: string;
  created_by_admin: boolean;
  integration_email: string | null;
  integration_scope: 'organization' | 'user' | 'team' | 'role_based';
  
  // ... other fields
}

// Display component
const IntegrationCard = ({ integration }: { integration: Integration }) => {
  return (
    <div className="integration-card">
      <h3>{integration.name}</h3>
      
      {/* Show connected account email */}
      {integration.integration_email && (
        <p className="text-sm text-gray-600">
          Connected as: <strong>{integration.integration_email}</strong>
        </p>
      )}
      
      {/* Show scope badge */}
      <div className="flex gap-2 mt-2">
        <span className={`badge ${
          integration.integration_scope === 'organization' 
            ? 'badge-blue' 
            : 'badge-green'
        }`}>
          {integration.integration_scope === 'organization' 
            ? '🏢 Organization-wide' 
            : '👤 Personal'}
        </span>
        
        {/* Show admin badge */}
        {integration.created_by_admin && (
          <span className="badge badge-purple">
            Created by Admin
          </span>
        )}
      </div>
    </div>
  );
};
```

### Filtering by Scope

```typescript
// Show only user's personal integrations
const personalIntegrations = integrations.filter(
  (int) => int.integration_scope === 'user' && 
           int.created_by_user_id === currentUser.id
);

// Show org-wide integrations (visible to all)
const orgIntegrations = integrations.filter(
  (int) => int.integration_scope === 'organization'
);

// Show all visible integrations for current user
const visibleIntegrations = integrations.filter(
  (int) => int.integration_scope === 'organization' ||
           (int.integration_scope === 'user' && int.created_by_user_id === currentUser.id)
);
```

## Backend Implementation Notes

### OAuth Webhook Handler

When receiving webhook from Nango after OAuth completion:

```python
@router.post("/webhooks/nango")
async def nango_webhook(
    webhook_data: dict,
    current_user: User = Depends(get_current_user),
):
    # Extract user info from JWT
    user_id = current_user.user_id
    is_admin = current_user.user_role == UserAppRole.ADMIN
    
    # Get connection details from Nango
    nango_connection = await nango.get_connection(provider, connection_id)
    
    # Extract integration email from provider metadata
    metadata = nango_connection.get("connection_metadata", {})
    integration_email = None
    
    if integration_type == IntegrationType.GOOGLE_DRIVE:
        integration_email = metadata.get("email") or metadata.get("user_email")
    elif integration_type == IntegrationType.HUBSPOT:
        integration_email = metadata.get("user") or metadata.get("hub_email")
    elif integration_type == IntegrationType.SLACK:
        integration_email = metadata.get("user", {}).get("email")
    
    # Get integration metadata for scope
    integration_metadata = INTEGRATION_METADATA.get(integration_type)
    integration_scope = integration_metadata.default_scope
    
    # Create integration with all access control fields
    integration = await integration_service.create_integration(
        integration_type=integration_type,
        name=auto_generated_name,
        connection_id=connection_id,
        created_by_user_id=user_id,
        created_by_admin=is_admin,
        integration_email=integration_email,
        integration_scope=integration_scope,
        connection_metadata=metadata,
        scopes=scopes,
    )
    
    return {"status": "success"}
```

### Integration Metadata Configuration

```python
INTEGRATION_METADATA = {
    IntegrationType.HUBSPOT: IntegrationMetadata(
        default_scope=IntegrationScope.ORGANIZATION,
        can_be_created_by=[UserAppRole.ADMIN],
        allow_multiple_connections=False,
    ),
    IntegrationType.GOOGLE_DRIVE: IntegrationMetadata(
        default_scope=IntegrationScope.USER,
        can_be_created_by=[UserAppRole.MEMBER, UserAppRole.ADMIN],
        allow_multiple_connections=True,
    ),
}
```

## Testing Checklist

### Database Migration
- [ ] Run migration: `python -m migrations.migration_manager upgrade org`
- [ ] Verify columns created: `created_by_user_id`, `created_by_admin`, `integration_email`, `integration_scope`
- [ ] Verify indexes created
- [ ] Test rollback: `python -m migrations.migration_manager downgrade org -1`

### API Testing
- [ ] Create HubSpot integration as admin → verify `created_by_admin=true`, `integration_scope=organization`
- [ ] Create Google Drive as member → verify `created_by_admin=false`, `integration_scope=user`
- [ ] Verify `integration_email` is populated from OAuth metadata
- [ ] List integrations as admin → see all org + personal integrations
- [ ] List integrations as member → see only org + own personal integrations

### Frontend Testing
- [ ] Display integration email in UI
- [ ] Show scope badges (Organization vs Personal)
- [ ] Show "Created by Admin" badge when applicable
- [ ] Filter personal vs org-wide integrations
- [ ] Test with multiple Google Drive accounts (different emails shown)

## Security Considerations

1. **Access Control Enforcement**
   - Backend MUST filter integrations by scope in `list_integrations()`
   - USER-scoped integrations only returned to creator
   - ORGANIZATION-scoped integrations returned to all org users

2. **Email Privacy**
   - `integration_email` is the EXTERNAL account email, not internal user email
   - Safe to display to users who can see the integration
   - Consider privacy for user-scoped integrations

3. **Admin Permissions**
   - Only admins can create ORGANIZATION-scoped integrations for some types
   - Checked via `IntegrationMetadata.can_user_create(user_role)`
   - Enforced in OAuth initiation endpoint

## Migration Timeline

1. **Phase 1: Schema Update** ✅
   - Run database migration
   - Update API schemas
   - Update service layer

2. **Phase 2: Backend Implementation**
   - Update OAuth webhook handler to populate new fields
   - Update integration listing to filter by scope
   - Add permission checks

3. **Phase 3: Frontend Implementation**
   - Update TypeScript interfaces
   - Add UI components for displaying access control info
   - Implement filtering by scope

4. **Phase 4: Data Backfill**
   - Update existing integrations with correct `created_by_user_id`
   - Set `integration_scope` based on integration type
   - Mark admin-created integrations with `created_by_admin=true`

## Related Documentation

- **API Docs:** `docs/API Documentation/integrations-api.md`
- **Technical Docs:** `docs/Technical Documentation/integration-access-control-schema.md`
- **Implementation Guide:** `docs/Technical Documentation/integration-access-control-next-steps.md`

## Questions & Answers

**Q: Can a user see another user's personal Google Drive integration?**
A: No. USER-scoped integrations are only visible to the creator.

**Q: What if integration_email is null?**
A: Some providers don't share email. Display "Connected" without email in UI.

**Q: Can members create ORGANIZATION-scoped integrations?**
A: Depends on integration type. Configured in `IntegrationMetadata.can_be_created_by`.

**Q: How do I change an integration from USER to ORGANIZATION scope?**
A: Update the `integration_scope` field. Consider also updating `created_by_admin` if applicable.

**Q: What happens to existing integrations after migration?**
A: They get `created_by_user_id='00000000-0000-0000-0000-000000000000'` temporarily. Run backfill script to set correct values.
