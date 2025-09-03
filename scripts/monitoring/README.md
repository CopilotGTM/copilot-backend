# Monitoring Scripts

This folder contains production monitoring and health check scripts.

## Scripts

### `monitor_secrets.py`
**Purpose**: Secret Management System Health Monitor
**Usage**: 
```bash
python monitoring/monitor_secrets.py --health
python monitoring/monitor_secrets.py --metrics
python monitoring/monitor_secrets.py --diagnostics
```

**When to use**:
- Production health monitoring
- Troubleshooting secret access issues
- Performance optimization
- Automated monitoring systems

**Required for**:
- ✅ Pre-deployment validation
- ✅ Production monitoring
- ✅ Incident response
- ✅ Performance tuning

## Access Control
- **Operations Team**: Read/Execute
- **DevOps**: Read/Execute
- **SRE**: Read/Execute/Modify
- **Security Team**: Read only

## Automation
These scripts are designed to be:
- Run by monitoring systems (Prometheus, Grafana, etc.)
- Integrated into alerting pipelines
- Executed on schedules (cron jobs, Azure Functions)
- Called from CI/CD health checks
