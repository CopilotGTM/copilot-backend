# Admin Scripts

This directory contains administrative scripts for managing organizations, tenants, and related database operations.

## Scripts

### `init_super_admin.py`
Creates the initial super admin account on first deployment. This script:
- Reads `SUPER_ADMIN_EMAIL` from environment variables
- Creates a super admin account with a temporary password
- Sends an email with login credentials
- Only runs if no super admin exists

### `create_organization.py`
Creates a new organization with the specified configuration. This script sets up:
- Organization record in the master database
- Database schema for the organization
- Initial database migrations

### `delete_organizations.py`
Deletes organizations and their associated data. This script:
- Removes organization records from the master database
- Drops organization-specific database schemas
- Cleans up associated data

### `delete_tenants.py`
Deletes tenant data and schemas. This script:
- Removes tenant-specific database schemas
- Cleans up tenant data
- Handles multi-tenant cleanup operations

## Usage

All scripts should be run from the project root directory:

```bash
# Initialize super admin (first deployment)
python scripts/admin/init_super_admin.py

# Create a new organization
python scripts/admin/create_organization.py

# Delete organizations
python scripts/admin/delete_organizations.py

# Delete tenants
python scripts/admin/delete_tenants.py
```

## Environment Variables

The following environment variables are required for admin functionality:

```bash
# Super Admin Configuration
SUPER_ADMIN_EMAIL=admin@yourcompany.com

# Supabase Configuration
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your-anon-key
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
SUPABASE_JWT_SECRET=your-jwt-secret

# Email Configuration (Resend)
RESEND_API_KEY=your-resend-api-key
FROM_EMAIL=noreply@yourdomain.com

# Application Configuration
APP_NAME=CopilotGTM
APP_URL=https://your-app-domain.com
```

## Prerequisites

- Database connection configured
- Supabase project set up with authentication
- Resend account configured for email delivery
- Proper environment variables set
- Admin privileges for database operations 