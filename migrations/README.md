# Database Migrations

This directory contains database migrations organized by scope and purpose.

## Structure

```
migrations/
├── master_migrations/          # Master schema migrations (public)
│   ├── alembic.ini           # Master migrations config
│   ├── env.py                # Migration environment
│   ├── script.py.mako        # Migration template
│   ├── versions/             # Master migration files
│   └── README.md             # Master migrations documentation
├── org_migrations/           # Organization-specific migrations
│   ├── alembic.ini          # Organization migrations config
│   ├── env.py               # Migration environment
│   ├── script.py.mako       # Migration template
│   └── versions/            # Organization migration files
└── README.md                # This file
```

## Migration Types

### Master Migrations (`master_migrations/`)
- **Purpose**: System-wide changes in the `public` schema
- **Scope**: Organizations, users, system tables
- **Usage**: `python scripts/run_master_migrations.py`
- **Examples**: Adding organization fields, user authentication changes

### Organization Migrations (`org_migrations/`)
- **Purpose**: Organization-specific changes in org schemas
- **Scope**: Chat sessions, messages, knowledge base, etc.
- **Usage**: `python scripts/run_org_migrations.py`
- **Examples**: Adding chat features, embedding tables

## Default Configuration

The main `alembic.ini` points to `master_migrations/` by default, so standard alembic commands will work with master migrations:

```bash
# These commands work with master migrations
alembic upgrade head
alembic revision --autogenerate -m "description"
alembic downgrade -1
```

## Organization-Specific Commands

For organization migrations, use the dedicated scripts:

```bash
# Run organization migrations
python scripts/run_org_migrations.py org1 org2

# Create new organization
python scripts/create_organization.py --name "Org Name" --admin-email "admin@org.com"
```

## Best Practices

1. **Master migrations first**: Always run master migrations before organization migrations
2. **Test migrations**: Always test migrations in development before production
3. **Backup data**: Backup before running migrations in production
4. **Version control**: Commit migration files to version control
5. **Documentation**: Update this README when adding new migration types

## Migration Workflow

1. **Development**: Create and test migrations locally
2. **Staging**: Run migrations on staging environment
3. **Production**: Run migrations on production with proper backups
4. **Rollback**: Keep rollback plans ready for critical migrations 