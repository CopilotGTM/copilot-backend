# Scripts Directory

This directory contains production-ready utility scripts organized by purpose.

## Folder Structure

### 📊 `/monitoring/`
Production monitoring and health check scripts
- `monitor_secrets.py` - Secret management system health monitor
- Used by: Operations, DevOps, SRE teams

### 🛡️ `/security/`
Security validation and compliance tools
- `validate_health_security.py` - Health endpoint security validation
- Used by: Security teams, CI/CD pipelines

### 🚀 `/deployment/`
Application deployment and startup scripts
- `local-deploy.ps1` - Local development deployment
- Used by: DevOps, Developers

### 🔧 `/maintenance/`
Routine maintenance and cleanup scripts
- For scheduled maintenance tasks
- Used by: Operations teams
- `ensure_internal_crm_setup.py` - Ensure internal CRM integration + default pipeline exist per org
- `delete_recent_internal_deals.py` - Delete internal CRM deals created in a recent time window (dry-run by default)
- `purge_organisation_data.py` - Dry-run or atomically empty one tenant while preserving mandatory internal CRM setup; execute only with schema migrations/provisioning paused

### 👑 `/admin/`
Administrative and super-admin scripts
- `deploy_with_super_admin.py`
- `init_super_admin.py`
- Used by: System administrators

## Available Scripts (Root Level)

### `startup.sh`

This is the main startup script, likely used in a containerized environment (e.g., as a Helm hook). It orchestrates the initial setup process when the application starts.

**Usage:**

This script is typically run automatically during deployment.

**What it does:**

1.  **Runs Master Migrations:** Executes `run_master_migrations.py` to apply the latest migrations to the master database.
2.  **Runs Organization Migrations:** Executes `run_org_migrations.py --all` to bring organization schemas to head.
3.  **Initializes Super Admin (Conditional):** If the `SUPER_ADMIN_EMAIL` environment variable is set, it runs `admin/init_super_admin.py` to create the super admin user if missing.
4.  **Publishes Deal Import Template:** Uploads the deal import CSV template to app storage if missing.

---

### `run_master_migrations.py`

This script manages the master database schema, which includes tables for organizations, users, and other shared resources. It uses Alembic for handling migrations.

**Usage:**

```bash
python -m scripts.run_master_migrations [OPTIONS]
```

**Options:**

-   `--revision <REVISION>`: Specify the migration revision to upgrade or downgrade to (default: `head`).
-   `--downgrade`: Downgrade the database instead of upgrading.
-   `--skip-role`: Skip the creation of the `master_copilot_role`.
-   `--skip-migrations`: Skip running the Alembic migrations.
-   `--verbose`: Enable verbose logging.

---

### `run_org_migrations.py`

This script is used to run database migrations for specific organization schemas. Each organization has its own schema, and this script applies the necessary updates.

**Usage:**

```bash
python -m scripts.run_org_migrations [ORG_ID...] [OPTIONS]
```

**Arguments:**

-   `ORG_ID`: One or more organization IDs to migrate. If none are provided, no migrations will run.

**Options:**

-   `--target-version <VERSION>`: The target migration version (default: `head`).
-   `--list-orgs`: List all available organizations and their details, then exit.

---

### `admin/`

This subdirectory contains scripts related to administrative tasks, such as creating the initial super admin user. For more details, see the `README.md` file within the `admin/` directory.
