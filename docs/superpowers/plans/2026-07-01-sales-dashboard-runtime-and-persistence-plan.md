# Sales Dashboard Runtime and Persistence Plan

## Current State

The Sales Dashboard API is mounted at `GET /users/dashboard/sales` and is documented in
`docs/API Documentation/sales-dashboard-api.md`.

The current implementation builds the dashboard synchronously from tenant CRM tables:

- `src/api/routers/dashboard/sales_dashboard_handler.py`
- `src/service/dashboard/sales_dashboard_service.py`
- `src/service/dashboard/data_provider.py`
- `src/service/dashboard/widgets/*`

Preferences are user-local and live in `public.users.preferences.dashboard.sales`.
Dashboard data is not persisted today.

## Live Dev Finding

On July 1, 2026, the dev API pod was healthy:

- Kubernetes context: `copilot-gtm-aks-dev`
- Namespace: `copilotgtm`
- API image: `copilotgtmacrdev.azurecr.io/copilot-backend-api:1725`
- Public health endpoint: `https://devapi.copilotgtm.com/health` returned `200`

A real user request to `GET /users/dashboard/sales` failed with HTTP 500.
Recent request IDs included:

- `af804b65-1d19-48bd-bdb7-efd700a66e90`
- `a227f1e3-ed02-49a3-94b1-6f7578cd83ee`
- `cfdc0a76-f1c5-4434-88ac-629f2741c937`

The failing stack is:

```text
sales_dashboard_handler.get_sales_dashboard
SalesDashboardService.get_sales_dashboard
SalesDashboardService._build_dashboard_sections
build_diagnostic_widgets
SalesDashboardDataProvider.get_stage_movement_analytics
SalesDashboardDataProvider._get_stage_movement_history
AttributeError: stage_external_id
```

Root cause: `SalesDashboardDataProvider._get_stage_movement_history()` was still
projecting `crm_deal_stage_history.stage_external_id` and `from_stage_external_id`.
Those provider columns were intentionally removed from `CRMDealStageHistory`; the
current model and other readers use `stage_id` and `from_stage_id`, joining
`crm_deal_stages` for labels.

This is an application-code drift, not a Kubernetes health issue and not an org
schema migration issue.

## Immediate Run Plan

1. Keep the dashboard route synchronous for the first recovery step.
2. Fix only `SalesDashboardDataProvider._get_stage_movement_history()`:
   - project `stage_id` and `from_stage_id`
   - use joined stage labels/names for display
   - fall back to the internal UUID string only when a stage row is missing
3. Validate locally with:
   ```powershell
   uv run pytest tests/service/dashboard/test_sales_dashboard_service.py tests/service/dashboard/widgets/test_stage_movement_widget.py tests/service/crm/test_stage_movement_analytics.py
   ```
4. Deploy the API image to dev.
5. Re-trigger `GET /users/dashboard/sales` from the frontend.
6. Confirm live logs show HTTP 200 for `/users/dashboard/sales` and no
   `Failed to calculate stage movement analytics` errors.

## Persistence Goal

After the live dashboard runs, persist generated dashboard snapshots per
organisation so the frontend can read a fast, stable payload and future AI-created
dashboards can reuse the same storage model.

## Proposed Tenant Table

Create a tenant-scoped table in each `org_*` schema:

```text
dashboards
- dashboard_id uuid primary key
- organisation_id uuid not null
- dashboard_key text not null
- dashboard_name text not null
- dashboard_type text not null
- schema_version integer not null
- definition jsonb not null default '{}'
- latest_data jsonb
- latest_data_generated_at timestamptz
- latest_generation_status text not null default 'pending'
- latest_generation_error text
- refresh_interval_seconds integer not null default 86400
- created_by_user_id uuid
- created_at timestamptz not null
- updated_at timestamptz not null
- is_deleted boolean not null default false
```

Recommended constraints/indexes:

- unique active dashboard per organisation/key:
  `(organisation_id, dashboard_key)` where `is_deleted = false`
- lookup for scheduled refresh:
  `(organisation_id, latest_generation_status, latest_data_generated_at)`

The first dashboard row should use:

- `dashboard_key = 'sales'`
- `dashboard_name = 'Sales Dashboard'`
- `dashboard_type = 'system'`
- `schema_version = 1`
- `definition` containing the enabled widget IDs and default filters
- `latest_data` containing the same `SalesDashboardResponse` payload returned by
  the current API

## Service Design

Keep the first implementation small:

- `SalesDashboardService` remains the renderer for the Sales Dashboard payload.
- Add `DashboardSnapshotService` for persistence concerns:
  - load or create the `sales` dashboard metadata row
  - render using `SalesDashboardService`
  - store `latest_data`, `latest_data_generated_at`, status, and error
- Add a read path that can return the latest snapshot, with optional synchronous
  regeneration during the transition period.

Do not create a generalized AI dashboard builder yet. The table shape should allow
it later through `definition`, but the first implementation should only persist the
existing Sales Dashboard.

## 24-Hour Job

Use the existing worker/scheduler pattern rather than inventing a new scheduler.
The job should run once every 24 hours and enqueue or execute one organisation at a
time:

1. List active organisations from `public.organisations`.
2. For each organisation, acquire an org session.
3. Ensure the `sales` dashboard row exists.
4. Render the dashboard payload.
5. Save the payload into `dashboards.latest_data`.
6. Mark success/failure per organisation without stopping the full run.

Operational requirements:

- log `organisation_id`, `dashboard_key`, status, duration, and error message
- never let one org failure block another org
- make reruns idempotent by updating the same `dashboard_key`
- keep the synchronous API available until the scheduled snapshot is proven stable

## API Transition

Phase 1:

- Keep `GET /users/dashboard/sales` rendering live data after the immediate fix.

Phase 2:

- Add a snapshot-backed internal path:
  - return `latest_data` when fresh enough
  - optionally regenerate when missing or stale

Phase 3:

- Make `GET /users/dashboard/sales` prefer the snapshot and expose a refresh
  endpoint only if the frontend needs an explicit manual refresh.

## Verification Gates

Before calling the dashboard work complete:

- live dev request to `/users/dashboard/sales` returns HTTP 200
- API logs do not show `stage_external_id` or dashboard analytics failures
- focused dashboard tests pass
- org migration creates the `dashboards` table in a test org schema
- 24-hour job can process one target org and save a non-empty `latest_data`
- rerunning the job updates the existing `sales` row instead of creating a
  duplicate
