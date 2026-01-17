# Grafana Dashboard Enhancement Plan

## Overview

This document outlines the plan to enhance the Copilot Backend Logs Grafana dashboard with best practices for observability, focusing on quick issue resolution through drill-down capabilities and request correlation.

## Current State Assessment

### What We Have
- Basic Grafana + Loki + Promtail stack deployed via Helm
- Dashboard with:
  - Overview stats (Total Logs, Total Errors, Warnings, Active Services)
  - Error counts by service (API, Deal Worker, Chat Worker, etc.)
  - Log Volume Timeline
  - Request Tracing section (requires LOG_FORMAT=json)
  - Individual worker log panels
- Labels: `app`, `instance`, `pod`, `container`, `namespace`, `node`
- JSON log parsing with fields: `level`, `logger`, `request_id`, `celery_task_id`, `celery_task_name`, `celery_queue`

### Current Gaps
1. No click-through from error counts to filtered logs
2. Request ID correlation requires manual query entry
3. Missing request rate/throughput metrics
4. No easy navigation from aggregated view to detailed logs
5. No latency/response time visibility
6. Error panels don't link to the actual error logs

## Research Summary: Best Practices

### From Grafana Labs
- **Data Links**: Enrich URLs with query result metadata for drill-down ([Grafana Data Links](https://grafana.com/docs/grafana/latest/panels-visualizations/configure-data-links/))
- **Template Variables**: Text box for line filters, label filters with dropdowns ([6 Ways to Improve Dashboards](https://grafana.com/blog/2023/05/18/6-easy-ways-to-improve-your-log-dashboards-with-grafana-and-grafana-loki/))
- **Panel Links**: Navigate from stat panels to detailed views
- **Explore Integration**: Link dashboard to Explore for ad-hoc investigation

### From Observability Best Practices
- **Correlation IDs**: Use consistent `request_id` across all services ([Microsoft Engineering Playbook](https://microsoft.github.io/code-with-engineering-playbook/observability/correlation-id/))
- **Three Pillars**: Integrate traces, logs, and metrics with consistent naming ([Distributed Tracing Best Practices](https://coralogix.com/guides/observability/distributed-tracing/))
- **Drill-Down Design**: From high-level overview → service-level → request-level → individual log lines

## Proposed Dashboard Structure

### Phase 1: Enhanced Overview with Drill-Down Links

#### 1.1 Service Health Row (Enhanced)
| Panel | Type | Click Action |
|-------|------|--------------|
| Total Requests (1h) | Stat | Link to Request Volume panel |
| Error Rate (%) | Stat | Link to Error Logs filtered view |
| Avg Response Time | Stat | Link to Latency breakdown |
| Active Services | Stat | Link to Service Health table |

#### 1.2 Error Summary Row (New)
| Panel | Type | Click Action |
|-------|------|--------------|
| API Errors | Stat (red if >0) | **Data Link** → Explore with `{instance="api"} \|~ "(?i)error"` |
| Worker Errors | Stat | **Data Link** → Explore with `{app="worker"} \|~ "(?i)error"` |
| Critical Errors | Stat | **Data Link** → Explore with `\|~ "(?i)critical\|fatal"` |

#### 1.3 Request Rate Graph (New)
- **Panel**: Time series showing requests/minute by service
- **Query**: `sum(count_over_time({app=~"copilot-backend\|worker"}[1m])) by (instance)`
- **Click**: Data link to filtered logs for that service

### Phase 2: Request Correlation Hub

#### 2.1 Failed Requests Table (Enhanced)
| Column | Source | Action |
|--------|--------|--------|
| Request ID | `request_id` from JSON | **Clickable** → Drill to Request Timeline |
| Service | `instance` label | Filter link |
| Error Type | Parsed from message | - |
| Timestamp | Log timestamp | - |
| Duration | If available | - |

**Data Link Configuration:**
```
URL: /d/copilot-logs/copilot-backend-logs?var-request_id=${__data.fields.request_id}&from=${__from}&to=${__to}
```

#### 2.2 Request Timeline Panel (Enhanced)
- Shows all logs for a specific `request_id`
- Sorted chronologically (API entry → Worker processing → Completion/Error)
- Color-coded by level (INFO=blue, WARN=yellow, ERROR=red)

### Phase 3: Service-Level Deep Dive

#### 3.1 Per-Service Dashboard Sections

Each worker section should include:

```
┌─────────────────────────────────────────────────────────────┐
│ [Service Name] Worker                                        │
├─────────────┬─────────────┬─────────────┬───────────────────┤
│ Total Logs  │ Errors (1h) │ Warnings    │ Avg Process Time  │
│    1,234    │     12      │     45      │     2.3s          │
├─────────────┴─────────────┴─────────────┴───────────────────┤
│ [Error Logs - Last 15 minutes]                               │
│ • 10:23:45 ERROR Task failed: Connection timeout...  [→]    │
│ • 10:21:12 ERROR Validation error: Missing field...  [→]    │
├─────────────────────────────────────────────────────────────┤
│ [Log Volume Timeline - mini]                                 │
└─────────────────────────────────────────────────────────────┘
```

#### 3.2 Clickable Error Rows
Each error row should have:
- **[→] Icon**: Data link to full log context (±5 minutes around the error)
- **Request ID** (if present): Link to Request Timeline

### Phase 4: Advanced Features

#### 4.1 Template Variables (Enhanced)
| Variable | Type | Purpose |
|----------|------|---------|
| `$service` | Query (Loki labels) | Filter by instance |
| `$level` | Custom (ERROR, WARN, INFO) | Filter by log level |
| `$search` | Text box | Free-text search |
| `$request_id` | Text box | Request correlation |
| `$celery_task_id` | Text box | Task-specific logs |

#### 4.2 Quick Action Buttons Row
- **[Show Errors Only]** → Sets `$level=ERROR`
- **[Show Warnings]** → Sets `$level=WARN`
- **[Clear Filters]** → Resets all variables
- **[Open in Explore]** → Current query in Explore mode

#### 4.3 Annotations
- Mark deployment times on timeline
- Show scaling events (HPA triggers)
- Highlight incident windows

## Implementation Details

### Data Link Examples

#### Error Stat → Filtered Logs
```json
{
  "title": "View Errors",
  "url": "/explore?orgId=1&left={\"datasource\":\"loki\",\"queries\":[{\"refId\":\"A\",\"expr\":\"{instance=\\\"${__field.labels.instance}\\\"} |~ \\\"(?i)error|exception\\\"\"}],\"range\":{\"from\":\"${__from}\",\"to\":\"${__to}\"}}"
}
```

#### Request ID → Full Trace
```json
{
  "title": "View Full Request",
  "url": "/d/copilot-logs/copilot-backend-logs?var-request_id=${__data.fields.request_id}&from=${__from}&to=${__to}"
}
```

#### Log Row → Context
```json
{
  "title": "Show Context",
  "url": "/explore?orgId=1&left={\"datasource\":\"loki\",\"queries\":[{\"refId\":\"A\",\"expr\":\"{instance=\\\"${__data.fields.instance}\\\"}\",\"context\":{\"timeEpochMs\":${__data.fields.timestamp},\"direction\":\"BACKWARD\",\"limit\":100}}]}"
}
```

### Required LOG_FORMAT=json Fields

Ensure all services emit these JSON fields:
```json
{
  "timestamp": "2024-01-16T10:23:45.123Z",
  "level": "ERROR",
  "logger": "copilot.api.routes.deals",
  "message": "Failed to process deal update",
  "request_id": "abc-123-def",
  "celery_task_id": "task-456",
  "celery_task_name": "process_deal_update",
  "celery_queue": "deal-queue",
  "org_id": "org-789",
  "user_id": "user-012",
  "duration_ms": 1234,
  "error_type": "ConnectionError",
  "stack_trace": "..."
}
```

## Dashboard Panels Summary

### Overview Row
| # | Panel | Query | Data Link |
|---|-------|-------|-----------|
| 1 | Total Logs | `sum(count_over_time({app=~".*"}[$__range]))` | → Log Volume |
| 2 | Total Errors | `sum(count_over_time({app=~".*"} \|~ "(?i)error\|exception\|critical"[$__range]))` | → Error Logs |
| 3 | Total Warnings | `sum(count_over_time({app=~".*"} \|~ "(?i)warning\|warn"[$__range]))` | → Warning Logs |
| 4 | Error Rate % | `(errors/total)*100` | → Error Rate Graph |

### Errors by Service Row (with drill-down)
| # | Panel | Query | Data Link |
|---|-------|-------|-----------|
| 5 | API Errors | `{instance="api"} \|~ error` | → Explore (API errors) |
| 6 | Deal Worker | `{instance="deal-worker"} \|~ error` | → Explore (Deal errors) |
| 7 | Chat Worker | `{instance="chat-worker"} \|~ error` | → Explore (Chat errors) |
| 8 | Integrations | `{instance="integrations-worker"} \|~ error` | → Explore (Integration errors) |
| 9 | Knowledge | `{instance=~"knowledge.*"} \|~ error` | → Explore (Knowledge errors) |
| 10 | Organization | `{instance="organization-worker"} \|~ error` | → Explore (Org errors) |

### Request Tracing Row
| # | Panel | Purpose |
|---|-------|---------|
| 11 | Request ID Input | Text variable for filtering |
| 12 | Request Timeline | Shows all logs for entered request_id |
| 13 | Failed Requests Table | Top 15 failed request IDs (clickable) |

### Service Detail Sections (Collapsible)
Each service gets:
- Stats row (logs, errors, warnings)
- Recent error logs panel (with context links)
- Log stream panel

## Rollout Plan

### Phase 1: Core Drill-Down (Priority: High)
1. Add data links to all error stat panels
2. Enhance Request ID variable with auto-complete
3. Add "Failed Requests" table with clickable request IDs
4. **Estimated panels to modify: 10**

### Phase 2: Enhanced Request Tracing (Priority: High)
1. Improve Request Timeline panel with better formatting
2. Add request duration calculation (if timestamps available)
3. Color-code log entries by level
4. **Estimated panels to add: 2**

### Phase 3: Service Deep Dive (Priority: Medium)
1. Add mini-dashboard for each service type
2. Include service-specific metrics where available
3. Add collapsible sections for cleaner navigation
4. **Estimated panels to add: 6-8**

### Phase 4: Advanced Features (Priority: Low)
1. Add annotation support for deployments
2. Create alerts dashboard link
3. Add Explore quick-action buttons
4. **Estimated panels to add: 3-4**

## Success Metrics

After implementation, measure:
1. **Mean Time to Identify (MTTI)**: Time from alert to root cause identification
2. **Click Depth**: Number of clicks needed to find relevant logs
3. **Dashboard Load Time**: Should remain under 3 seconds
4. **User Adoption**: Track dashboard visits and usage patterns

## Dependencies

1. **LOG_FORMAT=json**: Must be enabled in Kubernetes secrets
2. **Request ID propagation**: Already implemented (see `docs/Technical Documentation/request-tracing-and-flower.md`)
3. **Consistent logging**: All services must emit required JSON fields

## References

- [Grafana Data Links Documentation](https://grafana.com/docs/grafana/latest/panels-visualizations/configure-data-links/)
- [6 Easy Ways to Improve Log Dashboards](https://grafana.com/blog/2023/05/18/6-easy-ways-to-improve-your-log-dashboards-with-grafana-and-grafana-loki/)
- [Distributed Tracing Best Practices](https://coralogix.com/guides/observability/distributed-tracing/)
- [Correlation IDs - Microsoft Engineering Playbook](https://microsoft.github.io/code-with-engineering-playbook/observability/correlation-id/)
- [Dashboard Navigation Using Links](https://grafana.com/blog/2019/12/10/pro-tips-dashboard-navigation-using-links/)
- [Loki Dashboard Examples](https://grafana.com/grafana/dashboards/13186-loki-dashboard/)

---

## Approval Checklist

- [ ] Phase 1: Core Drill-Down approved
- [ ] Phase 2: Enhanced Request Tracing approved
- [ ] Phase 3: Service Deep Dive approved
- [ ] Phase 4: Advanced Features approved
- [ ] LOG_FORMAT=json enabled in all environments

**Requested by**: DevOps Team
**Created**: 2026-01-16
**Status**: Pending Approval
