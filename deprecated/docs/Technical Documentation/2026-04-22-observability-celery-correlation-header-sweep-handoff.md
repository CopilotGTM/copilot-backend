# Deprecated Observability Celery Correlation Header Sweep Handoff

Date: `2026-04-22`
Worktree: `D:\CopilotGTM\copilot-backend-observability-hardening`
Branch: `codex/observability-hardening`
Primary problem: repo-wide sweep of raw Celery `.delay()` / `.apply_async()` dispatch paths that may be dropping observability correlation headers.

## Current Status

### Done already in this worktree

- Correlation-header propagation was applied across the production coordinator/fan-out sites:
  - `src/workers/chat_worker/tasks/reminder_tasks.py`
  - `src/workers/chat_worker/tasks/stale_job_cleanup_tasks.py`
  - `src/workers/deal_worker/services/meeting_insight_services/meeting_prep_service.py` (signature)
  - `src/workers/deal_worker/services/meeting_insight_services/meeting_summary_service.py` (chain signatures)
  - `src/workers/deal_worker/tasks/deal_insight_tasks/deal_memory_tasks.py`
  - `src/workers/deal_worker/tasks/deal_insight_tasks/risk_blocker_extraction_task.py`
  - `src/workers/deal_worker/tasks/deal_insight_tasks/stakeholder_research_tasks.py`
  - `src/workers/deal_worker/tasks/deal_mappers/calendar_matching_tasks.py`
  - `src/workers/deal_worker/tasks/deal_mappers/email_matching_tasks.py`
  - `src/workers/deal_worker/tasks/deal_mappers/stakeholder_tasks.py`
  - `src/workers/deal_worker/tasks/deal_mappers/timeline_sync_tasks.py`
  - `src/workers/deal_worker/tasks/stale_job_cleanup_tasks.py`
  - `src/workers/deal_worker/tasks/transcript_fetch_tasks.py`
  - `src/workers/deal_worker/utils/pipeline_dispatch.py` (per-signature inside the canvas)
  - `src/workers/knowledge_worker/tasks/stale_job_cleanup_tasks.py`
- `/internal/health/detailed` was fixed to preserve its intentional unhealthy `HTTPException` payload instead of swallowing it.
- Sentry was hardened:
  - default `SENTRY_SEND_DEFAULT_PII` changed from `true` to `false`
  - `init_sentry()` now runs at module import in `src/api/fastapi_app.py` so startup/import failures are captured
  - in-place scrubber added in `src/infra/observability/sentry.py` covering request headers/body/cookies/query_string, user IP, extras, contexts, and breadcrumb data
- OTLP metrics layer added:
  - `src/infra/observability/metrics.py` with lazy-bound instruments
  - HTTP request metrics emitted from `RequestContextMiddleware`
  - Celery task metrics emitted from `task_prerun` / `task_postrun` signal handlers
  - Metric provider bootstrap in `src/infra/observability/otel._setup_metrics`
  - SLI/SLO guidance and proposed alerts documented in `2026-04-22-observability-metrics-sli-slo-foundation.md`
- Targeted tests were added for metrics helpers, Sentry scrubbing, OTel init ordering, healthcheck payload preservation, Celery worker routing defaults, prerun request-id seeding, and one stakeholder correlation dispatch path.

### Not done yet

- Integrations worker fan-out sites (`all_orgs_task.py` variants for calendar/email/messaging) still need correlation headers.
- Signature/canvas regression tests beyond the simple `apply_async` paths.
- Grafana dashboards still read from Loki logs rather than the new metric families.
- Alert rules and SLO burn queries are not yet provisioned in-repo.
- End-to-end test execution and validation for the OTLP metrics pipeline against a real collector.

## Why This Problem Exists

The observability contract in this repo is:

1. Dispatch side must attach headers with `correlation_headers(...)`
2. Worker startup restores them with `load_from_headers(...)`

Relevant code:

- `src/infra/observability/context.py`
- `src/workers/base/celery_base.py`
- `src/infra/messaging/task_dispatcher.py`

The key function is:

```python
def correlation_headers(extra: Mapping[str, Any] | None = None) -> dict[str, str] | None:
    headers: dict[str, str] = {}
    request_id = get_request_id()
    if request_id:
        headers["request_id"] = request_id
    ...
    return headers or None
```

And the worker-side restore path is:

```python
@task_prerun.connect(weak=False)
def _task_prerun_handler(...):
    headers = getattr(getattr(task, "request", None), "headers", None) or {}
    load_from_headers(headers)
```

So if a task is dispatched with:

- `.delay(...)`
- `.apply_async(...)` without `headers=correlation_headers(...)`

then the child task loses the parent `request_id` and any other propagated correlation fields.

## Why It Matters

If correlation is dropped:

- API request to worker tracing breaks
- worker to worker lineage breaks
- Grafana and Loki investigations show partial flows
- child task failures look unrelated to parent workflows
- some pipelines are traceable while others are not

This codebase relies heavily on structured logs and correlation IDs instead of first-class metrics. Because of that, losing headers is a real observability bug, not a cosmetic inconsistency.

## Goal

Sweep the repo and classify every raw Celery dispatch site into one of these buckets:

1. `Must propagate correlation`
2. `May remain raw`
3. `Docs / examples only`

Then patch only the first bucket.

## Non-Goals

- Do not redesign Celery architecture
- Do not change task semantics
- Do not add new business logic
- Do not rewrite documentation-only examples unless explicitly wanted
- Do not run broad refactors outside observability propagation

## Good Reference Pattern

Existing good pattern:

File: `src/workers/integrations_worker/tasks/token_refresh.py`

```python
task = refresh_tokens_org_task.apply_async(
    kwargs={"organisation_id": str(org_id), "buffer_hours": buffer_hours},
    headers=correlation_headers(),
)
```

This is the baseline pattern for parent to child task propagation.

## Patch Rules

### Case 1: `.delay(...)`

Replace:

```python
some_task.delay(arg1, arg2)
```

with:

```python
some_task.apply_async(
    kwargs={"arg1_name": arg1, "arg2_name": arg2},
    headers=correlation_headers(),
)
```

If the call site currently relies on positional args, prefer converting to keyword args for clarity and stability.

### Case 2: `.apply_async(...)` already exists

Replace:

```python
some_task.apply_async(kwargs={...})
```

with:

```python
some_task.apply_async(
    kwargs={...},
    headers=correlation_headers(),
)
```

### Case 3: Celery signatures

If code uses a signature object:

```python
sig.apply_async()
```

investigate whether the signature should become:

```python
sig.set(headers=correlation_headers()).apply_async()
```

Only do this when the signature is a child task that should inherit current context. Do not blindly modify canvases without checking how the signature is built.

### Case 4: Celery chain / pipeline objects

If code does:

```python
pipeline.apply_async()
```

do not patch blindly. First determine:

- whether the whole pipeline is rooted in a current request/task context
- whether headers must be set on the root canvas only
- whether child signatures inside the canvas need explicit headers

These cases require careful review.

## Repo-Wide Raw Dispatch Inventory

Source query used:

```powershell
rg -n --hidden -S "\.delay\(|\.apply_async\(" src | sort
```

### Likely production code sites to classify

- `src/workers/chat_worker/tasks/reminder_tasks.py:49`
- `src/workers/chat_worker/tasks/stale_job_cleanup_tasks.py:76`
- `src/workers/deal_worker/services/meeting_insight_services/meeting_prep_service.py:503`
- `src/workers/deal_worker/tasks/deal_insight_tasks/deal_memory_tasks.py:283`
- `src/workers/deal_worker/tasks/deal_insight_tasks/stakeholder_research_tasks.py:285`
- `src/workers/deal_worker/tasks/deal_mappers/calendar_matching_tasks.py:352`
- `src/workers/deal_worker/tasks/deal_mappers/email_matching_tasks.py:427`
- `src/workers/deal_worker/tasks/deal_mappers/timeline_sync_tasks.py:144`
- `src/workers/deal_worker/tasks/stale_job_cleanup_tasks.py:103`
- `src/workers/deal_worker/tasks/transcript_fetch_tasks.py:228`
- `src/workers/deal_worker/utils/pipeline_dispatch.py:334`
- `src/workers/deal_worker/utils/pipeline_dispatch.py:356`
- `src/workers/integrations_worker/tasks/calendar/all_orgs_task.py:122`
- `src/workers/integrations_worker/tasks/email/all_orgs_task.py:122`
- `src/workers/integrations_worker/tasks/messaging/channels_all_orgs_task.py:125`
- `src/workers/integrations_worker/tasks/messaging/deal_messages_all_orgs_task.py:127`
- `src/workers/knowledge_worker/tasks/stale_job_cleanup_tasks.py:85`

### Already fixed in this worktree

- `src/workers/deal_worker/tasks/deal_insight_tasks/risk_blocker_extraction_task.py:220`
- `src/workers/deal_worker/tasks/deal_mappers/stakeholder_tasks.py:115`
- `src/workers/deal_worker/tasks/deal_mappers/stakeholder_tasks.py:119`
- `src/workers/deal_worker/tasks/deal_mappers/stakeholder_tasks.py:248`
- `src/workers/deal_worker/tasks/deal_mappers/stakeholder_tasks.py:252`
- `src/workers/deal_worker/tasks/deal_mappers/stakeholder_tasks.py:414`

### Docs / examples only

- `src/workers/deal_worker/README.md:152`
- `src/workers/integrations_worker/tasks/calendar/__init__.py:23`
- `src/workers/integrations_worker/tasks/calendar/__init__.py:26`
- `src/workers/integrations_worker/tasks/email/__init__.py:14`
- `src/workers/knowledge_worker/parsers/README.md:116`

These should not be the first implementation target unless the team specifically wants docs aligned too.

## Classification Logic

### Must propagate correlation

Use this bucket when:

- a task is dispatched from inside an API request path
- a task is dispatched from inside another task
- the child task is part of the same logical workflow
- operators would reasonably expect parent and child logs to share a `request_id`

Examples:

- org coordinator task dispatching per-org child tasks
- per-deal task dispatching follow-up tasks
- background worker dispatching downstream pipeline steps

### May remain raw

Use this bucket when:

- the task is intentionally a new root workflow
- the dispatcher is a periodic beat-style coordinator with no useful current `request_id`
- no upstream correlation exists or none is meaningful

Important:

Even for root workflows, adding `headers=correlation_headers()` is usually harmless because it returns `None` when nothing exists. So the practical default can still be “add it unless there is a reason not to.”

### Needs careful review

Use this bucket when:

- dispatch happens through Celery signatures
- dispatch happens through `chain`, `group`, `chord`, or composite pipelines
- dispatch code may already encode routing or headers elsewhere

This applies especially to:

- `src/workers/deal_worker/services/meeting_insight_services/meeting_prep_service.py`
- `src/workers/deal_worker/utils/pipeline_dispatch.py`

## Suggested Execution Order

1. Sweep simple raw `.apply_async(kwargs=...)` sites and add `headers=correlation_headers()`
2. Sweep simple `.delay(...)` sites and convert them to `apply_async(kwargs=..., headers=...)`
3. Review signature / canvas sites separately
4. Add regression tests for representative worker-to-worker propagation paths

## File-by-File Notes

### `src/workers/chat_worker/tasks/reminder_tasks.py`

- Check if `execute_reminder_task.apply_async(...)` is dispatched from another task context
- If yes, add `headers=correlation_headers()`

### `src/workers/chat_worker/tasks/stale_job_cleanup_tasks.py`

- Tiered worker fan-out likely should propagate correlation if called from a coordinator task
- Add `correlation_headers` import and headers on per-org dispatch

### `src/workers/deal_worker/tasks/deal_insight_tasks/deal_memory_tasks.py`

- Current `.delay(...)` likely dispatches per-org child jobs from a parent task
- Convert to `apply_async(kwargs=..., headers=correlation_headers())`

### `src/workers/deal_worker/tasks/deal_insight_tasks/stakeholder_research_tasks.py`

- Same pattern as deal memory
- Convert `.delay(...)` to header-aware `apply_async(...)`

### `src/workers/deal_worker/tasks/deal_mappers/calendar_matching_tasks.py`

- Per-org child task dispatch from coordinator
- Add `headers=correlation_headers()`

### `src/workers/deal_worker/tasks/deal_mappers/email_matching_tasks.py`

- Same as calendar matching
- Add `headers=correlation_headers()`

### `src/workers/deal_worker/tasks/deal_mappers/timeline_sync_tasks.py`

- Coordinator to child fan-out
- Add `headers=correlation_headers()`

### `src/workers/deal_worker/tasks/stale_job_cleanup_tasks.py`

- Coordinator to child fan-out
- Add `headers=correlation_headers()`

### `src/workers/deal_worker/tasks/transcript_fetch_tasks.py`

- Coordinator to org-level child fan-out
- Add `headers=correlation_headers()`

### `src/workers/integrations_worker/tasks/calendar/all_orgs_task.py`

- Same pattern as token refresh reference
- likely simple fix

### `src/workers/integrations_worker/tasks/email/all_orgs_task.py`

- Same pattern as token refresh reference
- likely simple fix

### `src/workers/integrations_worker/tasks/messaging/channels_all_orgs_task.py`

- Same pattern
- likely simple fix

### `src/workers/integrations_worker/tasks/messaging/deal_messages_all_orgs_task.py`

- Same pattern
- likely simple fix

### `src/workers/knowledge_worker/tasks/stale_job_cleanup_tasks.py`

- Coordinator to child fan-out
- likely simple fix

### `src/workers/deal_worker/services/meeting_insight_services/meeting_prep_service.py`

- This uses a signature object
- inspect how `sig` is built before changing anything
- safest likely outcome is `sig.set(headers=correlation_headers()).apply_async()`
- do not patch without confirming

### `src/workers/deal_worker/utils/pipeline_dispatch.py`

- This uses a pipeline / canvas
- inspect whether the pipeline already contains signatures with options
- this is the highest-risk observability patch in the sweep
- do not let a small model patch this file without a narrow, explicit review

## Acceptance Criteria

The sweep is complete when:

- every production raw dispatch site has been classified
- all simple coordinator and child-task dispatches use `correlation_headers()`
- signature and pipeline cases have explicit decisions, not guesswork
- docs-only examples are left alone or updated intentionally
- targeted tests cover at least one new simple coordinator path and one signature/pipeline path if changed

## Recommended Output Format For A Smaller LLM

If handing this to a smaller model, instruct it to return:

1. classified dispatch inventory
2. exact files it will patch
3. only the simple propagation fixes first
4. separate review notes for signature/pipeline sites

## Suggested Prompt For A Smaller LLM

Use the isolated worktree:
`D:\CopilotGTM\copilot-backend-observability-hardening`

Task:

- Read `src/infra/observability/context.py`, `src/workers/base/celery_base.py`, and `src/infra/messaging/task_dispatcher.py`
- Sweep the repo for raw Celery `.delay()` and `.apply_async()` calls under `src/`
- Classify each site as:
  - must propagate correlation
  - may remain raw
  - docs/examples only
- Patch only the simple production sites that should propagate correlation
- Use `headers=correlation_headers()` for existing `apply_async(...)`
- Replace simple `.delay(...)` calls with `apply_async(kwargs=..., headers=correlation_headers())`
- Do not patch `meeting_prep_service.py` or `pipeline_dispatch.py` unless you explicitly explain the Celery signature/canvas behavior first
- Add or update targeted tests only for the files you patch

Constraints:

- keep changes limited to observability propagation
- no broad refactors
- do not run tests unless explicitly asked

## Final Note

This document is intentionally operational rather than aspirational. It is meant to let another model or engineer continue the next observability slice inside the correct worktree with minimal ambiguity.
