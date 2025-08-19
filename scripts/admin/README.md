# Admin Scripts

This directory contains administrative scripts for managing the initial setup and super admin account.

## Scripts

### `deploy_with_super_admin.py`

This script orchestrates the deployment process, including running database migrations and initializing the super admin account. It is designed to be run in automated deployment environments.

**What it does:**

1.  **Checks Environment Variables:** Verifies that all required environment variables are set.
2.  **Runs Master Migrations:** Executes the main `run_master_migrations.py` script.
3.  **Initializes Super Admin:** Calls `init_super_admin.py` to create the super admin account if it doesn't exist.

### `init_super_admin.py`

This script creates the initial super admin account.

**What it does:**

-   Reads `SUPER_ADMIN_EMAIL` from environment variables.
-   Creates a super admin account with a temporary password if one does not already exist.
-   Sends an email to the super admin with login credentials.

## Usage

The `deploy_with_super_admin.py` script is the primary entry point for automated deployments.

```bash
# Run the complete deployment and initialization process
python scripts/admin/deploy_with_super_admin.py
```

To run the super admin initialization manually:

```bash
# Initialize super admin (if not done by the deployment script)
python scripts/admin/init_super_admin.py
```

## Environment Variables

The following environment variables are required for these scripts to function correctly:

```bash
# Super Admin Configuration
SUPER_ADMIN_EMAIL=admin@yourcompany.com

# Database Configuration
DATABASE_HOST=your-database-host
DATABASE_USER=your-database-user
DATABASE_PASSWORD=your-database-password
DATABASE_NAME=your-database-name

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

-   Database connection must be configured and accessible.
-   A Supabase project must be set up for authentication.
-   A Resend account must be configured for sending emails.
-   All required environment variables must be set.
