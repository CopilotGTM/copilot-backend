# Dev AKS API and Worker Resilience Plan

Date: 2026-07-04

## Question

Dev Kubernetes has shown repeated backend pod replacement during July 3 and July 4. The concern is that queued jobs start Celery workers, consume memory, and then the API also becomes unavailable. The API must not go down just because workers scale, restart, or wait for a second node.

This document started as diagnostic and planning only. It was reviewed and implemented in Helm for Dev and Prod on 2026-07-04.

## Current Evidence

Live checks were run against `copilot-gtm-aks-dev`, not the active default context, because the default context was `copilot-gtm-aks-prod`.

Current Dev state:

- One node: `Standard_D2as_v6`, 2 vCPU, about 6.8 GiB allocatable memory.
- One API pod: `copilot-backend`, `1/1` available.
- One always-on chat worker and one chat beat pod.
- Other workers are KEDA scale-to-zero: deal, integrations, knowledge, organization.
- Current node usage was about `875m` CPU and `4569Mi` memory.
- Node requested memory was about `3316Mi`, but configured memory limits were overcommitted at about `324%` of allocatable memory.
- During the current rollout window, Kubernetes emitted `Insufficient memory` for `copilot-backend-integrations-worker`.
- The API HPA temporarily could not compute CPU metrics while the target pod was unready.
- Helm history shows many Dev releases across July 3 and July 4, including a rollback on July 3 and a deployment on July 4.

Repo evidence:

- `charts/application/values-dev.yaml` sets API `replicaCount: 1`, HPA `minReplicas: 1`, `maxReplicas: 2`, request `25m` CPU / `768Mi` memory, and limit `500m` CPU / `1536Mi` memory.
- `charts/application/values-prod.yaml` sets API `replicaCount: 2`, HPA `minReplicas: 2`, `maxReplicas: 5`, request `500m` CPU / `1Gi` memory, and limit `1500m` CPU / `6Gi` memory.
- The API deployment has startup, readiness, and liveness probes on `/health`.
- The public `/health` endpoint is intentionally lightweight and returns a static healthy response.
- Worker deployments are separate from the API deployment, use KEDA Redis queue triggers, and have `maxUnavailable: 0` for worker rollouts.
- Worker PDBs exist with `minAvailable: 0`. There is no API PDB in the webservice chart.

## Root Cause

The issue is not that a Celery worker crash directly restarts the API container. API and workers are separate Kubernetes Deployments.

The real risk is shared cluster capacity and single-replica API availability:

1. Dev currently has one API replica. During a rollout, readiness failure, liveness restart, node drain, or eviction, there is no second ready API pod to keep service endpoints populated.
2. Dev currently has one node. When KEDA activates workers, Kubernetes may have nowhere to schedule them immediately. If existing pods and rollout surge pods are present at the same time, even a request-sized worker can fail scheduling.
3. Requests are low but limits are high. That lets many pods fit on paper, but real memory spikes can still create node pressure. Under node pressure, Kubernetes protects only according to QoS, priority, and requests; it does not know that the API is more important unless we configure it.
4. The API rollout strategy allows `maxUnavailable: 1`. With only one Dev API replica, a rollout can legally drop available API capacity to zero during the update.
5. HPA cannot save availability during cold start or unready periods. HPA acts after metrics exist, and metrics were unavailable while pods were unready.

So the production-grade answer is capacity isolation and availability policy, not retrying jobs inside the API path.

## What Should Happen

Workers are allowed to restart. Celery and Redis should preserve queued work and retry or re-deliver according to task settings.

The API should remain available when:

- A worker pod OOMs or is killed.
- KEDA scales a worker from zero.
- A worker cannot schedule until another node appears.
- A deployment rolls out a new image.
- One node is temporarily unavailable.

That requires at least one ready API replica to remain schedulable and protected at all times.

## Recommended Changes

### 1. Make Dev API production-shaped

Implemented Dev API availability settings:

- `api.replicaCount: 1`.
- `api.autoscaling.minReplicas: 1`.
- `api.autoscaling.maxReplicas: 2`.
- API rollout `maxUnavailable: 0`.
- Dev API PodDisruptionBudget disabled to avoid blocking node drain and one-node cost recovery.
- Prod API PodDisruptionBudget with `minAvailable: 1`.

Why: Dev is cost-sensitive and must be able to scale down to one node. With one API replica, `maxUnavailable: 0` means a rollout creates the new API pod first and keeps the old API pod serving until the replacement is ready. If there is not enough room, the replacement pod waits Pending instead of intentionally dropping the only API endpoint.

Risk: one API replica cannot protect against full node loss. That is the tradeoff for one-node Dev cost. Prod keeps two API replicas.

### 2. Keep API schedulable before workers

Implemented a higher API priority class and lower worker priority class, then assigned them in Helm.

Implemented shape:

- `copilot-api-critical`: value `100000`, `preemptionPolicy: PreemptLowerPriority`.
- `copilot-worker`: value `1000`, `preemptionPolicy: Never`.
- API Deployment uses `priorityClassName: copilot-api-critical`.
- API startup/migration hook uses `priorityClassName: copilot-api-critical`.
- Chat worker Deployment uses `priorityClassName: copilot-api-critical`.
- Chat beat, background worker Deployments, and CronJobs use `priorityClassName: copilot-worker`.

Why: when a node is tight, Kubernetes should prefer the API over optional background capacity. Without priority, worker and API pods compete as peers.

Risk: API preemption can interrupt worker jobs. That is acceptable only if Celery task acknowledgement/retry behavior is correct for each queue. Worker preemption is disabled so workers do not preempt unrelated lower-priority pods.

### 3. Reserve real node capacity for the API

Dev should not run the API on a node where all worker surge combinations can consume the same memory.

Implemented chart behavior:

- API `topologySpreadConstraints` prefer spreading across `kubernetes.io/hostname`.
- `whenUnsatisfiable: ScheduleAnyway`, so Dev can still run on one node while spreading automatically when a second node exists.

Remaining infrastructure target:

- Node pool minimum: 2 nodes.
- Keep KEDA workers scale-to-zero if cost matters, but do not count scale-to-zero workers as API availability capacity.

Better target:

- Small always-on system/API node pool for API, gateway, KEDA, metrics, DNS.
- Separate worker node pool for memory-heavy or bursty workers.
- Optional taints/tolerations so heavy workers do not land on the API/system pool.

Why: autoscaler startup is too late for request availability. It can add capacity after pending pods appear, but the API must already have enough protected capacity.

Risk: higher Dev cost. This is the cost of making Dev behave like production for availability.

### 4. Right-size requests and limits

Keep Dev resource requests low only where the blast radius is acceptable. For API, use requests that reflect real baseline plus headroom.

Implemented Dev values:

- API request remains `25m` CPU and `768Mi` memory.
- API limit remains `500m` CPU and `1536Mi` memory.
- Integrations worker request: keep high enough to avoid OOM churn, but place it on worker capacity.
- Continue `concurrency: 1` for memory-heavy workers in Dev.

Why: Dev's explicit cost goal is one-node operation. Raising the API request and running two API replicas reserves too much node memory for Dev. Prod keeps stronger API capacity.

Risk: increasing requests can expose insufficient node capacity. That is useful; it fails at scheduling instead of failing later under memory pressure.

### 5. Separate API health from deep dependency health

Keep `/health` lightweight for liveness. Add or document distinct probes:

- Liveness: "process can serve an HTTP response."
- Readiness: "process is initialized and can accept traffic."
- Internal detailed health: dependency visibility only, not a liveness killer.

The current public `/health` is already lightweight, which is correct for liveness. Do not add database, Redis, or worker dependency checks to liveness.

Why: a slow DB or Redis should not cause Kubernetes to kill the API process. It should remove traffic only when the API cannot serve requests safely.

Risk: readiness that is too strict can still remove all API endpoints. If dependency readiness is added, it must be staged carefully with two API replicas and alerts.

### 6. Preserve queued work when workers die

Before relying on worker preemption or OOM recovery, audit Celery task settings for the queues that matter:

- `acks_late`
- retry policy
- idempotency keys
- time limits
- visibility timeout / broker redelivery behavior

Why: Kubernetes can restart a pod, but it cannot make a task idempotent. The worker recovery guarantee belongs in Celery/task design.

Risk: enabling late acknowledgement on non-idempotent tasks can duplicate side effects. Audit first.

### 7. Add an alert and dashboard for the actual failure mode

Add alerts for:

- API available replicas below 1.
- API endpoints empty.
- API restarts.
- Pod OOMKilled / exit code 137.
- Worker pods pending due to `Insufficient memory`.
- Node memory pressure.
- KEDA activation/deactivation rate per worker.

Why: the problem is not visible enough from health charts if they only show app-level health. The chart needs Kubernetes availability signals.

## Change Order

1. Render Helm locally with the new API PDB, priority class hooks, and Dev values.
2. Apply only the Dev API availability changes first: two API replicas, min replicas two, API PDB, rollout `maxUnavailable: 0`.
3. Verify during a deployment that `kubectl get endpoints copilot-backend -n copilotgtm` never becomes empty.
4. Add node capacity or a second Dev node before turning on more always-on workers.
5. Add worker priority classes and optional worker node-pool isolation.
6. Audit Celery acknowledgement/retry behavior for high-risk queues.
7. Add alerts/dashboards.

## Implementation Review Notes

The review found these loopholes before and during implementation:

- Hard anti-affinity or `DoNotSchedule` topology spread would make Dev fragile while it has only one node. The chart now uses soft `ScheduleAnyway` spread.
- Helm's `default` function treats numeric `0` as empty. The API deployment template now uses `hasKey` so explicit `maxUnavailable: 0` renders as `0`, not `1`.
- A worker PriorityClass with normal preemption could let workers preempt unrelated lower-priority pods. Live and background worker priority now use `preemptionPolicy: Never`.
- Dev two-replica API plus a larger request would fight the one-node cost target. Dev now stays one API replica and original API memory sizing; Prod keeps two API replicas.
- Dev API PDB with one API replica would block voluntary eviction and can prevent autoscaler node drain. Dev disables API PDB; Prod keeps it.
- The API startup hook initially had no priority, so a deployment could stall before rollout if background workers occupied schedulable capacity. The hook now uses API priority.
- PriorityClass resources are cluster-scoped. Live Dev and Prod checks showed the new priority class names do not already exist, so Helm will create them instead of adopting conflicting resources.
- Azure DevOps Dev and Prod backend pipelines pass `values-dev.yaml` / `values-prod.yaml` into Helm directly, so these changes are picked up by the normal Dev/Prod deployment path. The local helper has an existing `--reuse-values` path; that path was not changed in this implementation.
- API PDB is useful even if a future values file accidentally renders one API replica. It will block voluntary disruption rather than silently allowing the only API pod to be drained.

## Runtime Flow

Dev steady state:

1. One API pod runs with the highest priority.
2. Chat/realtime workers run with live-worker priority.
3. Background workers and CronJobs run with lower worker priority.
4. KEDA can scale background workers from zero when queues have work.

If a background worker needs more memory than its container limit, only that worker container is OOMKilled and restarted. The API pod does not restart because the worker exceeded its own limit.

If a new worker cannot fit on the current one-node Dev cluster, Kubernetes leaves it Pending until capacity is available or autoscaler adds a node. It does not need to evict the API to schedule low-priority background work.

If node memory pressure happens after pods are already running, Kubernetes eviction uses QoS, priority, and usage relative to requests. The chart now makes the API and chat worker highest priority, with other workers and scheduled jobs lower priority.

If an API rollout starts and the new API pod cannot fit, `maxUnavailable: 0` keeps the old API pod serving. The rollout waits rather than intentionally taking the only API endpoint down.

Worker job recovery relies on the existing Celery configuration: `worker_prefetch_multiplier=1`, `task_acks_late=True`, and `task_reject_on_worker_lost=True` in `src/workers/base/celery_base.py`. That requeues work when Kubernetes kills a worker child/pod, subject to each task being safe to retry.

## Architect Review Findings

Fixed in this chart change:

- Dev cost regression: two Dev API replicas and larger API requests would reserve extra memory and fight one-node scale-down.
- Helm zero-value bug: `maxUnavailable: 0` must not pass through Helm `default`, because `default` renders it as `1`.
- Wrong priority tiers: API and chat worker are now both top priority. All other workers, chat beat, and CronJobs are lower priority.
- Dev PDB drain bug: a one-replica API PDB can block autoscaler drain, so it is disabled in Dev and retained in Prod.
- Startup hook starvation: API startup/migration hook now has top priority.

Remaining shortcomings:

- Dev cannot survive complete node loss while it is intentionally one node. That requires at least two nodes or a different runtime platform.
- Priority does not stop a process from exceeding its own container memory limit. If chat worker exceeds `2.5Gi`, that chat worker can still be OOMKilled.
- Priority only influences scheduling/preemption and eviction order. It does not replace task idempotency.
- Celery requeues lost worker tasks, but duplicated side effects are still possible for non-idempotent task implementations.
- `maxUnavailable: 0` protects Helm/Kubernetes rollouts, not arbitrary VM death.

## Verification Checklist

Use these checks before considering the change complete:

- `helm template` renders API Deployment, HPA, PDB, and priority classes without errors.
- `kubectl get deploy copilot-backend -n copilotgtm` shows `1/1` available in Dev.
- Dev does not render an API PDB.
- Prod renders an API PDB with `minAvailable: 1`.
- During a rollout, `kubectl get endpoints copilot-backend -n copilotgtm` always has at least one endpoint.
- Trigger one KEDA worker scale-up and confirm API endpoints remain populated.
- Trigger one worker restart and confirm queued work either completes or is retried according to documented task semantics.
- `kubectl describe node` no longer shows API availability depending on overcommitted worker limits on a single node.

Completed static verification:

- `helm lint charts/application -f charts/application/values.yaml -f charts/application/values-dev.yaml` passed.
- `helm lint charts/application -f charts/application/values.yaml -f charts/application/values-prod.yaml` passed.
- Focused Dev render shows API `replicas: 1`, rollout `maxUnavailable: 0`, original Dev API memory sizing, no API PDB, API startup hook/API/chat worker `priorityClassName: copilot-api-critical`, background worker/CronJob `priorityClassName: copilot-worker`, and soft topology spread.
- Focused Prod render shows API `replicas: 2`, rollout `maxUnavailable: 0`, API PDB `minAvailable: 1`, API startup hook/API/chat worker `priorityClassName: copilot-api-critical`, background worker/CronJob `priorityClassName: copilot-worker`, and soft topology spread.
- Server-side dry-run against `copilot-gtm-aks-dev` passed.
- Server-side dry-run against `copilot-gtm-aks-prod` passed.

## Non-Goals

- Do not make liveness probe depend on database, Redis, OpenAI, or worker availability.
- Do not solve API availability by keeping every worker always on.
- Do not add new dependencies.
- Do not change task semantics until the queue-level audit proves what needs changing.

## Bottom Line

The API is not production-safe in Dev because Dev is configured for cost: one node, one API replica, scale-to-zero workers, and low requests. That is acceptable for cheap development, but not for an environment where frontend/API availability matters.

The minimum production-way fix is two protected API replicas plus enough reserved capacity to schedule them independently of worker bursts. Worker failures should be isolated to worker Deployments and Celery retries; they should not be able to empty API endpoints or starve API scheduling.
