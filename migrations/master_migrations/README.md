# Master Migrations

This directory contains migrations for the master schema (public) that manages organizations, users, and other system-wide data.

## Purpose

Master migrations handle changes to:
- `public.organisations` table
- `public.users` table  
- `public.org_migrations` table
- Any other system-wide tables in the public schema

## Structure

```
migrations/master_migrations/
├── alembic.ini          # Alembic configuration for master migrations
├── env.py               # Migration environment
├── script.py.mako       # Migration template
├── versions/            # Migration files
│   ├── 001_add_supabase_support_and_chat_timestamps.py
│   └── 002_add_organization_schema_fields.py
└── README.md           # This file
```

## Usage

### Command Line

```bash
# Run master migrations to latest version
python scripts/run_master_migrations.py

# Run to specific version
python scripts/run_master_migrations.py --revision 002

# Downgrade master migrations
python scripts/run_master_migrations.py --revision 001 --downgrade
```

### Direct Alembic

```bash
# Upgrade to latest
alembic -c migrations/master_migrations/alembic.ini upgrade head

# Upgrade to specific version
alembic -c migrations/master_migrations/alembic.ini upgrade 002

# Downgrade
alembic -c migrations/master_migrations/alembic.ini downgrade 001
```

### API Endpoint

```http
POST /admin/organizations/migrate/master?target_version=head
```

## Migration Files

### 001_add_supabase_support_and_chat_timestamps.py
- Adds `supabase_user_id` field to users table
- Adds `created_at` and `updated_at` timestamps to chat_messages
- Drops the old `user_credentials` table

### 002_add_organization_schema_fields.py
- Adds `schema_name` and `role_name` fields to organisations table
- Adds `created_at` and `updated_at` timestamps to organisations table

## Important Notes

1. **Never run organization migrations here** - Use `migrations/org_migrations/` for organization-specific changes
2. **Always test migrations** - Master migrations affect the entire system
3. **Backup before running** - Master migrations modify system tables
4. **Run in order** - Migrations must be applied sequentially

## Separation from Organization Migrations

- **Master migrations**: System-wide changes (public schema)
- **Organization migrations**: Organization-specific changes (org schemas)

This separation ensures:
- Clear distinction between system and organization changes
- Independent migration management
- Better organization and maintenance
- Reduced confusion during development 