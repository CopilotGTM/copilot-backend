# Observability Metrics, SLI, and SLO Foundation

Date: 2026-04-22
Worktree: `D:\CopilotGTM\copilot-backend-observability-hardening`

## What changed

The backend now exports first-class OTLP metrics in addition to traces and logs.

Instrumented metric families:

- `copilotgtm.http.server.request.count`
- `copilotgtm.http.server.request.duration`
- `copilotgtm.celery.task.execution.count`
- `copilotgtm.celery.task.duration`

Code locations:

- API request runtime: `src/api/middleware/request_context.py`
- Celery task runtime: `src/workers/base/celery_base.py`
- OTLP metrics exporter bootstrap: `src/infra/observability/otel.py`
- Shared metric helpers: `src/infra/observability/metrics.py`

## Metric attributes

### HTTP metrics

- `http.method`
- `http.route`
- `http.response.status_code`
- `http.status_family`

Notes:

- `http.route` uses the route template when available, for example `/users/crm/deals/{deal_id}`.
- This avoids raw-UUID path cardinality explosions.

### Celery metrics

- `celery.task_name`
- `celery.queue`
- `celery.status`

Notes:

- `celery.status` is taken from task postrun state and normalized to uppercase.
- Duration is only recorded when a start timestamp was captured in `task_prerun`.

## Proposed initial SLIs

These are the first SLIs that should be wired into Grafana dashboards and alerting.
They are intentionally simple and tied directly to the new metric layer.

### API availability

Definition:

- Successful requests / total requests
- Exclude internal health endpoints from the SLI

Implementation basis:

- `copilotgtm.http.server.request.count`
- Error budget burn should focus on `http.status_family="5xx"`

### API latency

Definition:

- p95 request duration for user-facing API routes

Implementation basis:

- `copilotgtm.http.server.request.duration`

### Celery reliability

Definition:

- Successful task executions / total task executions by `celery.task_name`

Implementation basis:

- `copilotgtm.celery.task.execution.count`
- Failure analysis uses `celery.status!="SUCCESS"`

### Celery latency

Definition:

- p95 task duration by `celery.task_name`

Implementation basis:

- `copilotgtm.celery.task.duration`

## Proposed initial SLOs

These are recommended starting points, not contractual SLA commitments.

### API

- Availability: 99.5% monthly for non-health API routes
- Latency: p95 under 1500 ms for read-heavy routes
- Latency: p95 under 3000 ms for write-heavy routes

### Celery

- Reliability: 99.0% daily success rate for infrastructure and sync tasks
- Reliability: 97.0% daily success rate for LLM-heavy analysis tasks
- Latency: task-specific p95 targets should be grouped by worker and job class, not forced into one global threshold

## First alert candidates

### API 5xx burn

- Trigger when 5xx ratio exceeds short-window and long-window thresholds
- Start with burn-rate style alerting instead of absolute-count alerting

### API latency regression

- Trigger when p95 latency breaches target for sustained periods
- Break down by `http.route`

### Celery task failure spike

- Trigger when failure ratio increases for a specific `celery.task_name`
- This should page only for critical scheduled/sync workflows

### Celery task latency regression

- Trigger when p95 duration increases materially for a specific task family
- This is especially useful for sync coordinators and long-running analysis tasks

## What is still not done

- Grafana dashboards are still mostly log-derived and have not yet been rewritten to use these new metrics.
- Alert rules are not yet provisioned in repo.
- SLO query definitions need to be adapted to the metric backend actually used behind OTLP ingestion.

## Recommended next implementation step

Update `docs/grafana/dashboards/platform-overview.json` so the headline request/error/latency panels read from the new metrics instead of Loki log queries.
