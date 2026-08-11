# Salesforce CDC Operations

This runbook operates the single aggregate Salesforce CDC worker in production. All checks below
are read-only. Any production deploy, metadata enablement, repair publication, pod restart, or
rollback requires fresh explicit approval; read-only access never grants mutation authority.

Set the context explicitly on every production command:

```powershell
$Context = "copilot-gtm-aks-prod"
$Namespace = "copilotgtm"
$Deployment = "copilot-backend-salesforce-cdc-worker"
```

## Deployment and process checks

Verify one replica, the dedicated Python command, image, strategy, and resources:

```powershell
kubectl --context $Context -n $Namespace get deployment $Deployment -o jsonpath='{.spec.replicas}{"`n"}{.spec.template.spec.containers[0].command}{"`n"}{.spec.template.spec.containers[0].image}{"`n"}{.spec.template.spec.containers[0].resources}{"`n"}{.spec.strategy}{"`n"}'
kubectl --context $Context -n $Namespace get hpa,scaledobject | Select-String $Deployment
kubectl --context $Context -n $Namespace get pods -l app.kubernetes.io/name=salesforce-cdc-worker -o wide
```

The expected state is one replica, command
`python -m src.workers.salesforce_cdc_worker.main`, no HPA or KEDA object, and limits of 1 CPU / 3
GiB with requests of 500m / 1 GiB.

Inspect restart count and the current/previous termination reason:

```powershell
$Pod = kubectl --context $Context -n $Namespace get pods -l app.kubernetes.io/name=salesforce-cdc-worker -o jsonpath='{.items[0].metadata.name}'
kubectl --context $Context -n $Namespace get pod $Pod -o jsonpath='{.status.containerStatuses[0].restartCount}{"`n"}{.status.containerStatuses[0].state}{"`n"}{.status.containerStatuses[0].lastState}{"`n"}'
kubectl --context $Context -n $Namespace logs $Pod --timestamps --tail=300
kubectl --context $Context -n $Namespace logs $Pod --previous --timestamps --tail=300
```

Inspect process live/ready files and aggregate counts:

```powershell
kubectl --context $Context -n $Namespace exec $Pod -- python -m src.workers.salesforce_cdc_worker.healthcheck --live
kubectl --context $Context -n $Namespace exec $Pod -- python -m src.workers.salesforce_cdc_worker.healthcheck --ready
kubectl --context $Context -n $Namespace exec $Pod -- sh -c 'cat /tmp/salesforce-cdc-live.json /tmp/salesforce-cdc-ready.json'
```

The readiness JSON reports `desired`, `running`, and `capacity_exceeded`. `running` must equal
`desired` unless an integration is repairing, waiting for a legacy lease, or rejected by capacity.

## Integration state

Use an approved read-only tenant database session and query the named integration only:

```sql
SELECT
    integration_id,
    connection_metadata #>> '{salesforce_cdc,mode}' AS mode,
    connection_metadata #>> '{salesforce_cdc,aggregate,status}' AS status,
    connection_metadata #>> '{salesforce_cdc,aggregate,heartbeat_at}' AS heartbeat_at,
    connection_metadata #>> '{salesforce_cdc,aggregate,lease_expires_at}' AS lease_expires_at,
    connection_metadata #>> '{salesforce_cdc,aggregate,last_replay_id}' IS NOT NULL AS has_replay,
    connection_metadata #>> '{salesforce_cdc,aggregate,last_event_at}' AS last_event_at,
    connection_metadata #>> '{salesforce_cdc,aggregate,last_error_class}' AS last_error_class,
    connection_metadata #>> '{salesforce_cdc,aggregate,last_error_message}' AS last_error_message
FROM knowledge_integrations
WHERE integration_id = :integration_id;
```

Do not print credentials or the replay value. Presence is enough. Correlate by IDs in structured
logs using these event names: `salesforce_cdc_discovery`, `salesforce_cdc_connected`,
`salesforce_cdc_disconnected`, `salesforce_cdc_replay_committed`,
`salesforce_cdc_repair_started`, `salesforce_cdc_repair_failed`, and
`salesforce_cdc_capacity_exceeded`.

## Repair checks

Inspect the repair tasks without publishing anything:

```powershell
kubectl --context $Context -n $Namespace exec deployment/copilot-backend-integrations-worker -- celery -A src.workers.integrations_worker.celery_app inspect active
kubectl --context $Context -n $Namespace exec deployment/copilot-backend-integrations-worker -- celery -A src.workers.integrations_worker.celery_app inspect reserved
kubectl --context $Context -n $Namespace exec deployment/copilot-backend-integrations-worker -- celery -A src.workers.integrations_worker.celery_app inspect scheduled
```

Filter the output for `sync_salesforce_*` and `mark_salesforce_cdc_repair_*`, then inspect the
integration's sync-history result in the tenant database. The repair chain is serial: deals,
companies, contacts, users, tasks, notes, then final state. Never start a second repair while one is
active, reserved, scheduled, or while aggregate status is `repairing`.

Salesforce retains platform events for about 72 hours. If the committed aggregate replay is outside
the retention window, incremental recovery is impossible. Keep the connector fenced, run the full
serial repair only after fresh approval, verify every sync-history phase succeeded, and let the
success finalizer return the aggregate state to `pending` with `EARLIEST`.

## Alerts

- Pod OOM or any restart-count increase: page immediately.
- Process heartbeat older than 60 seconds: page immediately.
- Desired stream not running for 5 minutes: page.
- Aggregate heartbeat older than 6 minutes while Salesforce is reachable: page.
- `repair_failed`: page.
- Capacity usage `7/7`: warn and plan capacity work.
- `capacity_exceeded`: page and reject onboarding of the eighth integration.

Metrics are:

```text
copilotgtm.salesforce.cdc.connection.count
copilotgtm.salesforce.cdc.event.count
copilotgtm.salesforce.cdc.record.count
copilotgtm.salesforce.cdc.reconnect.delay
copilotgtm.salesforce.cdc.repair.count
copilotgtm.salesforce.cdc.capacity.count
```

Only `outcome`, `entity`, and `reason` are metric labels. Organisation and integration IDs stay in
structured logs.

## Rollback to legacy mode

Rollback is a production mutation and requires fresh explicit approval. First stop aggregate
ownership and confirm its lease is inactive. Change only `salesforce_cdc.mode` back to `legacy`,
preserving the legacy per-object metadata. Do not copy the aggregate replay ID into any per-object
entry: replay IDs are opaque and scoped to their Pub/Sub topic. Resume legacy subscribers from their
own cursors, or use their existing full-repair path if those cursors have expired. Verify no aggregate
lease overlaps a legacy lease before accepting events.

The dry-run migration command is safe for planning; adding `--apply`, publishing a repair task,
restarting the Deployment, running Helm, or changing metadata is not and always needs fresh approval.
