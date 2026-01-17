# Local Development Docker Configurations

This folder contains Docker configurations for local development, including observability tools.

## Structure

```
docker/
├── grafana/
│   ├── dashboards/
│   │   └── copilot-logs.json      # Pre-configured log dashboard
│   └── provisioning/
│       ├── dashboards/            # Dashboard auto-provisioning
│       └── datasources/           # Loki datasource config
└── loki/
    ├── loki-config.yml            # Loki log storage config
    └── promtail-config.yml        # Log collection config
```

## Quick Start

Start the monitoring stack alongside your application:

```bash
docker-compose --profile monitoring up -d
```

Access Grafana at: **http://localhost:3001** (admin/admin)

## Services

| Service | Port | Description |
|---------|------|-------------|
| Grafana | 3001 | Log visualization dashboard |
| Loki | 3100 | Log aggregation and storage |
| Promtail | 9080 | Log collection agent |

## Dashboard Features

The **Copilot Backend Logs** dashboard provides:

- **Overview Stats**: Total logs, errors, warnings, active services
- **Errors by Service**: Click any error count to drill down
- **Log Volume Timeline**: Visual log distribution over time
- **Request Tracing**: Trace a request across API and workers (requires JSON logs)
- **Service Logs**: Side-by-side log panels for each service

## Enabling JSON Logs (Recommended)

For full request tracing capabilities, enable JSON logging:

```bash
# In your .env file
LOG_FORMAT=json
```

This enables:
- Structured log fields (request_id, celery_task_id, etc.)
- Request tracing across services
- Failed request ID tracking

## Container to Service Mapping

| Container | Queues |
|-----------|--------|
| copilot-api | N/A (HTTP server) |
| copilot-deal-worker | deal, deal_jobs |
| copilot-chat-worker | attachments, chat_processing, chat_messages |
| copilot-integrations-worker | integrations, integrations_priority |
| copilot-knowledge-extract-worker | knowledge_extract |
| copilot-knowledge-upload-worker | knowledge_upload |
| copilot-organization-worker | organization |

## Troubleshooting

**No logs appearing?**
- Ensure containers are running: `docker-compose ps`
- Check Promtail can access Docker socket
- Verify Loki is healthy: `curl http://localhost:3100/ready`

**"No data" in dashboard panels?**
- Wait 30 seconds for logs to be ingested
- Check time range in Grafana (default: Last 1 hour)
- Ensure the Service filter includes your containers

**Request tracing not working?**
- Set `LOG_FORMAT=json` in your `.env`
- Restart your services after changing the env var
