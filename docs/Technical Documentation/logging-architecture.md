# Logging And Telemetry Architecture

This document explains how logging and telemetry work in the backend today.

Use this document for:

- understanding how logs flow from API and Celery into Grafana/Loki
- adding new logs without breaking correlation or increasing noise
- debugging request-to-worker propagation
- understanding the difference between local, dev, and prod logging behavior

Related documents:

- `docs/Technical Documentation/logging-remediation-plan.md`
- `docs/Technical Documentation/logging-investigation-queries.md`
- `docs/grafana/README.md`

## Goals

The logging system is designed to do four things well:

- keep local logs readable during active development
- keep dev/prod logs structured for Grafana/Loki queries
- preserve request and Celery task correlation across process boundaries
- keep worker, beat, and API telemetry separated at the service level

## Main Components

### 1. Root logging pipeline

File: `src/infra/utils/logger.py`

This module owns the application logging pipeline.

Responsibilities:

- configures the root logger once per process
- emits human-readable console logs in `ENV=local`
- emits structured JSON logs in non-local environments
- injects correlation context into every record
- suppresses noisy framework and third-party loggers
- normalizes uvicorn logging to use the same pipeline

Important entry points:

- `configure_logging(component=...)`
- `get_logger(...)`
- `resolve_celery_component(...)`

### 2. Correlation context

File: `src/infra/observability/context.py`

This module stores request/task correlation data in `contextvars`.

Context currently includes:

- `request_id`
- `celery_task_id`
- `celery_task_name`
- `celery_queue`

This context is injected into every log record by the logging filter in
`src/infra/utils/logger.py`.

### 3. OpenTelemetry bootstrap

File: `src/infra/observability/otel.py`

This module initializes tracing and OTLP log export.

Responsibilities:

- builds the OTel `service.name`
- exports traces to OTLP
- exports logs to OTLP
- instruments FastAPI
- auto-instruments Celery, `httpx`, `requests`, SQLAlchemy, and Redis

### 4. API request logging

Files:

- `src/api/fastapi_app.py`
- `src/api/middleware/request_context.py`

The API process configures logging at import time and adds request middleware.

The request middleware:

- reads or generates `X-Request-Id`
- stores it in request context
- emits a structured access log on request completion
- clears context at the end of the request

### 5. Celery worker and beat bootstrap

Files:

- `src/workers/base/celery_base.py`
- `src/workers/*/celery_app.py`

The shared Celery bootstrap handles:

- task context loading from Celery headers
- task failure logging
- OTel initialization for workers and beat
- Redis transport settings
- disabling Celery root logger hijacking

Each worker entrypoint handles:

- `configure_logging(component=...)`
- worker-specific app creation
- worker lifecycle logs
- worker resource startup/shutdown such as Redis pool management

## Log Flow

## API Flow

1. `src/api/fastapi_app.py` calls `configure_logging(component="api")`
2. FastAPI request middleware sets `request_id`
3. Application code logs through `get_logger()` or stdlib loggers
4. The root logging filter injects `request_id`, trace IDs, and any Celery context
5. In `ENV=local`, the log is rendered as readable console text
6. In `ENV=dev` or `ENV=prod`, the log is rendered as JSON
7. If OTel is enabled, the same record is also exported through the OTLP log bridge

## Worker Flow

1. Each worker `celery_app.py` calls `configure_logging(component=...)`
2. `create_celery_app(...)` registers shared observability signal handlers
3. Task dispatchers attach correlation headers using `correlation_headers(...)`
4. `task_prerun` loads those headers back into runtime context
5. Worker code logs as normal
6. The root logging filter injects `request_id`, `celery_task_id`, `celery_task_name`, and `celery_queue`
7. If OTel is enabled, worker logs and spans export with a worker-specific service name

## Beat Flow

Beat uses the same Celery app module as the worker process, but it must be separated in telemetry.

Current behavior:

- JSON logs use a beat-specific `component` such as `chat_beat`
- OTel service naming also uses beat-specific suffixes such as `-chat_beat`
- beat initialization goes through Celery `beat_init` / `beat_embedded_init` hooks

This separation is important because Grafana dashboards often use OTel `service_name` as the primary selector.

## Environment Behavior

### Local

Primary goal: fast debugging.

Behavior:

- console logs are human-readable
- structured fields are rendered inline where possible
- successful API access logs are visible by default
- OTLP export is opt-in via configuration

### Dev

Primary goal: shared observability.

Behavior:

- logs are structured JSON
- successful API access logs are visible by default
- request and Celery correlation should be preserved
- OTLP export is typically enabled

### Prod

Primary goal: operational signal over noise.

Behavior:

- logs are structured JSON
- successful API access logs are suppressed by default
- 4xx and 5xx access logs remain visible
- OTLP export is typically enabled

## Structured Fields

These fields are the current baseline for structured logs.

- `timestamp`
- `level`
- `logger`
- `message`
- `environment`
- `process_id`
- `process_name`
- `component`
- `trace_id`
- `span_id`
- `request_id`
- `celery_task`
- `celery_task_id`
- `celery_queue`

Common business fields already used in application logs:

- `event`
- `organisation_id`
- `user_id`
- `deal_id`
- `stakeholder_id`
- `job_id`
- `integration_id`
- `http_method`
- `http_path`
- `http_status_code`
- `duration_ms`

Note:

- The canonical domain name is `organisation_id`, not `org_id`
- Celery task name is emitted as `celery_task`
- Some older docs and queries still refer to `celery_task_name`; new code should prefer the current emitted field names when writing new dashboards or queries

## Service And Component Naming

Two different names matter:

### `component`

This is added by the JSON formatter in `src/infra/utils/logger.py`.

Examples:

- `api`
- `chat_worker`
- `deal_worker`
- `chat_beat`

### OTel `service.name`

This is created in `src/infra/observability/otel.py`.

Examples:

- `copilotgtm-backend-api`
- `copilotgtm-backend-chat_worker`
- `copilotgtm-backend-chat_beat`

Important rule:

- worker and beat must not share the same OTel service identity

The shared Celery bootstrap resolves the service suffix from the active runtime app and current Celery mode so that:

- worker imports do not overwrite other worker identities
- beat exports do not collapse into worker service names

## Correlation Across API And Celery

Request-to-worker correlation depends on Celery headers.

### Dispatch side

Task clients call:

- `correlation_headers(...)`

This attaches `request_id` and any extra correlation fields to the outgoing Celery message headers.

### Execution side

The shared Celery bootstrap listens to:

- `task_prerun`
- `task_postrun`
- `task_failure`

At task start:

- headers are read from `task.request.headers`
- request context is restored
- Celery task metadata is stored in context

At task end:

- request/task context is cleared

Result:

- API logs and downstream worker logs can be queried with the same `request_id`
- one task can be traced using `celery_task_id`

## How To Add Logs Correctly

### Use `get_logger()`

Preferred:

```python
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)
```

### Use structured fields for identifiers at `INFO+`

Preferred:

```python
logger.info(
    "Deal summary created",
    extra={
        "event": "deal_summary_created",
        "organisation_id": str(organisation_id),
        "deal_id": str(deal_id),
        "job_id": str(job_id),
    },
)
```

Avoid:

```python
logger.info(f"Deal summary created for org={organisation_id} deal={deal_id} job={job_id}")
```

### Use the right log level

- `DEBUG`: hot-path success chatter, client init/shutdown, noisy diagnostics
- `INFO`: business milestones and operator-meaningful state changes
- `WARNING`: expected-but-notable issues, retries, 4xx-style failures
- `ERROR`: real failures that need investigation

### Prefer `logger.exception(...)` for failures

Preferred:

```python
try:
    close_redis_pool()
except Exception:
    logger.exception(
        "Failed to close Redis connection pool",
        extra={"event": "redis_pool_close_failed"},
    )
```

This preserves stack traces and keeps the message stable for queries.

## Worker Lifecycle Logging

Worker lifecycle logs should:

- be emitted from each worker's own `celery_app.py`
- include a stable `event`
- include `worker_component`
- ignore Celery lifecycle signals emitted for other apps

This guard matters because multiple worker app modules may be imported in one process during tests or management commands.

Preferred pattern:

```python
def _is_current_worker_signal(sender) -> bool:
    return getattr(getattr(sender, "app", None), "main", None) == app.main
```

## Known Query Notes

The current Grafana README assumes OTel/Loki queries primarily use `service_name`.

That is correct for OTLP-exported logs.

For stdout JSON logs or non-OTLP ingestion paths, `component` remains useful.

If a dashboard or query behaves strangely, first confirm which ingestion path you are querying:

- OTLP/Loki with OTel resource labels
- raw structured JSON logs from stdout/stderr

## Troubleshooting

### I cannot correlate API and worker logs

Check:

- the task was dispatched through a client using `correlation_headers(...)`
- the worker goes through `create_celery_app(...)`
- worker logs include both `request_id` and `celery_task_id`

### Beat logs are showing up under the worker service

Check:

- the process is started in beat mode
- the worker module uses `resolve_celery_component(...)`
- beat initialization is going through the shared Celery bootstrap

### Successful API requests are missing

Check:

- `ENV`
- `ACCESS_LOG_LEVEL`

Current default:

- `local` and `dev`: success access logs visible
- `prod`: success access logs suppressed

### Logs are too noisy

Check:

- whether the event belongs at `DEBUG` instead of `INFO`
- whether IDs are embedded in message text instead of structured fields
- whether a third-party logger needs to be added to `_reduce_noisy_third_party_logs()`

## Tests

Regression coverage for the logging runtime lives in:

- `tests/infra/test_otel_runtime.py`

This file currently verifies:

- JSON and local console formatting
- access-log behavior
- worker-vs-beat component resolution
- worker OTel init deferral
- multi-app Celery suffix correctness
- beat-specific OTel suffixing
- selected structured logging expectations

When changing logging behavior, update tests in that file in the same change.
