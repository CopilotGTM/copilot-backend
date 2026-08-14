# Production Rollout Readiness Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make production releases deterministic when PgBouncer reuses sessions, prevent migration state from leaking into application connections, avoid worker rollout CPU deadlocks, and emit useful diagnostics for the exact deployment that fails.

**Architecture:** Keep normal application connections on PgBouncer port 6432, but override only the Helm startup migration Job to PostgreSQL port 5432. Make the catalog validator independent of ambient session state with a transaction-local `pg_catalog` search path. Validate the live catalog immediately after migrations, roll queue workers without surge, preserve one autoscaler node of headroom, protect KEDA with cluster-critical priority, and capture failed-deployment diagnostics directly rather than depending on a shell `ERR` trap crossing function boundaries.

**Tech Stack:** Python 3.12, SQLAlchemy, PostgreSQL, Alembic, pytest, Helm, Kubernetes, Azure Pipelines, Bash.

---

## Task 1: Lock down search-path-independent catalog validation

**Files:**
- Modify: `tests/infra/test_tenant_runtime_readiness.py`
- Modify: `tests/migrations/org/test_085_rls_postgresql.py`
- Modify: `src/infra/database/tenant_runtime_readiness.py`

- [ ] Add sync and async unit assertions that `SET LOCAL search_path TO pg_catalog` executes immediately before the catalog SQL.
- [ ] Add a real-PostgreSQL regression that first contaminates the transaction search path with `tenant_data, extensions, public`, then proves the readiness validator still accepts valid policies.
- [ ] Run the focused tests and confirm the new unit tests fail before implementation.
- [ ] Add the single transaction-local search-path statement to both validator entry points. Keep `SET TRANSACTION READ ONLY` first when requested.
- [ ] Rerun the focused tests and confirm they pass.

## Task 2: Keep migrations off transaction-pooled connections

**Files:**
- Modify: `tests/scripts/test_shared_tenant_startup.py`
- Modify: `tests/scripts/test_backend_pipeline_deploy.py`
- Modify: `charts/application/charts/webservice/templates/job.yaml`
- Modify: `charts/application/values-prod.yaml`
- Modify: `scripts/startup.sh`

- [ ] Add static deployment-contract tests requiring a startup-Job-only `MIGRATION_DATABASE_PORT=5432` environment variable.
- [ ] Add a startup-script contract requiring the migration port to replace `DATABASE_PORT` and clear `DATABASE_URL` before any Python migration process starts.
- [ ] Confirm the new tests fail before implementation.
- [ ] Extend the existing webservice Job template to render job-specific environment variables alongside Workload Identity variables.
- [ ] Set `api.jobs.startup.env.MIGRATION_DATABASE_PORT` to `5432` only in production values; do not change the API or worker secret, which remains on PgBouncer.
- [ ] In `scripts/startup.sh`, export the direct migration port and unset any URL that could override it before importing application configuration.
- [ ] Run the exact runtime catalog validator after organisation migrations and before initialization/seeding so the Helm hook fails before workload rollout if the migrated database is unsafe.
- [ ] Rerun focused startup/chart tests.

## Task 3: Prevent worker rollout CPU surge deadlocks

**Files:**
- Modify: `tests/scripts/test_backend_pipeline_deploy.py`
- Modify: `charts/application/charts/worker/values.yaml`
- Add: `tests/infra/test_prod_aks_capacity.py`
- Modify: `infra/terraform/azure/prod/terraform.tfvars`
- Modify: `infra/terraform/azure/prod/addons.tf`

- [ ] Add a chart contract test requiring queue-worker rolling updates to use `maxSurge: 0` and `maxUnavailable: 1`.
- [ ] Confirm the test fails against the current worker default.
- [ ] Change the worker subchart default to replacement-first rolling updates. Celery queues remain durable and the existing graceful shutdown path drains terminating workers; API replicas retain their separate zero-unavailable strategy.
- [ ] Raise the production node-pool autoscaler ceiling from three to four so steady workload demand can trigger one headroom node.
- [ ] Assign KEDA's operator, metrics server, and admission webhook the existing `system-cluster-critical` priority so application workers cannot preempt the autoscaling control plane.
- [ ] Render the production chart and verify every worker Deployment has the bounded strategy while the API remains `maxSurge: 1`, `maxUnavailable: 0`.

## Task 4: Capture diagnostics for whichever deployment fails

**Files:**
- Modify: `tests/scripts/test_backend_pipeline_deploy.py`
- Modify: `.azure-pipelines/azure-backend-pipeline-dev.yml`
- Modify: `.azure-pipelines/azure-backend-pipeline-prod.yml`

- [ ] Replace the chat-worker-only static assertions with a generic failed-deployment diagnostic contract covering Deployment state, matching Pods, Pod descriptions, current logs, previous logs, and namespace events.
- [ ] Require `set -Eeuo pipefail` and an explicit failure branch inside `wait_if_present`; diagnostics must not depend solely on `ERR` inheritance.
- [ ] Confirm the contract tests fail before implementation.
- [ ] Implement the same bounded helper in Dev and Prod pipelines and pass the exact deployment name that timed out.
- [ ] Keep the chat-worker 600-second exception and existing workload wait list unchanged.
- [ ] Rerun pipeline contract tests and parse/render the pipeline YAML where tooling permits.

## Task 5: Verification and publication

**Files:** All files above.

- [ ] Run focused pytest suites for readiness, startup, migration search path, and pipeline deployment contracts.
- [ ] Run the real PostgreSQL readiness test when disposable test URLs are available; otherwise report the explicit skip and preserve the runnable regression.
- [ ] Run `helm dependency build` and render the production chart with non-secret placeholder values; inspect all Deployment strategies and the startup Job environment.
- [ ] Run Ruff/format checks for changed Python files.
- [ ] Run `.agents/skills/writing-python-comments/scripts/check_comment_smells.py`.
- [ ] Review `git diff --check`, the complete diff, and the exact staged file set.
- [ ] Commit only this recovery slice, push `codex/fix-prod-rollout-readiness`, and create a PR targeting `main`.

## Task 6: Bounded production recovery after explicit approval

- [ ] Before any mutation, re-read Helm status/history, workload images/readiness, pending Pods/events, and pipeline state using only `--context copilot-gtm-aks-prod`.
- [ ] Ask for fresh approval naming the exact recovery actions: resolve the pending Helm rollback if still present, recycle any PgBouncer backend sessions only if contamination remains, and deploy the merged release.
- [ ] Include the Terraform-backed node-pool maximum of four in the approved recovery and wait for KEDA's three control-plane Pods and external metrics APIService to become Ready before deploying the application.
- [ ] Do not reverse migrations 087/088; they completed successfully and Helm rollback does not roll database schema backward.
- [ ] Run the approved recovery actions one at a time and capture task/revision identifiers.
- [ ] Verify Helm reaches a terminal deployed revision, startup Job passes, all API/worker Deployments finish on one intended image, Pods are Ready without crash loops or unschedulable events, and the live runtime catalog gate passes.
- [ ] Report focused checks, PR merge, deployment result, and live production state separately.
