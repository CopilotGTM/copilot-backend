# Async Integration and Deal Workers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Keep the change in one branch/PR, but do not deploy it to production without fresh explicit approval.

**Goal:** Let integration and deal worker pods overlap independent I/O-heavy tasks on one asyncio event loop while preserving retry, timeout, idempotency, tenant isolation, and graceful-shutdown behavior.

**Architecture:** Keep Celery as the durable delivery and retry boundary. Reuse the chat worker's existing `threads -> run_async() -> one worker-owned asyncio loop` pattern for integration and deal workers; do not add a custom pool or dependency. Add cooperative coroutine deadlines because Celery's thread pool cannot enforce prefork soft/hard time limits, keep Salesforce CDC in its dedicated async process, and retain the existing `deal`/`deal_jobs` isolation.

**Tech Stack:** Python 3.12, asyncio, Celery 5.6.2, SQLAlchemy async/asyncpg, Redis, Kubernetes/Helm, KEDA, pytest.

---

## Decision summary

The task bodies are already mostly async: every currently registered integration task entry point inspected and all but one active deal task file enter async work through `run_async()`. They are not multiplexed inside a pod because integration and deal deployments still use Celery's default prefork pool. A plain pool switch is not safe: Celery documents that alternative pools can silently disable `soft_timeout` and `max_tasks_per_child`, and this repository has many tasks that rely on `SoftTimeLimitExceeded` for state cleanup or chain failure.

Use one implementation PR with these runtime shapes:

| Workload | Queue(s) | Runtime | Initial concurrency | Reason |
|---|---|---|---:|---|
| Integration sync | `integrations`, `integrations_priority` | Celery threads + shared asyncio loop | 4 | Provider/DB I/O overlaps; bounded first step for provider and PgBouncer pressure |
| Salesforce CDC | standalone `salesforce-cdc-worker` | Existing native async process | Existing bounded 7 streams | Long-lived streams already have leases and must not share Celery task slots |
| Deal orchestration/hydration | `deal` | Celery threads + shared asyncio loop | 4 | Mostly short database/storage/provider I/O |
| Deal insights/LLM | `deal_jobs` | Celery threads + shared asyncio loop | 2 | Preserve bulkhead; lower concurrency for memory and provider limits |

Do not merge `deal` and `deal_jobs`, raise all queues to ten slots, or use gevent/eventlet. Do not treat `run_async()` alone as concurrency.

## Non-negotiable invariants

1. A Celery message is acknowledged only after its coroutine reaches the same terminal outcome as today.
2. A task redelivered after process/pod loss is harmless: durable writes are idempotent, claimed, locked, or cursor-fenced before concurrency is enabled.
3. Provider calls have explicit connect/read/overall deadlines shorter than the task deadline.
4. No database session is held across provider, LLM, storage, broker publication, or retry sleeps.
5. Cancellation rolls back and closes open sessions; it never advances a sync cursor or marks a job successful.
6. One task's cancellation cannot cancel or close another task's coroutine, client, or database pool.
7. Tenant identity remains explicit in every query/session; no mutable global tenant state is introduced.
8. Graceful shutdown stops consumption, lets bounded tasks finish, cancels overdue coroutines, closes Redis/async DB resources, and then stops the shared loop.
9. `SoftTimeLimitExceeded`-dependent behavior is replaced by a pool-independent coroutine deadline before a worker is changed to threads.
10. Deployment changes are code/chart changes only. Dev/prod rollout and production mutation remain separately authorized operations.

## File map

**Shared runtime**

- Modify `src/infra/utils/async_runner.py`: deadline-aware submission, cancellation wait, loop health, active-coroutine accounting, shutdown behavior.
- Modify `src/workers/base/celery_base.py`: expose per-task deadline metadata and metrics without relying on process signals.
- Modify `tests/infra/test_async_runner.py`: concurrency, context isolation, timeout, cancellation, and shutdown regression tests.
- Modify `tests/infra/observability/test_celery_memory_observability.py`: retain per-task metrics in the threaded runtime.

**Integration worker**

- Modify `src/workers/integrations_worker/celery_app.py`: start/stop the shared loop.
- Modify integration task entry points under `src/workers/integrations_worker/tasks/`: replace `SoftTimeLimitExceeded`-only cleanup with pool-independent timeout handling and verify every `run_async()` call has an effective deadline.
- Modify provider files under `src/workers/integrations_worker/providers/` only where an external call lacks an explicit timeout or cancellation-safe client lifetime.
- Add `tests/workers/integrations_worker/test_async_runtime.py`: real overlap and timeout-state contracts.
- Extend existing cursor/lock/recovery tests rather than creating parallel abstractions.

**Deal worker**

- Modify `src/workers/deal_worker/celery_app.py`: start/stop the shared loop.
- Modify `src/workers/deal_worker/tasks/deal_asset_post_upload_task.py` and `src/service/crm/assets/post_upload_service.py`: make the lone sync-heavy entry path safe in the threaded worker while keeping CPU thumbnail work off the event loop.
- Modify deal task entry points under `src/workers/deal_worker/tasks/` where timeout/retry cleanup assumes prefork signals.
- Add `tests/workers/deal_worker/test_async_runtime.py`: overlap, queue bulkhead, redelivery, and shutdown contracts.

**Deployment and operational proof**

- Modify `charts/application/values.yaml`, `charts/application/values-dev.yaml`, and `charts/application/values-prod.yaml`: explicit thread pools and bounded initial concurrency.
- Modify `tests/charts/test_worker_cronjobs.py`: rendered command, queue isolation, pool, concurrency, and removal of unsupported child-recycling assertions.
- Add `scripts/worker_concurrency_smoke.py`: self-contained, repeatable integration/deal overlap and failure smoke check.
- Modify `src/infra/observability/metrics.py` only if the existing Celery metrics cannot express active coroutines, queue wait, deadline cancellation, and DB-pool wait.

## Task 1: Prove the current boundary before changing runtime

- [ ] Record the current task inventory and fail the audit if a newly registered integration/deal task bypasses the approved runtime.

Add an AST-based test to `tests/workers/test_async_worker_task_inventory.py` that imports the two Celery apps, enumerates registered tasks routed to `integrations`, `integrations_priority`, `deal`, or `deal_jobs`, and classifies each entry point as:

- `run_async()` coroutine bridge;
- explicitly approved synchronous thread-safe work (`deal_asset.post_upload` until Task 5); or
- explicitly isolated long-lived work (`integrations.run_salesforce_cdc_subscriber`), which must remain on prefork until Task 4 proves every producer has cut over to the standalone aggregate worker.

The assertion must print missing task names, routes, and source modules. Do not assert only file counts.

- [ ] Run the inventory test and confirm the pre-change classification.

Run:

```powershell
uv run pytest tests/workers/test_async_worker_task_inventory.py -q
```

Expected: PASS with every registered task classified; no silent fallback category.

- [ ] Capture a local baseline for two representative workflows per queue.

Use calendar/email or CRM record sync for integrations, contact hydration/CRM import for `deal`, and one LLM job for `deal_jobs`. Record task duration, queue wait, RSS delta, and final durable state. The baseline is comparison evidence, not a release gate by itself.

## Task 2: Make `run_async()` deadline- and cancellation-safe

- [ ] Write failing tests in `tests/infra/test_async_runner.py` for these exact behaviors:

1. Four caller threads enter four coroutines concurrently on the same event loop.
2. request/task `ContextVar` values are isolated between concurrent coroutines.
3. an explicit deadline cancels the submitted coroutine and raises `WorkerAsyncTimeoutError` only after its `finally` block runs;
4. a timed-out coroutine cannot continue and commit a simulated late write;
5. stopping the loop cancels active coroutines, waits a bounded grace period, and reports any coroutine that refuses cancellation;
6. one timeout does not cancel a sibling coroutine;
7. post-fork reset still discards inherited pools/loops.

- [ ] Implement the minimum shared runtime in `src/infra/utils/async_runner.py`.

Add:

```python
class WorkerAsyncTimeoutError(TimeoutError):
    """The worker coroutine exceeded its cooperative execution deadline."""


def run_async(
    coro: Coroutine[Any, Any, T],
    *,
    timeout_seconds: float | None = None,
) -> T:
    ...
```

For the shared loop path, use `run_coroutine_threadsafe`, wait with the resolved timeout, call `future.cancel()` on expiry, then wait a short bounded cancellation grace so async context managers can roll back/close. Raise `WorkerAsyncTimeoutError` only after cancellation completes. If cancellation does not complete, take the worker out of readiness and fail-stop the process so the late-ack message is redelivered; never return and acknowledge while the coroutine can still mutate state. Preserve the existing persistent-loop behavior for prefork/solo callers.

The timeout resolver should prefer an explicit `timeout_seconds`, then the current Celery task's soft limit, then the worker default. Keep a small cleanup reserve before the hard limit. Do not inspect private Celery pool internals.

- [ ] Add loop-health state.

Track last loop tick, active coroutine count, and oldest coroutine age in thread-safe read-only helpers. Do not build a new scheduler. The health data is needed so a blocked event loop is distinguishable from a healthy Celery parent heartbeat.

- [ ] Run focused shared-runtime tests.

```powershell
uv run pytest tests/infra/test_async_runner.py tests/workers/test_celery_base_requeue.py -q
uv run ruff check src/infra/utils/async_runner.py tests/infra/test_async_runner.py
```

Expected: all pass; no pending-task or un-awaited-coroutine warnings.

## Task 3: Decouple task timeout semantics from the Celery pool

- [ ] Add a single reusable timeout classification at the worker boundary.

Use `WorkerAsyncTimeoutError` as the pool-independent signal. At task boundaries that currently catch `SoftTimeLimitExceeded`, handle both through one tuple during migration:

```python
except (SoftTimeLimitExceeded, WorkerAsyncTimeoutError) as exc:
    # Preserve this task's existing failure/retry/history transition.
    ...
```

Do not invent a new retry policy. Preserve each task's existing terminal/retry behavior and error metadata.

- [ ] Audit every integration/deal task deadline.

For each registered task, confirm:

- effective coroutine timeout is less than its hard limit;
- HTTP/gRPC/LLM/storage operations have a smaller native timeout;
- async retry sleeps fit inside the overall deadline;
- cancellation cannot be swallowed by a broad `except Exception`;
- failure/history rows are written from a fresh session after provider work;
- cursor advancement occurs only after successful data commit.

Add parameterized contract tests that fail with the task name when these conditions are missing. Where source inspection is insufficient, add a focused behavioral test to the task's existing test module.

- [ ] Remove reliance on unsupported pool features from the threaded workers.

Once the runtime deadline tests pass, remove `SoftTimeLimitExceeded` as the only enforcement mechanism for these queues. Keep task decorator soft/hard values as documentation and compatibility for prefork execution, but tests must prove correctness through `WorkerAsyncTimeoutError`.

## Task 4: Enable safe integration-worker multiplexing

- [ ] Start and stop the shared event loop in `src/workers/integrations_worker/celery_app.py` using the same lifecycle as chat.

Call `start_shared_loop()` only for this app's `worker_ready` signal. On shutdown, close Redis resources and then stop the loop after task drain. Add signal-isolation tests so importing multiple Celery apps cannot start/stop the wrong loop.

- [ ] Preserve queue and lock semantics.

Keep `integrations_priority` and `integrations` on the same initial worker, but verify concurrency for the following shared-key cases:

- same integration calendar/email sync: one owner, other task defers via existing lock;
- same HubSpot/Salesforce record: idempotent upsert/history transition;
- webhook recovery: `FOR UPDATE SKIP LOCKED` leases one receipt once;
- token refresh: no lost token or stale overwrite;
- cursor sync: cursor persists only after all accepted records commit.

Extend the existing lock, cursor, phase-failure, and webhook recovery tests. Do not add a second lock framework.

- [ ] Keep Salesforce CDC out of this pool.

The current code still has legacy producers for `integrations.run_salesforce_cdc_subscriber`, so do not switch the normal integration deployment to threads while it consumes `integrations_streaming`. Prove aggregate-mode cutover for every producer, remove/disable the legacy publications, and only then remove `integrations_streaming` from the normal worker's consumed queues. Keep the standalone `salesforce-cdc-worker` command, lease, renewal, readiness, and fixed stream bound unchanged. If any legacy producer must remain, route `integrations_streaming` to a separate prefork deployment rather than occupy a thread slot.

- [ ] Add `tests/workers/integrations_worker/test_async_runtime.py`.

Use real caller threads and the shared loop with mocked provider/DB boundaries. Prove two different organisation/integration jobs overlap, while two jobs for the same lock key do not execute provider writes concurrently. Prove one provider timeout records the existing failure state and leaves the sibling task successful.

- [ ] Render the integration worker with an explicit pool.

Set base/dev/prod integration configuration to `pool: threads`, initial `concurrency: 4`, and remove `maxTasksPerChild` because Celery does not support that guarantee for the thread pool. Keep pod autoscaling limits unchanged for the first rollout.

Do not raise database pool environment values in this PR unless the smoke test proves pool timeout/queueing at four slots. Existing worker DB pools allow three concurrent connections per engine (`pool_size=1`, `max_overflow=2`); the task design should release sessions across external waits.

## Task 5: Enable safe deal-worker multiplexing

- [ ] Start and stop the shared event loop in `src/workers/deal_worker/celery_app.py` with the same signal-isolation and loop-health behavior as Task 4.

- [ ] Keep `deal` and `deal_jobs` as separate deployments.

Set:

```yaml
deal-worker:
  config:
    pool: threads
    concurrency: 4

deal-insights-worker:
  config:
    pool: threads
    concurrency: 2
```

The main queue must not consume `deal_jobs`, and the insight worker must not consume `deal`. This prevents LLM latency/memory from consuming interactive hydration slots.

- [ ] Make `deal_asset.post_upload` explicitly thread-safe.

This is the only active deal task file without a top-level `run_async()` bridge. Keep synchronous SQLAlchemy sessions local to the task thread, run Azure async operations through the shared loop, and ensure thumbnail CPU work never executes on the shared event-loop thread. If `generate_thumbnails()` contains CPU-bound image work, move only that blocking section to `asyncio.to_thread`; do not rewrite the asset service.

Add tests proving two assets use separate sessions, one failure does not close another asset's storage client, Redis publication occurs only after commit, and retry/redelivery does not duplicate terminal state.

- [ ] Verify durable deal-job claim/idempotency paths under overlap.

Cover at least:

- CRM CSV batch redelivery and continuation publication after commit;
- contact hydration's existing busy/duplicate claim;
- meeting summary/notification delivery claim and per-target idempotency;
- deal insight jobs transitioning once from pending/running to terminal;
- stale cleanup not reclaiming a currently active threaded coroutine.

Reuse existing `DealJobs` state, locks, and delivery ledgers. Add a claim/fence only where a concurrent test proves a missing invariant.

- [ ] Add `tests/workers/deal_worker/test_async_runtime.py`.

Prove four main-queue I/O jobs overlap, two insight jobs overlap, queue isolation remains exact, a timed-out job rolls back, and a sibling completes. Include a cancellation test during LLM/provider wait and a redelivery test after simulated process loss.

## Task 6: Make health, shutdown, and observability truthful

- [ ] Change threaded-worker liveness to include shared-loop health.

The existing local Celery heartbeat proves only that the worker parent is advancing. Add a small healthcheck mode that fails liveness when the shared asyncio loop has stopped ticking beyond a bounded threshold while active coroutines exist. Readiness should fail after preStop stops consumption or when the shared loop is unavailable.

- [ ] Preserve the current preStop drain sequence.

Rendered deployments must:

1. cancel queue consumers;
2. allow active tasks to finish within `terminationGracePeriodSeconds`;
3. cancel overdue coroutines and await cleanup;
4. close Redis and async DB pools;
5. stop the shared loop;
6. exit so unacknowledged late-ack messages can be redelivered.

Add a subprocess/worker smoke test; unit-testing signal handlers alone is insufficient.

- [ ] Emit bounded metrics with existing observability infrastructure.

Record by worker and queue, not organisation/provider IDs:

- active coroutine count;
- coroutine duration and deadline cancellations;
- loop lag;
- task retry/failure rate;
- queue wait and oldest message age;
- DB pool checkout wait/timeouts;
- provider 429/5xx/timeouts;
- RSS and event-loop thread CPU.

Do not add high-cardinality tenant, integration, deal, or task IDs to metric labels; retain them only in structured logs/traces.

## Task 7: Lock deployment manifests and configuration

- [ ] Extend `tests/charts/test_worker_cronjobs.py` before changing values.

Assertions must cover dev and prod rendered manifests:

- integration: `--pool=threads --concurrency=4`, queues exclude streaming after cutover, no `--max-tasks-per-child`;
- deal: `--pool=threads --concurrency=4 -Q deal`;
- deal insights: `--pool=threads --concurrency=2 -Q deal_jobs`;
- all three use shared-loop-aware liveness/readiness;
- graceful shutdown remains enabled and longer than the largest supported coroutine deadline plus cleanup reserve;
- KEDA min/max replicas and queue thresholds remain unchanged in the first rollout.

- [ ] Run chart verification.

```powershell
uv run pytest tests/charts/test_worker_cronjobs.py -q
helm dependency build charts/application
helm lint charts/application -f charts/application/values-dev.yaml
helm lint charts/application -f charts/application/values-prod.yaml
```

Expected: all tests/lints pass and the rendered worker commands match the table above.

## Task 8: Add a real Redis/Celery/PostgreSQL concurrency smoke test

- [ ] Add `scripts/worker_concurrency_smoke.py` with explicit modes:

```powershell
uv run python scripts/worker_concurrency_smoke.py --worker integrations --jobs 4
uv run python scripts/worker_concurrency_smoke.py --worker deal --jobs 4
uv run python scripts/worker_concurrency_smoke.py --worker deal-insights --jobs 2
```

The script must enqueue test-safe commands with unique correlation IDs, measure pickup/start/end times, and query durable terminal state. It must fail unless execution intervals overlap and every task reaches exactly one expected terminal state.

- [ ] Add failure modes to the smoke test.

Run these against disposable/local infrastructure:

- provider delay longer than coroutine deadline;
- worker `TERM` during provider wait;
- worker process/pod kill after durable commit but before ack;
- duplicate/redelivered task message;
- Redis publish interruption after DB commit;
- DB pool saturation;
- one poison task beside healthy tasks.

Expected: no duplicate durable rows, no cursor advancement before commit, no permanently `in_progress` job without recovery, healthy siblings complete, and unacknowledged work is redelivered or reclaimed by its existing recovery path.

## Task 9: Verification gate for the one-PR change

- [ ] Run focused suites serially first.

```powershell
uv run pytest tests/infra/test_async_runner.py tests/workers/test_async_worker_task_inventory.py tests/workers/integrations_worker tests/workers/deal_worker tests/charts/test_worker_cronjobs.py -q
uv run ruff check src/infra/utils/async_runner.py src/workers/integrations_worker src/workers/deal_worker tests/infra/test_async_runner.py tests/workers/integrations_worker tests/workers/deal_worker tests/charts/test_worker_cronjobs.py scripts/worker_concurrency_smoke.py
uv run mypy src/infra/utils/async_runner.py src/workers/integrations_worker src/workers/deal_worker
git diff --check
```

- [ ] Run the full repository suite using the repository's normal CI command.

If parallel pytest is nondeterministic, rerun the exact failure and then the full suite serially. Report focused and full results separately; do not equate a local mock-based test with real Redis/PostgreSQL/Celery proof.

- [ ] Run local real-infrastructure smokes from Task 8.

Acceptance thresholds:

- integration four-job wall time is materially below sequential time and all four finish;
- deal main four-job overlap and deal-insight two-job overlap are visible in timestamps;
- zero duplicate durable writes in kill/redelivery cases;
- zero stuck test jobs after recovery window;
- no event-loop mismatch, cross-loop asyncpg, pending-task, or un-awaited-coroutine errors;
- DB checkout wait remains below the selected task/provider deadline;
- no sustained RSS growth across repeated batches.

## Task 10: Dev canary and production decision gate

- [ ] Deploy to Dev only after explicit approval for that deployment.

Use a canary sequence even though the code is one PR:

1. integration worker at four threads;
2. main deal worker at four threads;
3. deal-insights worker at two threads.

For each stage, run burst, timeout, redelivery, and graceful-restart smokes before moving to the next. Observe at least one real scheduled integration cycle and one real deal workflow.

- [ ] Compare before/after evidence.

Require lower queue age/pickup latency without regression in failure rate, duplicate writes, provider throttling, DB pool timeouts, memory, or stale jobs. Roll back the affected deployment command/values if any invariant fails; the durable data model must make rollback safe.

- [ ] Stop before production.

Production rollout requires fresh explicit approval. Before asking, present Dev results, exact image/pipeline ID, rendered worker commands, concurrency, queue topology, failure-injection results, and rollback version. After approval, roll out integration, deal, then deal-insights with the same observation gates; do not combine rollout verification into a claim based only on pod readiness.

## Completion checklist

- [ ] Integration and deal tasks truly overlap I/O within a pod; this is proven by timestamps, not inferred from `async def`.
- [ ] Every task has a pool-independent deadline and provider-native timeout.
- [ ] Cancellation and shutdown cannot leave late mutations running.
- [ ] Redelivery tests prove idempotency/claim/fence behavior for high-use workflows.
- [ ] Salesforce CDC remains isolated in its native async worker.
- [ ] `deal` and `deal_jobs` remain separate bulkheads.
- [ ] Rendered Dev/Prod commands use the intended pool/concurrency and no unsupported recycling flag.
- [ ] Focused, full, chart, and real-infrastructure checks are reported separately.
- [ ] Dev canary evidence is captured.
- [ ] No production change occurs without fresh explicit approval.
