# Maintenance Scripts

This folder contains routine maintenance and cleanup scripts.

## Purpose
Scripts for ongoing system maintenance, optimization, and cleanup tasks.

## Recommended Scripts (to be added)

### `cleanup_logs.py`
- Clean up old log files
- Rotate application logs
- Archive historical data

### `rotate_secrets.py`
- Rotate API keys and secrets
- Update secret references
- Validate secret rotation

### `optimize_database.py`
- Database maintenance tasks
- Index optimization
- Statistics updates

### `health_report.py`
- Generate system health reports
- Performance metrics summary
- Capacity planning data

## Gmail master routing

`backfill_gmail_master_routes.py` audits enabled Gmail integrations against the
public integration locator. It is read-only by default and does not print email
addresses:

```powershell
python scripts/maintenance/backfill_gmail_master_routes.py
```

After the master migration has added `integration_email`, pass `--execute` to
write only normalized emails whose connection, organisation, and integration
identifiers already match.
## Access Control
- **Operations Team**: Read/Execute/Modify
- **Database Admins**: Read/Execute (DB scripts)
- **DevOps**: Read/Execute

## Scheduling
These scripts should be:
- Scheduled during maintenance windows
- Run during low-traffic periods
- Automated via cron jobs or Azure Functions
- Monitored for completion and errors
