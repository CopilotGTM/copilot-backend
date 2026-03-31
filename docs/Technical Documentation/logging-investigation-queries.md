# Logging Investigation Queries

This document defines the concrete Grafana/Loki queries the logging system must support as part of Step 2 of the logging remediation plan.

It intentionally separates:

- queries that work with the fields emitted today
- queries that become cleaner after Step 6 field normalization

Use this document as the contract for later logging cleanup. If a future code change removes support for one of these investigations, that is a regression.

## Current Structured Fields Available Today

From the current logging pipeline, these fields are reliably available in structured logs:

- `timestamp`
- `level`
- `logger`
- `message`
- `environment`
- `process_id`
- `process_name`
- `request_id`
- `celery_task_id`
- `celery_task_name`
- `celery_queue`

Additional request fields are already emitted by access logging:

- `event="http_request"`
- `http_method`
- `http_path`
- `http_status_code`
- `duration_ms`

## Investigation Queries

### 1. Find API failures

Current query:

```logql
{job="api"} | json | http_status_code >= 500
```

Purpose:

- find failed API requests quickly
- separate request failures from background worker failures

Expected result:

- one row per failed HTTP request
- includes `request_id`, `http_method`, `http_path`, `http_status_code`, `duration_ms`

### 2. Find slow API requests

Current query:

```logql
{job="api"} | json | event="http_request" | duration_ms > 2000
```

Purpose:

- identify slow API requests before they become incidents
- verify that performance issues are visible without reading full traces

Expected result:

- slow requests with path, status code, duration, and request id

Note:

- `2000ms` is the initial working threshold for investigation queries
- this does not force slow-request promotion in code yet; it only defines the query contract

### 3. Find authentication failures

Current fallback query:

```logql
{job="api"} | json | logger=~"supabase_user_client|user_auth|user_auth_handler" | message=~".*Token verification failed.*|.*Token refresh failed.*|.*Authentication error.*"
```

Target query after Step 6:

```logql
{job="api"} | json | event="auth_failure"
```

Purpose:

- find invalid JWTs, refresh-token failures, and auth-path errors
- measure whether auth failures are being over-classified as system errors

Expected result:

- auth failures grouped by logger and message today
- a clean `event`-based query after field normalization

### 4. Trace a single request

Current query:

```logql
{job="api"} | json | request_id="<request-id>"
```

Purpose:

- follow one API request through all related API log lines
- correlate request timing and failures

Expected result:

- all log lines for one request
- includes access logs and any related application logs that propagated `request_id`

### 5. Trace a Celery task

Current query:

```logql
{job=~"worker|deal-worker|chat-worker|knowledge-worker|integrations-worker|organization-worker"} | json | celery_task_id="<task-id>"
```

Purpose:

- follow a single worker task across start, execution, and failure/completion logs
- debug queue-specific issues without scanning all worker traffic

Expected result:

- all log lines carrying the task id
- includes `celery_task_name` and `celery_queue` when present

### 6. Correlate one API request to worker activity

Current query:

```logql
{job=~"api|worker|deal-worker|chat-worker|knowledge-worker|integrations-worker|organization-worker"} | json | request_id="<request-id>"
```

Purpose:

- trace an HTTP request into downstream Celery execution
- validate request-to-worker correlation

Expected result:

- API logs and any worker logs that inherited the same `request_id`

### 7. Find tenant secret failures

Current fallback query:

```logql
{job="api"} | json | logger=~"azure_key_vault_provider|secrets_manager" | level=~"WARNING|ERROR" | message=~".*secret.*|.*Key Vault.*"
```

Target query after Step 6:

```logql
{job="api"} | json | event="secret_fetch_failed"
```

Purpose:

- isolate tenant secret fetch failures from general startup or info chatter
- distinguish retry noise from terminal failures

Expected result:

- warning/error records related to secret retrieval today
- a clean event-driven query after field normalization

### 8. Find external dependency failures

Current query:

```logql
{job=~"api|worker|deal-worker|chat-worker|knowledge-worker|integrations-worker|organization-worker"} | json | level="ERROR" | logger=~"azure_key_vault_provider|secrets_manager|nango_client|supabase_user_client"
```

Purpose:

- isolate failures from the highest-value external dependencies involved in auth, secrets, and integrations

Expected result:

- error-level dependency failures without broad application noise

## Query Gaps To Close In Step 6

These queries currently depend on message matching or logger names and should become event-driven after field normalization:

- auth failures: move to `event="auth_failure"`
- secret failures: move to `event="secret_fetch_failed"`
- integration failures: likely move to specific events such as `integration_action_failed` or `nango_request_failed`

## Acceptance Criteria For Step 2

Step 2 is complete when:

- this document exists and is approved
- each investigation has at least one concrete query
- each query is labeled as `current` or `target after Step 6`
- Step 6 field work is driven by the gaps documented here, not by guesswork

