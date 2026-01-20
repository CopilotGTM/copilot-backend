# PgBouncer Connection Pooling

This document explains the PgBouncer connection pooling setup for optimizing database connections across all workers and the API.

## Problem Statement

With 6-8 Celery workers + 1 FastAPI API, each process maintains its own SQLAlchemy connection pool to PostgreSQL:

```
┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│  FastAPI    │  │Deal Worker  │  │Chat Worker  │  ... (6-8 total)
│  API App    │  │             │  │             │
└──────┬──────┘  └──────┬──────┘  └──────┬──────┘
       │                │                │
       │  SQLAlchemy    │  SQLAlchemy    │  SQLAlchemy
       │  Pool (5+2)    │  Pool (5+0)    │  Pool (5+0)
       │                │                │
       ▼                ▼                ▼
┌──────────────────────────────────────────────────┐
│         PostgreSQL (Azure Flexible Server)       │
└──────────────────────────────────────────────────┘
```

**Issues:**
- **Connection multiplication**: 8 processes × 5 pool_size = 40+ potential connections per tenant
- **No connection sharing**: Each worker creates independent connections, even for the same organization
- **Resource waste**: Idle connections in one worker can't be used by another

## Solution: PgBouncer Connection Pooler

PgBouncer sits between your application and PostgreSQL, managing a shared pool of connections:

```
┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│  FastAPI    │  │Deal Worker  │  │Chat Worker  │  ... (6-8 total)
│  API App    │  │             │  │             │
└──────┬──────┘  └──────┬──────┘  └──────┬──────┘
       │                │                │
       │  SQLAlchemy    │  SQLAlchemy    │  SQLAlchemy
       │  Pool (2+0)    │  Pool (2+0)    │  Pool (2+0)
       │                │                │
       └────────────────┼────────────────┘
                        │ Port 6432
                        ▼
              ┌─────────────────────┐
              │      PgBouncer      │
              │  (Transaction Mode) │
              │  Pool: 50-100 conn  │
              └──────────┬──────────┘
                         │ Port 5432
                         ▼
              ┌─────────────────────┐
              │     PostgreSQL      │
              └─────────────────────┘
```

**Benefits:**
- All processes share PgBouncer's connection pool
- ~20-30 total PostgreSQL connections instead of 100+
- Per-tenant credentials still work (validated against `pg_shadow`)

## Azure Flexible Server Built-in PgBouncer

Azure Database for PostgreSQL Flexible Server has **built-in PgBouncer**. No additional infrastructure needed.

### Enable via Azure Portal

1. Go to Azure Portal → Your PostgreSQL Flexible Server
2. Navigate to **Server parameters**
3. Set these parameters:
   | Parameter | Value | Description |
   |-----------|-------|-------------|
   | `pgbouncer.enabled` | `on` | Enable PgBouncer |
   | `pgbouncer.default_pool_size` | `50` | Connections per user/database pair |
   | `pgbouncer.max_client_conn` | `200` | Max client connections |
   | `pgbouncer.pool_mode` | `transaction` | Release connection after transaction |
4. Click **Save** (server will restart)

### Enable via Azure CLI

```bash
# Enable PgBouncer
az postgres flexible-server parameter set \
  --resource-group <your-rg> \
  --server-name <your-server> \
  --name pgbouncer.enabled \
  --value on

# Set pool size
az postgres flexible-server parameter set \
  --resource-group <your-rg> \
  --server-name <your-server> \
  --name pgbouncer.default_pool_size \
  --value 50

# Set max client connections
az postgres flexible-server parameter set \
  --resource-group <your-rg> \
  --server-name <your-server> \
  --name pgbouncer.max_client_conn \
  --value 200

# Set pool mode
az postgres flexible-server parameter set \
  --resource-group <your-rg> \
  --server-name <your-server> \
  --name pgbouncer.pool_mode \
  --value transaction
```

## Application Configuration

### Environment Variables

```env
# Enable PgBouncer (Azure Flexible Server built-in)
USE_PGBOUNCER=true
PGBOUNCER_PORT=6432

# Reduce local pool sizes since PgBouncer handles pooling
DATABASE_POOL_SIZE=2
DATABASE_MAX_OVERFLOW=0
```

### Config Class (`src/infra/config/configs.py`)

```python
class DatabaseConfig:
    # ... existing config ...

    # PgBouncer Configuration
    USE_PGBOUNCER: bool = os.getenv("USE_PGBOUNCER", "false").lower() == "true"
    PGBOUNCER_PORT: int = int(os.getenv("PGBOUNCER_PORT", "6432"))
```

### Database Providers

The providers (`provider_sync.py`, `provider_async.py`) should:

1. Use PgBouncer port when enabled:
   ```python
   port = database_config.PGBOUNCER_PORT if database_config.USE_PGBOUNCER else database_config.DATABASE_PORT
   ```

2. Reduce local pool sizes when PgBouncer handles pooling:
   ```python
   if database_config.USE_PGBOUNCER:
       pool_size = 2
       max_overflow = 0
   ```

## Per-Tenant Credentials

Your existing per-tenant authentication continues to work unchanged:

1. **Workers fetch tenant password** from Azure Key Vault (no change)
2. **Connect to PgBouncer** on port 6432 with tenant credentials
3. **PgBouncer validates** credentials against PostgreSQL's `pg_shadow` table
4. **Connection routed** to PostgreSQL using pooled connection

```
Worker → Key Vault (fetch password)
      → PgBouncer:6432 (tenant credentials)
      → pg_shadow validation
      → PostgreSQL (pooled connection)
```

**Key point**: PgBouncer's `auth_type = md5` validates credentials the same way PostgreSQL does. Your tenant roles already exist in `pg_shadow`, so they authenticate correctly.

## Local Development Setup

For local development, add PgBouncer to `docker-compose.yml`:

```yaml
  # ---------------------------------------------------------------------------
  # PgBouncer - Connection Pooler (mirrors Azure Flexible Server built-in)
  # ---------------------------------------------------------------------------
  pgbouncer:
    image: edoburu/pgbouncer:1.21.0
    container_name: copilot-pgbouncer
    ports:
      - "6432:6432"
    environment:
      - DATABASE_URL=postgres://${DATABASE_USER}:${DATABASE_PASSWORD}@postgres:5432/${DATABASE_NAME}
      - POOL_MODE=transaction
      - DEFAULT_POOL_SIZE=50
      - MAX_CLIENT_CONN=200
      - MAX_DB_CONNECTIONS=100
      - AUTH_TYPE=md5
    depends_on:
      - postgres
    profiles: ["full", "api", "workers"]
```

### Local Per-Tenant Auth

For local development with per-tenant auth, you have two options:

**Option A: Use auth_query** (recommended)

Create `config/pgbouncer/pgbouncer.ini`:
```ini
[databases]
* = host=postgres port=5432

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
auth_type = md5
auth_query = SELECT usename, passwd FROM pg_shadow WHERE usename=$1
pool_mode = transaction
default_pool_size = 50
max_client_conn = 200
```

**Option B: Use single admin user for local dev**

Simpler for development - use the main postgres user for all connections locally.

## Pool Mode Explanation

| Mode | Description | Use Case |
|------|-------------|----------|
| `session` | Connection held for entire client session | Long-lived connections |
| `transaction` | Connection released after each transaction | **Recommended for web apps** |
| `statement` | Connection released after each statement | Not recommended (breaks transactions) |

**We use `transaction` mode** because:
- Web requests are short-lived
- Each request = one transaction
- Maximizes connection sharing

## Connection Savings

| Metric | Before PgBouncer | After PgBouncer |
|--------|------------------|-----------------|
| SQLAlchemy pool per process | 5-7 | 2 |
| Total app-side connections | 40-60 | 16-20 |
| PostgreSQL connections | 40-60 | 20-30 (shared) |
| Idle connection waste | High | Low |

## Monitoring

### Check PgBouncer is enabled (Azure)

```bash
az postgres flexible-server parameter show \
  --resource-group <rg> \
  --server-name <server> \
  --name pgbouncer.enabled
```

### Test connection via PgBouncer

```bash
# Azure
psql "host=<server>.postgres.database.azure.com port=6432 dbname=<db> user=<tenant_role> sslmode=require"

# Local
psql -h localhost -p 6432 -U postgres -d copilot
```

### Monitor PostgreSQL connections

```sql
-- Total active connections
SELECT count(*) FROM pg_stat_activity WHERE datname = 'your_db';

-- Connections by user
SELECT usename, count(*)
FROM pg_stat_activity
WHERE datname = 'your_db'
GROUP BY usename;

-- Connections by state
SELECT state, count(*)
FROM pg_stat_activity
WHERE datname = 'your_db'
GROUP BY state;
```

### Monitor PgBouncer

Connect to PgBouncer's admin database:

```bash
psql -h <host> -p 6432 -U <admin_user> pgbouncer
```

Then run:

```sql
-- Pool statistics
SHOW POOLS;

-- Client connections
SHOW CLIENTS;

-- Server connections (to PostgreSQL)
SHOW SERVERS;

-- Overall stats
SHOW STATS;
```

## Troubleshooting

### "FATAL: no pg_hba.conf entry for host"

PgBouncer is trying to connect but PostgreSQL isn't allowing the connection. Check `pg_hba.conf` allows connections from PgBouncer's IP.

### "FATAL: password authentication failed"

1. Verify the tenant role exists in PostgreSQL
2. Check password in Key Vault matches PostgreSQL
3. Ensure PgBouncer can query `pg_shadow` (for auth_query mode)

### Connection timeouts

1. Increase `pgbouncer.max_client_conn`
2. Check if pool is exhausted: `SHOW POOLS;`
3. Reduce SQLAlchemy pool size to push more pooling to PgBouncer

### Transactions not committing

If using `statement` mode (not recommended), transactions won't work. Use `transaction` mode.

## Rollback Plan

If issues occur after enabling PgBouncer:

1. Set `USE_PGBOUNCER=false` in environment variables
2. Restart all workers and API
3. Connections will go directly to PostgreSQL on port 5432
4. Investigate issues with PgBouncer disabled

## Kubernetes Deployment

For Azure Flexible Server, no separate PgBouncer deployment needed. Just set environment variables:

```yaml
# In your Kubernetes secret or ConfigMap
USE_PGBOUNCER: "true"
PGBOUNCER_PORT: "6432"
DATABASE_POOL_SIZE: "2"
DATABASE_MAX_OVERFLOW: "0"
```

If using a non-Azure PostgreSQL that lacks built-in PgBouncer, deploy PgBouncer as a separate service. See `charts/application/charts/pgbouncer/` for Helm chart (if implemented).

## Related Documentation

- [Request Tracing and Flower](./request-tracing-and-flower.md) - Monitoring Celery tasks
- [AKS Logging Best Practices](./aks-logging-best-practices.md) - Log aggregation for debugging
- [Celery Worker Creation Guide](./celery-worker-creation-guide.md) - Worker patterns including DB session management
