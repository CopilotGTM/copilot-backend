# AKS Logging Best Practices (Azure Monitor + Helm workers)

This repo deploys a FastAPI webservice + multiple Celery worker Deployments via Helm (`charts/application`). In AKS, the most reliable setup is:

`stdout/stderr (JSON logs) -> Azure Monitor Agent (Container Insights) -> Log Analytics (KQL) -> alerts/workbooks`.

## What to use (ranked)

- **Azure Monitor Container Insights (AMA)**: Default choice on AKS. Collects `stdout/stderr` for all containers and ships to a Log Analytics Workspace (LAW). Lowest operational burden.
- **OpenTelemetry (SDK + Collector) -> Azure Monitor / Application Insights**: Add when you want distributed tracing, metrics, and trace-to-log correlation. More setup, but a big observability win.
- **Fluent Bit / Fluentd DaemonSet**: Use when you need heavy filtering/routing (multiple sinks, drop noisy logs pre-ingest) or non-Azure destinations.
- **External SaaS (Datadog/Elastic/Splunk)**: Only if mandated org-wide; higher cost and extra agents.

## Logging principles (API + workers)

- **Write to `stdout/stderr` only** in containers (no file handlers).
- **Use single-line JSON** per log record (stack traces included as JSON fields, not multi-line text).
- **Always include correlation fields** so you can jump from an API request to the Celery work it triggered.
- **Keep prod levels sane**: `INFO` default, `WARNING` for retries/degraded, `ERROR` for failures with exception info. Avoid `DEBUG` in prod unless time-boxed.
- **Don't log secrets/PII** (tokens, auth headers, raw email bodies, customer documents). Redact aggressively.

## Request tracing (API -> workers)

This repo supports request-level correlation using `X-Request-Id`:

- FastAPI reads `X-Request-Id` / `X-Correlation-Id` (or generates one) and returns `X-Request-Id` on every response.
- When the API enqueues Celery tasks, it forwards `request_id` in task headers.
- Workers read that header and stamp `request_id` into all logs emitted via `get_logger(...)`.

Operationally: copy a single `X-Request-Id`, then query logs by `request_id` to see the full timeline across API + workers.

**Recommendation:** set `LOG_FORMAT=json` in your Kubernetes secret for the API + workers so KQL can `parse_json(LogMessage)` reliably.

More detail: `docs/Technical Documentation/request-tracing-and-flower.md:1`.

### Recommended JSON fields

Use consistent keys across FastAPI + Celery so KQL queries work everywhere:

- `service` (e.g., `api`, `worker-knowledge-upload`)
- `environment` (e.g., `prod`, `staging`)
- `version` / `commit_sha`
- `level`, `message`, `logger`
- `request_id` (from FastAPI middleware / `X-Request-Id`)
- `trace_id`, `span_id` (if using OpenTelemetry)
- `org_id`, `user_id` (only if not sensitive)
- `celery_task_id`, `celery_queue`, `job_id` (for workers)

## Azure / AKS: enable central logging (recommended path)

### 1) Enable Container Insights (logs) to Log Analytics

- Portal: `AKS -> Insights -> Enable` and select the target Log Analytics Workspace (LAW).
- CLI (example):

  ```bash
  az aks enable-addons --addons monitoring --resource-group <RG> --name <CLUSTER> --workspace-resource-id <LAW_RESOURCE_ID>
  ```

### 2) (Optional) Enable AKS control-plane diagnostic logs

Do this only if you need control-plane visibility (it increases ingestion cost).

- Portal: `AKS -> Diagnostic settings -> Send to Log Analytics`.
- Start with the categories you actually need (examples): `kube-apiserver`, `kube-scheduler`, `kube-controller-manager`, `cluster-autoscaler`, `guard`, `csi-*`.

### 3) Verify the agent is running

Depending on cluster version/config you may see `ama-logs-*` (AMA) or `omsagent-*` (legacy).

```bash
kubectl get pods -n kube-system | grep -E "ama-logs|omsagent"
```

```powershell
kubectl get pods -n kube-system | Select-String -Pattern "ama-logs|omsagent"
```

### 4) Where the data lands

In Log Analytics, you typically query:

- **Container stdout/stderr**: `ContainerLogV2` (preferred) and/or `ContainerLog` (legacy).
- **Kubernetes events**: `KubeEvents`.
- **Pod/container inventory (restarts/status)**: `KubePodInventory`, `KubeNodeInventory`.

## Day-2 operations: how to view logs quickly

### kubectl (fastest for “what is this pod doing now?”)

```bash
# API (all containers)
kubectl logs -n <namespace> deploy/<api-deployment> --all-containers --since=30m --tail=200 -f

# A worker type (all pods with that label)
kubectl logs -n <namespace> -l app.kubernetes.io/name=knowledge-upload-worker --all-containers --since=30m --tail=200 -f

# CrashLoopBackOff: get the previous container instance logs
kubectl logs -n <namespace> pod/<pod-name> --previous --tail=200
```

### Azure Monitor Logs (best for “show me everything across pods/workers”)

- Portal: `Azure Monitor -> Logs` (select your LAW) then run KQL queries (examples below).

## Kusto (KQL) snippets

### Sanity check: are logs arriving?

```kusto
ContainerLogV2
| where TimeGenerated > ago(10m)
| take 50
```

### Filter to your namespace (replace as needed)

```kusto
ContainerLogV2
| where TimeGenerated > ago(1h)
| where Namespace == "copilotgtm"
| project TimeGenerated, PodName, ContainerName, LogMessage
| order by TimeGenerated desc
```

### Parse JSON logs (recommended format)

```kusto
ContainerLogV2
| where TimeGenerated > ago(1h)
| where Namespace == "copilotgtm"
| extend j = parse_json(LogMessage)
| project TimeGenerated, PodName, ContainerName,
          level=tostring(j.level),
          request_id=tostring(j.request_id),
          celery_task_id=tostring(j.celery_task_id),
          message=tostring(j.message)
| order by TimeGenerated desc
```

### Errors grouped by workload (API vs workers)

```kusto
ContainerLogV2
| where TimeGenerated > ago(6h)
| where Namespace == "copilotgtm"
| extend j = parse_json(LogMessage)
| extend level = coalesce(tostring(j.level), "")
| where level in ("ERROR", "Error", "CRITICAL", "Critical") or LogMessage has "Traceback"
| summarize errors=count() by ContainerName
| order by errors desc
```

### Trace a single request across API + workers

```kusto
let rid = "<request-id>";
ContainerLogV2
| where TimeGenerated > ago(24h)
| where Namespace == "copilotgtm"
| extend j = parse_json(LogMessage)
| where tostring(j.request_id) == rid or LogMessage has rid
| project TimeGenerated, PodName, ContainerName, LogMessage
| order by TimeGenerated asc
```

### Celery: group failures by queue (works best if you log `celery_queue`)

```kusto
ContainerLogV2
| where TimeGenerated > ago(24h)
| where Namespace == "copilotgtm"
| extend j = parse_json(LogMessage)
| where tostring(j.celery_task_id) != "" and (tostring(j.level) in ("ERROR","CRITICAL") or LogMessage has "Traceback")
| summarize failures=count() by queue=tostring(j.celery_queue)
| order by failures desc
```

### Pod restarts and bad states (inventory tables)

```kusto
KubePodInventory
| where TimeGenerated > ago(24h)
| where Namespace == "copilotgtm"
| summarize arg_max(TimeGenerated, *) by PodName
| project TimeGenerated, PodName, PodStatus, PodRestartCount, ContainerName
| order by PodRestartCount desc
```

### Kubernetes events (scheduling failures, OOM, etc.)

```kusto
KubeEvents
| where TimeGenerated > ago(24h)
| where Namespace == "copilotgtm"
| where Reason in ("BackOff","CrashLoopBackOff","FailedScheduling","OOMKilled")
| project TimeGenerated, Name, Reason, Message
| order by TimeGenerated desc
```

## Flower (Celery monitoring UI)

Flower helps when you have many workers/queues and need a live view of:

- worker liveness + concurrency
- active/reserved/scheduled tasks
- task IDs + state transitions

### Enable via Helm

- Set `flower.enabled=true`
- Set `global.celery.enableEvents=true` (required for Flower task visibility)

### Access (recommended)

Keep it internal and use port-forward:

```bash
kubectl port-forward -n <namespace> svc/<release>-flower 5555:5555
```

Then open `http://localhost:5555`.

**Note:** Flower is task visibility, not CPU/memory. Use `kubectl top pods -n <namespace>` or Azure Monitor metrics for resource usage.

## Alerting recommendations (practical defaults)

- **Log alert**: error count in `ContainerLogV2` above threshold for 5–15 minutes (separate alerts for API vs workers).
- **KubeEvents alert**: `OOMKilled`, `FailedScheduling`, `CrashLoopBackOff`.
- **Restart spike alert**: `PodRestartCount` jumping for a workload.
- **Cost guardrail**: alert on daily ingestion volume or “noisy pod” producing too many lines.

## Cost control & retention

- Start with **app logs only** (Container Insights). Add control-plane logs only when you need them.
- Prefer **JSON + selective fields** over very chatty text logs.
- Use **LAW retention** (e.g., 30–60 days) and export cold logs to Blob/Event Hub if long-term retention is required.
- If ingestion is high, add **filters** at collection time (DCR) or a **Fluent Bit** pipeline to drop noise before it hits LAW.

## Repo-specific next steps (optional improvements)

- Set `LOG_FORMAT=json` (and `LOG_LEVEL=INFO`) in your Kubernetes secret so logs include `request_id`, `celery_task_id`, and `celery_queue` as JSON.
- Ensure Celery doesn't hijack root logging and emits structured records (e.g., `CELERYD_HIJACK_ROOT_LOGGER=False`).
- Ensure Helm values set consistent env vars like `LOG_LEVEL`, `LOG_FORMAT=json`, `ENVIRONMENT`, `COMMIT_SHA` across `webservice` and `worker` charts.

## Quick recommendation

Use **Azure Monitor Container Insights** for centralized logs today (lowest ops). Add **OpenTelemetry** next if you need request-to-worker correlation, traces, and metrics in one place.
