# Logging Remediation Plan

This document defines the step-by-step plan to fix logging across local development, dev observability, and production.

The main goal is not to "change JSON to text" or vice versa. The real goal is:

- make local logs readable during debugging
- make Grafana/Loki logs queryable and low-noise
- make failures stand out from routine success chatter
- keep one consistent logging policy across API and workers

## Current Problems

From the current implementation, the main issues are:

1. Local console logs hide useful structured fields.
The code already emits fields like `http_method`, `http_path`, `http_status_code`, `duration_ms`, `request_id`, and task context, but the local console formatter mostly prints only the message text.

2. Hot-path success events are logged at `INFO`.
Examples include token verification success, request completion for normal 2xx traffic, client initialization, and other plumbing-level events.

3. Expected user/auth failures are too loud.
Invalid JWTs, expired refresh tokens, and similar expected 4xx conditions are often logged as `ERROR`, which pollutes real failure detection.

4. Many important IDs live only inside message strings.
This makes Grafana/Loki filtering and grouping harder than it should be.

5. Local and remote observability behavior is not clearly separated.
`ENV=local` gives human-readable console logs, but local can still export OTLP logs to Grafana. That is acceptable, but the intent should be explicit and the noise profile should be controlled.

## Target State

After this remediation:

- local Docker logs should be readable without opening JSON blobs
- dev and prod logs should remain structured and query-friendly
- repeated success chatter should move to `DEBUG`
- `INFO` should represent business milestones or operationally meaningful state changes
- `WARNING` should represent transient issues or expected-but-notable failures
- `ERROR` should be reserved for actual failures that need investigation
- common investigations should be easy:
  - find all logs for one request
  - find all logs for one Celery job
  - find authentication failures
  - find slow requests
  - find tenant secret failures

## Logging Policy

This policy is the frozen baseline for the remediation. Step 1 is approval of this policy, not discovery of a new one.

This policy should apply across API, workers, auth, integrations, storage, and infra code.

### DEBUG

Use `DEBUG` for frequent success-path diagnostic events:

- token verification success
- client initialization and shutdown
- cache hits
- credential refresh success details
- per-call external client success chatter
- tool call start/end chatter when it happens frequently

`DEBUG` logs may embed IDs in message text if needed. They do not need full structured-field normalization.

### INFO

Use `INFO` for operational or business milestones:

- app startup and shutdown
- worker startup
- job dispatched
- job completed
- integration connected or disconnected
- scheduled job started or completed
- significant state change a human would actually care about during normal operations

All `INFO` and above logs that include business or operational identifiers must emit those identifiers as structured fields, not only inside free-text messages.

### WARNING

Use `WARNING` for notable but non-fatal conditions:

- invalid token or expired refresh token
- 4xx request outcomes that are expected but worth tracking
- retryable external dependency issues
- degraded external service conditions
- stale job recovery
- missing-but-optional data

### ERROR

Use `ERROR` for real failures:

- request failed unexpectedly
- job failed
- dependency unavailable after retries
- database/session failures
- unhandled exceptions
- security-sensitive failures that need attention

## Canonical Structured Fields

These field names should be used consistently in structured logs.

- `event`
- `request_id`
- `organisation_id`
- `user_id`
- `deal_id`
- `stakeholder_id`
- `job_id`
- `integration_id`
- `celery_task_id`
- `celery_task_name`
- `celery_queue`
- `http_method`
- `http_path`
- `http_status_code`
- `duration_ms`

Use `organisation_id`, not `org_id`, in structured output to stay consistent with the domain model naming already used across the codebase.

## Environment Strategy

We should explicitly support three operating modes.

### Local

Primary goal: fast human debugging.

- console output should be human-readable
- key structured fields should be rendered inline
- successful request timelines should remain visible without enabling full `DEBUG`
- repetitive auth success logs should not appear at `INFO`
- local does not export OTLP logs by default
- local OTLP export should be opt-in via explicit config, not an accidental side effect of `.env`

### Dev

Primary goal: shared observability and debugging in Grafana/Loki.

- logs should be structured JSON
- field names should be stable
- request and task correlation must be preserved
- noise should be lower than local debug mode

### Production

Primary goal: reliable operations and incident response.

- structured JSON only
- no hot-path success chatter at `INFO`
- meaningful fields for filtering, aggregation, and alerting
- log volume should stay proportional to useful signal

## Safety Note

All log-level changes should be safely reversible.

Operational env vars to support this plan:

- `LOG_LEVEL`: global verbosity
- `ACCESS_LOG_LEVEL`: access log verbosity
- `OTEL_ENABLED`: local OTLP export opt-in
- `SLOW_REQUEST_THRESHOLD_MS`: slow-request promotion threshold, if adopted in this phase

If a level change accidentally hides something important in production, operators must be able to restore verbosity without code changes.

## Step-by-Step Resolution Plan

We should execute the cleanup in the order below.

### Step 1: Approve the policy and align OTLP config

Review and approve the policy defined in this document, including the local/dev/prod environment strategy and the OTLP decision.

Decision to lock in:

- local does not export OTLP logs by default
- local OTLP export is opt-in
- `INFO+` logs must use structured fields for identifiers
- `DEBUG` logs may remain lighter-weight

Concrete config-alignment checkpoint:

- verify that local config does not opt into OTLP by default
- verify that `.env.example`, `.env`, and local Docker config do not effectively set OTLP export on by default for local workflows
- if local OTLP export exists today, document the explicit opt-in mechanism and remove any default-on behavior

Acceptance criteria:

- this document is approved as the implementation baseline
- OTLP behavior is agreed in writing before code changes begin
- local config matches the agreed OTLP policy

### Step 2: Define target investigation queries

Before changing structured fields, write the concrete Grafana/Loki queries the logging system must support. The current Step 2 implementation artifact is `docs/Technical Documentation/logging-investigation-queries.md`.

Initial target queries:

1. find API failures
   Example: `{job="api"} | json | http_status_code >= 500`
2. find slow requests
   Example: `{job="api"} | json | duration_ms > 2000`
3. find auth failures
   Example: `{job="api"} | json | event="auth_failure"`
4. trace a request
   Example: `{job="api"} | json | request_id="<id>"`
5. trace a Celery job
   Example: `{job="worker"} | json | celery_task_id="<id>"`
6. find tenant secret failures
   Example: `{job="api"} | json | event="secret_fetch_failed"`
   Fallback if event normalization is not yet implemented: `{job="api"} | json | logger="azure_key_vault_provider" | level="ERROR"`

Acceptance criteria:

- the required investigations are written down before field normalization starts
- the field set for Step 6 is driven by actual queries, not guesswork

### Step 3: Improve local readability

Update the local console formatter so it exposes important fields inline instead of hiding them in structured payloads.

Rendering rule:

- render the human message first
- append all non-empty relevant extra fields as `key=value` pairs
- do not print empty fields
- keep the output to a compact single line whenever possible

Fields to prioritize:

- `request_id`
- `http_method`
- `http_path`
- `http_status_code`
- `duration_ms`
- `organisation_id`
- `job_id`
- `celery_task_name`
- `celery_task_id`

Example formatter target:

Before:

```text
14:32:01.123 INFO    [request_context] (P1234 a1b2c3d4) HTTP request completed
```

After:

```text
14:32:01.123 INFO    [access] (P1234 a1b2c3d4) HTTP request completed method=GET path=/api/deals status=200 duration_ms=42
```

Acceptance criteria:

- local request logs are readable without opening Grafana
- one line should be enough to understand what request happened
- the formatter handles both access logs and occasional business/job logs without special casing every logger

### Step 4: Reclassify access and auth log levels

This step is level cleanup, not broad refactoring.

#### Access logs

Replace the vague "2xx to DEBUG or selectively INFO" rule with an explicit policy.

Recommended direction:

- `ACCESS_LOG_LEVEL` controls successful request visibility independently of global app verbosity
- default `ACCESS_LOG_LEVEL`:
  - local: `INFO`
  - dev: `WARNING`
  - prod: `WARNING`
- quiet/health endpoints: `DEBUG`
- 4xx requests: `WARNING`
- 5xx requests: `ERROR`
- successful requests should remain visible in local development in compact form
- slow-request promotion is deferred to a future iteration unless a concrete threshold is added as part of implementation

Primary file:

- `src/api/middleware/request_context.py`

#### Auth logs

Demote hot-path success logs and correctly classify expected auth failures.

Primary files:

- `src/infra/auth/supabase_user_client.py`
  - success at `verify_token`: line near current `logger.info("Token verified successfully")`
  - failure at `verify_token`: line near current `logger.error(...)`
  - failure at `refresh_access_token`: line near current `logger.error(...)`
- related auth wrapper:
  - `src/infra/auth/user_auth.py`

Acceptance criteria:

- normal login and authenticated page flows no longer flood the console with repetitive auth chatter
- invalid or expired tokens remain visible without looking like infrastructure outages
- successful request timelines remain usable locally without enabling full debug mode

### Step 5: Clean up client and dependency noise

Review log levels for high-volume infra/client code and move lifecycle chatter to `DEBUG` while keeping actionable issues at `WARNING` or `ERROR`.

Concrete checklist:

- Nango client lifecycle and routine success chatter
  - `src/service/integrations/nango_client.py`
  - examples currently around client init and close
- Azure Key Vault auth strategy and client creation chatter
  - `src/infra/secrets/azure_key_vault.py`
  - examples currently around auth strategy selection and client creation
- Redis client lifecycle logs
  - `src/infra/messaging/redis_client.py`
  - `src/infra/messaging/redis_streams.py`
- Celery bootstrap and worker lifecycle logs
  - `src/workers/base/celery_base.py`
  - all worker app entrypoints under `src/workers/*_worker/celery_app.py`
  - client-side queueing helpers such as `src/workers/deal_worker/client.py`
- OpenTelemetry initialization chatter
  - `src/infra/observability/otel.py`

The rule is:

- lifecycle chatter goes to `DEBUG`
- retries and transient dependency issues stay at `WARNING`
- actual inability to serve the request/job stays at `ERROR`

Acceptance criteria:

- infra/client logs do not dominate the request timeline
- real dependency failures are easy to spot

### Step 6: Standardize structured fields for `INFO+` logs

For logs at `INFO`, `WARNING`, and `ERROR`, stop relying only on free-text messages when identifiers matter.

Scope boundary:

- only `INFO+` logs must be normalized for identifiers
- `DEBUG` logs can remain message-oriented unless a specific query needs them

Migration rule:

- use structured fields as the source of truth for identifiers
- do not duplicate the same identifiers in both the message text and structured fields unless a specific exception is justified
- prefer generic human messages plus structured fields, because the local console formatter will render those fields inline anyway

Preferred pattern:

```python
logger.info(
    "Job dispatched",
    extra={"job_id": job_id, "organisation_id": organisation_id},
)
```

Apply the canonical field names from this document.

Initial target events include:

- job dispatch and completion
- auth failures
- integration actions
- significant external dependency failures
- access logs

Acceptance criteria:

- Grafana/Loki queries do not require brittle text matching for key investigations
- business and operational events can be filtered by IDs directly
- field names are consistent across API and workers

### Step 7: Add focused tests

Add or extend tests for these specific behaviors:

1. `_ConsoleFormatter` renders extra fields inline
2. `_JsonFormatter` preserves extra fields in output
3. access log middleware uses the correct level per status code and access policy
4. noisy third-party loggers are suppressed to `WARNING`

Acceptance criteria:

- the agreed formatter behavior is locked in
- the level policy for access logs is locked in
- noisy dependency logger suppression is protected from regressions

## Suggested Execution Order

To get value quickly, implement in this order:

1. Step 1: approve policy and align OTLP config
2. Step 2: define target Grafana/Loki queries
3. Step 3: improve local readability
4. Step 4: reclassify access and auth log levels
5. Step 5: clean up dependency noise
6. Step 6: standardize structured fields for `INFO+` logs
7. Step 7: add focused tests

## First Implementation Slice

The first code change should be intentionally small and high-value:

- update the local console formatter to render extra fields inline
- keep successful request timelines readable locally
- reduce access-log noise without hiding all 2xx traffic
- demote token verification success logs

That will immediately improve local debugging while keeping the rest of the observability stack intact.

## Out of Scope

This plan does not require:

- replacing Python logging with another logging library
- removing JSON logs from dev or production
- redesigning Grafana dashboards before the log data is cleaned up
- changing tracing architecture unless log correlation is found to be broken
- normalizing every single `DEBUG` log in the codebase

## Definition of Done

The logging remediation is complete when:

- local logs are readable during normal debugging
- repeated success chatter no longer dominates `INFO`
- failures are clearly visible
- request and task correlation works end-to-end
- Grafana/Loki queries can use structured fields instead of message parsing
- tests protect the agreed behavior
