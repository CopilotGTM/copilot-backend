# Request Tracing + Flower (API + Celery workers)

This document explains the changes made to improve production debugging when you have many workers and many concurrent requests.

## Why you see multiple middleware

In `src/api/fastapi_app.py`, the app uses multiple middleware because each one solves a different problem:

1) `CORSMiddleware`

- Purpose: lets browsers call your API from other origins (web apps on different domains).
- Handles: preflight `OPTIONS` requests and adds `Access-Control-*` headers.
- Without it: your frontend may fail in production due to CORS errors (even though the API is fine).

2) `RequestContextMiddleware`

- Purpose: assigns/propagates a **correlation id** (`X-Request-Id`) for every HTTP request.
- Outcome: you can take one request id and find **all related logs** across the API pod and the Celery worker pods.

Note: you may also see `SessionMiddleware` later in the file; that’s for cookie session behavior and is unrelated to request tracing.

## What was added for request tracing

### 1) Generate/propagate `X-Request-Id`

File: `src/api/middleware/request_context.py:1`

- Reads `X-Request-Id` or `X-Correlation-Id` from incoming requests.
- If missing, generates a new id.
- Stores it in a `ContextVar` and also sets `request.state.request_id`.
- Returns it to the client in the response header `X-Request-Id`.

### 2) Put `request_id` into logs automatically

Files:

- `src/infra/observability/context.py:1` (ContextVars + helpers)
- `src/infra/utils/logger.py:1` (log context filter + JSON formatter)

`get_logger(...)` now injects these fields into every log record (without changing call sites):

- `request_id`
- `celery_task_id`
- `celery_task_name`
- `celery_queue`

If you set `LOG_FORMAT=json`, logs are emitted as single-line JSON (recommended for Log Analytics / KQL).

### 3) Forward the `request_id` into Celery tasks

When the API enqueues tasks, it can now attach headers automatically:

- Client wrappers updated:
  - `src/workers/knowledge_worker/client.py:1`
  - `src/workers/chat_worker/client.py:1`
  - `src/workers/deal_worker/client.py:1`
  - `src/workers/integrations_worker/client.py:1`
  - `src/workers/organization_worker/client.py:1`
  - `src/workers/content_worker/client.py:1`

These wrappers now:

- attach `headers={"request_id": ...}` under the hood
- keep the `ContextVar` values when using thread pools (so correlation isn’t lost)

Some internal worker-to-worker task triggers were also updated to include headers:

- `src/service/integrations/nango_webhook_service.py:1`
- `src/workers/knowledge_worker/tasks/post_upload.py:1`
- `src/workers/integrations_worker/tasks/teams_sync_tasks.py:1`
- `src/workers/integrations_worker/services/dispatcher.py:1`

#### Important: commit before enqueue

If you enqueue a Celery task **before** committing the DB transaction that created the job row, the worker can start immediately and fail with:

- `RuntimeError: Job <id> not found`

This was fixed for chat by committing before enqueueing in `src/api/routers/chat/messages_handler.py:1` and adding a small retry safety net in `src/workers/chat_worker/tasks/message_processing.py:1`.

### 4) Load `request_id` inside workers for every task

File: `src/workers/base/celery_base.py:1`

All Celery apps created via `create_celery_app(...)` now register global Celery signal handlers:

- On task start: read task headers, set `request_id`, set task context (`celery_task_id`, `celery_task_name`, `celery_queue`)
- On task end: clear context so one task doesn’t “leak” into another

This is why worker logs can be filtered by `request_id` even when many tasks run concurrently.

## How to use this in production

### A) From a client request

1) Call the API.
2) Copy `X-Request-Id` from the response.
3) Search logs by that id (Log Analytics / kubectl).

### B) From a Celery task id

1) Find a failing `celery_task_id` in logs or Flower.
2) Search logs for that task id.
3) You will also see the `request_id` (if the task came from an API request).

## Flower (Celery monitoring UI)

Flower gives you a live view of workers and tasks (active/reserved/succeeded/failed), which is useful when you have many queues.

### How Flower + `request_id` work together
Flower is for **task visibility** (what is running, which worker picked it up, task ids, failures).
`request_id` is for **log correlation** (which API request caused which worker actions).

Typical workflow:
1) Use Flower to find a failing/slow task and copy its `task_id`.
2) Search logs for `celery_task_id=<task_id>` to see full stack traces and the associated `request_id` (if it came from an API request).
3) Or start from the API response `X-Request-Id` and search logs by `request_id` to see every related worker action.

Note: tasks triggered by Celery Beat/schedulers do not originate from an HTTP request, so `request_id` will be empty/`-` for those tasks (this is expected).

### Is Flower "internal" in our Helm charts?

Yes, by default it is internal:

- `Service` is `ClusterIP` (only reachable inside the cluster).
- `Ingress` is **disabled** by default (`flower.ingress.enabled=false`), so nothing is exposed publicly.

To keep it internal, **do not enable ingress**. Use port-forward when needed.

### What was added

An optional Helm subchart was added:

- Chart: `charts/application/charts/flower/templates/deployment.yaml:1`

### How to enable

You need **both**:

- `flower.enabled=true` (deploy Flower)
- `global.celery.enableEvents=true` (start workers with `celery -E` so Flower receives task events)

**Why events are required:** without `-E`, workers do not emit task lifecycle events, so Flower won't show task activity.

### How to access (recommended)

Keep Flower internal and port-forward:

```bash
kubectl port-forward -n <namespace> svc/<release>-flower 5555:5555
```

Open `http://localhost:5555`.

### Do I need the `flower` Python package in this repo?

Not for AKS/Helm: Flower runs as a separate container (`mher/flower`) and already includes the Flower binary.

If you want to run Flower locally from your Python environment (not via Docker), then installing the `flower` package is useful.

Note: the container image runs Flower as `celery flower` (there is no standalone `flower` binary in recent images).

## Recommended configuration (AKS)

- Set `LOG_FORMAT=json` and `LOG_LEVEL=INFO` in your Kubernetes secret (API + workers).
- Keep Flower disabled by default and enable it only when you need live task visibility (or lock it behind ingress auth).

## What should you do now?

### Option 1 (recommended): Deploy Flower temporarily (internal only)

1) Create a small override file (example `values.flower.yaml`):

   ```yaml
   global:
     celery:
       enableEvents: true
   flower:
     enabled: true
     ingress:
       enabled: false
   ```

2) Deploy/upgrade:

   ```bash
   helm upgrade --install <release> ./charts/application -n <namespace> -f values.flower.yaml
   ```

3) Port-forward:

   ```bash
   kubectl port-forward -n <namespace> svc/<release>-flower 5555:5555
   ```

4) Open `http://localhost:5555`.
5) When done, disable events + Flower again (to reduce Redis traffic):
   - set `global.celery.enableEvents=false`
   - set `flower.enabled=false`

### Option 2: Keep Flower always-on (only if you secure it)

If you want Flower permanently:

- Keep `Service` as `ClusterIP`
- Expose only via a private ingress/VPN, and enable auth (or protect it with your ingress auth layer).

### Before deploying: confirm Redis env vars exist

Flower uses the same Kubernetes secret (`global.secretName`) as the workers.
Make sure it includes `REDIS_HOST`, `REDIS_PORT`, and (if used) `REDIS_PASSWORD`.

For KQL queries and Azure Monitor guidance, see `docs/Technical Documentation/aks-logging-best-practices.md:1`.

Local operator workflow (deploy to AKS, view logs locally): `docs/Technical Documentation/aks-deploy-and-view-logs-from-local.md:1`.
