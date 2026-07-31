# Shared-row tenancy clean-break operations runbook

This runbook is the executable control for moving one explicitly selected
organisation dataset into the Terraform-owned `tenant_data` schema. Other
legacy organisation schemas remain untouched for later approved cutovers. It is
forward-only:

- master/control-plane data stays in `public` and continues through master
  session functions;
- every organisation row lives in `tenant_data` and continues through
  organisation session functions;
- the permanent runtime role is `app_tenant_runtime`, with forced PostgreSQL
  row-level security (RLS);
- organisation migrations always receive an explicit schema;
- the old per-organisation schema, role, secret, provider branch, and
  provisioning path are deleted during the same approved change.

There is no reverse copy and no legacy runtime fallback. Before source deletion,
stop and correct the permanent path or restore the database backup into an
isolated server. After source deletion, recovery restores the tenant export into
`tenant_data`; it never recreates an `org_*` runtime.

## Authority and stop conditions

This document is not approval to mutate any environment. Task 16 requires an
approved non-production window. Task 17 requires fresh approval for every
production mutation.

Two people must be assigned before a drill or cutover:

1. the operator, who runs the commands;
2. the approver, who verifies evidence and owns every continue/stop decision.

Stop immediately if any command exits non-zero, evidence differs from the
expected result, an unapproved writer appears, or a target differs from the
signed manifest. Do not delete source resources until every deletion gate below
is independently satisfied. Never put passwords, access tokens, connection
strings, raw rows, or unredacted Helm values in evidence.

## 1. Set exact inputs and create the evidence root

Run from the repository root in PowerShell 7. Replace every value marked
`REQUIRED`. Use `dev` for Task 16 and `prod` only for an independently approved
Task 17.

```powershell
$ErrorActionPreference = "Stop"
$Environment = "REQUIRED-dev-or-prod"
$TerraformDir = "infra/terraform/azure/$Environment"
$ValuesFile = "charts/application/values-$Environment.yaml"
$KubeContext = "REQUIRED"
$Namespace = "copilotgtm"
$Release = "copilot-backend"
$ApiDeployment = "copilot-backend"
$ImageTag = "REQUIRED-immutable-image-tag"

$OrganisationId = "REQUIRED-uuid"
$IsolationOrganisationId = "REQUIRED-different-uuid"
$SourceSchema = "REQUIRED-exact-org-schema"
$SourceRole = "REQUIRED-exact-org-role"
$SourceSecret = "REQUIRED-exact-key-vault-secret"
$RuntimeRole = "app_tenant_runtime"

$AzureResourceGroup = "REQUIRED"
$AzurePostgresServer = "REQUIRED"
$AzureKeyVault = "REQUIRED"
$RestoreServer = "$AzurePostgresServer-shared-tenant-proof-$(Get-Date -Format 'yyyyMMddHHmmss')"
$RestoreTimeUtc = (Get-Date).ToUniversalTime().AddMinutes(-5).ToString("yyyy-MM-ddTHH:mm:ssZ")

$EvidenceRoot = Join-Path $PWD "artifacts/shared-tenancy/$Environment-$(Get-Date -Format 'yyyyMMddTHHmmssZ')"
if (Test-Path -LiteralPath $EvidenceRoot) {
    throw "Evidence directory already exists: $EvidenceRoot"
}
New-Item -ItemType Directory -Path $EvidenceRoot | Out-Null

$PrecheckReport = Join-Path $EvidenceRoot "01-precheck.json"
$TerraformPlan = Join-Path $EvidenceRoot "02-tenant-data.tfplan"
$WritersStoppedEvidence = Join-Path $EvidenceRoot "03-writers-stopped.json"
$TenantExport = Join-Path $EvidenceRoot "04-tenant-export.zip"
$CutoverReport = Join-Path $EvidenceRoot "05-copy-and-validation.json"
$SmokeReport = Join-Path $EvidenceRoot "06-permanent-runtime-smoke.json"
```

Validate the exact environment and identity before doing anything else:

```powershell
if ($Environment -notin @("dev", "prod")) { throw "Environment must be dev or prod" }
if ([guid]::Parse($OrganisationId) -eq [guid]::Parse($IsolationOrganisationId)) {
    throw "Isolation organisation must differ from the approved organisation"
}
kubectl config get-contexts $KubeContext
kubectl --context $KubeContext -n $Namespace get deploy $ApiDeployment
helm status $Release -n $Namespace --kube-context $KubeContext
git rev-parse HEAD | Tee-Object (Join-Path $EvidenceRoot "00-git-sha.txt")
git status --short | Tee-Object (Join-Path $EvidenceRoot "00-git-status.txt")
```

The approver must record the exact organisation UUID, source schema, source
role, source secret, image digest/tag, Git SHA, Azure server, backup/export
destinations, window, operator, approver, and approved command version in the
change record. Wildcards and prefixes are not acceptable deletion targets.

Copy the evidence directory to approved write-once or versioned storage after
each numbered gate. Local files alone are not immutable evidence.

## 2. Read-only readiness gate

Run the model audit without database access:

```powershell
uv run python -m scripts.maintenance.audit_shared_tenant_readiness --models --json |
    Tee-Object (Join-Path $EvidenceRoot "01-model-audit.json")
```

Run the live database audit and organisation-bound precheck with the operator's
approved master-database environment loaded:

```powershell
uv run python -m scripts.maintenance.audit_shared_tenant_readiness `
    --database `
    --schema tenant_data `
    --runtime-role $RuntimeRole `
    --json |
    Tee-Object (Join-Path $EvidenceRoot "01-database-audit-before.json")

uv run python -m scripts.maintenance.cutover_shared_tenant_schema precheck `
    --organisation-id $OrganisationId `
    --report $PrecheckReport
```

The approver must inspect the reports, not only the exit code. Continue only
when all of the following are proved:

- `$SourceSchema` is populated and is the exact schema derived from
  `$OrganisationId`;
- its derived role is exactly `$SourceRole`;
- the organisation UUID is exactly `$OrganisationId`;
- source and destination migration heads are compatible;
- every tenant table has a non-null organisation identity and the required
  tenant-qualified keys;
- `tenant_data`, forced RLS, policies, ownership, grants, and the runtime role
  are correct wherever they already exist;
- no unexpected tenant schema, source writer, or legacy runtime caller exists.

Hash the evidence:

```powershell
Get-FileHash -Algorithm SHA256 `
    (Join-Path $EvidenceRoot "01-model-audit.json"), `
    (Join-Path $EvidenceRoot "01-database-audit-before.json"), `
    $PrecheckReport |
    ConvertTo-Json |
    Set-Content -NoNewline (Join-Path $EvidenceRoot "01-readiness-hashes.json")
```

## 3. Provision `tenant_data` once with Terraform

Terraform owns only the durable schema container. Alembic owns tables,
constraints, policies, grants, and migration history inside that schema.
Organisation creation does not run schema DDL.

```powershell
terraform "-chdir=$TerraformDir" fmt -check
terraform "-chdir=$TerraformDir" init -lockfile=readonly
terraform "-chdir=$TerraformDir" validate
terraform "-chdir=$TerraformDir" plan "-out=$TerraformPlan"
terraform "-chdir=$TerraformDir" show -json $TerraformPlan |
    Set-Content -NoNewline (Join-Path $EvidenceRoot "02-terraform-plan.json")
Get-FileHash -Algorithm SHA256 $TerraformPlan, `
    (Join-Path $EvidenceRoot "02-terraform-plan.json") |
    ConvertTo-Json |
    Set-Content -NoNewline (Join-Path $EvidenceRoot "02-terraform-plan-hashes.json")
```

The plan must create or preserve only
`postgresql_schema.tenant_data`; it must not replace the PostgreSQL server,
database, source schema, source role, or secrets. Apply the exact reviewed plan:

```powershell
terraform "-chdir=$TerraformDir" apply $TerraformPlan
terraform "-chdir=$TerraformDir" state show postgresql_schema.tenant_data |
    Tee-Object (Join-Path $EvidenceRoot "02-terraform-state-tenant-data.txt")
```

The resource has `if_not_exists = true`, `drop_cascade = false`, and
`prevent_destroy = true`. It is created once and retained.

## 4. Run organisation migrations against an explicit schema

No environment switch chooses an organisation schema. The command argument is
the target, so the same chain can run against `tenant_data` in dev/prod or an
isolated test schema.

```powershell
uv run python -m scripts.run_org_migrations --schema tenant_data --precheck |
    Tee-Object (Join-Path $EvidenceRoot "02-org-migration-precheck.txt")
uv run python -m scripts.run_org_migrations --schema tenant_data |
    Tee-Object (Join-Path $EvidenceRoot "02-org-migration-apply.txt")
uv run python -m scripts.maintenance.audit_shared_tenant_readiness `
    --database `
    --schema tenant_data `
    --runtime-role $RuntimeRole `
    --json |
    Tee-Object (Join-Path $EvidenceRoot "02-database-audit-after-migration.json")
```

For an isolated rehearsal schema, replace only the explicit argument:

```powershell
uv run python -m scripts.run_org_migrations --schema shared_tenant_rehearsal --precheck
uv run python -m scripts.run_org_migrations --schema shared_tenant_rehearsal
```

Never edit `.env` to choose a migration schema and never run the organisation
chain against `public`.

## 5. Prove the full-database backup is restorable

Azure PostgreSQL point-in-time restore is the full-database recovery proof.
Creating a backup reference without restoring it is not sufficient.

```powershell
az postgres flexible-server show `
    --resource-group $AzureResourceGroup `
    --name $AzurePostgresServer `
    --output json |
    Tee-Object (Join-Path $EvidenceRoot "03-source-server-before-restore.json")

az postgres flexible-server restore `
    --resource-group $AzureResourceGroup `
    --name $RestoreServer `
    --source-server $AzurePostgresServer `
    --restore-time $RestoreTimeUtc `
    --output json |
    Tee-Object (Join-Path $EvidenceRoot "03-restore-create.json")

$OriginalPgHost = $env:PGHOST
$RestoredPgHost = az postgres flexible-server show `
    --resource-group $AzureResourceGroup `
    --name $RestoreServer `
    --query fullyQualifiedDomainName `
    --output tsv
$env:PGHOST = $RestoredPgHost
az postgres flexible-server show `
    --resource-group $AzureResourceGroup `
    --name $RestoreServer `
    --output json |
    Tee-Object (Join-Path $EvidenceRoot "03-restored-server.json")
```

Wait until the restored server reports `Ready`. Connect to the isolated restored
server using approved operator credentials and run catalog checks and aggregate
counts/checksums only. Do not emit rows:

```powershell
psql -X -v ON_ERROR_STOP=1 -d $env:PGDATABASE `
    -c "SELECT current_database(), current_user, version();" |
    Tee-Object (Join-Path $EvidenceRoot "03-restore-connectivity.txt")
psql -X -v ON_ERROR_STOP=1 -d $env:PGDATABASE `
    -c "SELECT schemaname, count(*) AS table_count FROM pg_tables WHERE schemaname IN ('$SourceSchema','tenant_data') GROUP BY schemaname ORDER BY schemaname;" |
    Tee-Object (Join-Path $EvidenceRoot "03-restore-catalog.txt")
```

Reset the connection target before continuing:

```powershell
$env:PGHOST = $OriginalPgHost
```

Define the backup reference as the immutable change-record URL containing the
source server ID, `$RestoreTimeUtc`, restored server ID, and restore evidence
hashes:

```powershell
$BackupReference = "REQUIRED-immutable-backup-evidence-url"
```

Do not continue until the approver can retrieve the restore proof from approved
storage. The row-scoped tenant export is created after the atomic copy.

## 6. Stop every writer and prove queue drain

First suspend release-owned CronJobs and stop Celery Beat deployments so they
cannot publish new work:

```powershell
$CronJobs = @(
    kubectl --context $KubeContext -n $Namespace get cronjob `
        -l "app.kubernetes.io/instance=$Release" `
        -o json |
        ConvertFrom-Json |
        Select-Object -ExpandProperty items |
        ForEach-Object { $_.metadata.name }
)
foreach ($CronJob in $CronJobs) {
    kubectl --context $KubeContext -n $Namespace patch cronjob $CronJob `
        --type merge `
        -p '{"spec":{"suspend":true}}'
}

$Deployments = @(
    kubectl --context $KubeContext -n $Namespace get deployment `
        -l "app.kubernetes.io/instance=$Release" `
        -o json |
        ConvertFrom-Json |
        Select-Object -ExpandProperty items
)
$BeatDeployments = @(
    $Deployments |
        Where-Object {
            ($_.spec.template.spec.containers[0].command -contains "beat")
        } |
        ForEach-Object { $_.metadata.name }
)
foreach ($Deployment in $BeatDeployments) {
    kubectl --context $KubeContext -n $Namespace scale deployment $Deployment --replicas=0
}
```

Stop incoming API work. The HPA is removed only as a controlled, Helm-recreated
resource; the API deployment remains present for audit.

```powershell
kubectl --context $KubeContext -n $Namespace delete hpa $ApiDeployment --ignore-not-found
kubectl --context $KubeContext -n $Namespace scale deployment $ApiDeployment --replicas=0
kubectl --context $KubeContext -n $Namespace get endpoints $ApiDeployment -o json |
    Tee-Object (Join-Path $EvidenceRoot "03-api-endpoints-after-stop.json")
```

Let existing workers finish. Select one live worker pod and capture all Celery
states. Run this repeatedly until every worker maps to an empty list for all
three commands:

```powershell
$WorkerPod = (
    kubectl --context $KubeContext -n $Namespace get pods `
        -l "app.kubernetes.io/instance=$Release" `
        -o json |
        ConvertFrom-Json |
        Select-Object -ExpandProperty items |
        Where-Object {
            $_.status.phase -eq "Running" -and
            ($_.spec.containers[0].command -contains "worker")
        } |
        Select-Object -First 1
).metadata.name
if (-not $WorkerPod) { throw "No live Celery worker is available for drain inspection" }

kubectl --context $KubeContext -n $Namespace exec $WorkerPod -- `
    celery -A src.workers.chat_worker.celery_app:app inspect active --json --timeout=15 |
    Tee-Object (Join-Path $EvidenceRoot "03-celery-active.json")
kubectl --context $KubeContext -n $Namespace exec $WorkerPod -- `
    celery -A src.workers.chat_worker.celery_app:app inspect reserved --json --timeout=15 |
    Tee-Object (Join-Path $EvidenceRoot "03-celery-reserved.json")
kubectl --context $KubeContext -n $Namespace exec $WorkerPod -- `
    celery -A src.workers.chat_worker.celery_app:app inspect scheduled --json --timeout=15 |
    Tee-Object (Join-Path $EvidenceRoot "03-celery-scheduled.json")
```

Inspect broker queue lengths without printing Redis credentials:

```powershell
$QueueCheck = 'import json; from src.infra.messaging.redis_client import get_redis_client; r=get_redis_client(); q=["knowledge_extract","knowledge_upload","organization","attachments","chat_processing","chat_messages","voice_realtime","integrations","deal","deal_jobs"]; print(json.dumps({n:r.llen(n) for n in q},sort_keys=True))'
kubectl --context $KubeContext -n $Namespace exec $WorkerPod -- python -c $QueueCheck |
    Tee-Object (Join-Path $EvidenceRoot "03-celery-queue-lengths.json")
```

Continue only when active, reserved, and scheduled lists and every broker queue
length are zero. Then pause KEDA at zero and stop every remaining release-owned
worker:

```powershell
$ScaledObjects = @(
    kubectl --context $KubeContext -n $Namespace get scaledobject `
        -l "app.kubernetes.io/instance=$Release" `
        -o json |
        ConvertFrom-Json |
        Select-Object -ExpandProperty items |
        ForEach-Object { $_.metadata.name }
)
foreach ($ScaledObject in $ScaledObjects) {
    kubectl --context $KubeContext -n $Namespace annotate scaledobject $ScaledObject `
        autoscaling.keda.sh/paused="true" `
        autoscaling.keda.sh/paused-replicas="0" `
        --overwrite
}

$WriterDeployments = @(
    $Deployments |
        Where-Object { $_.metadata.name -ne $ApiDeployment } |
        ForEach-Object { $_.metadata.name }
)
foreach ($Deployment in $WriterDeployments) {
    kubectl --context $KubeContext -n $Namespace scale deployment $Deployment --replicas=0
}
kubectl --context $KubeContext -n $Namespace get deploy,pods,cronjob,scaledobject -o wide |
    Tee-Object (Join-Path $EvidenceRoot "03-kubernetes-writers-stopped.txt")
```

Using the approved master PostgreSQL connection, prove no source-role sessions
remain:

```powershell
psql -X -v ON_ERROR_STOP=1 -d $env:PGDATABASE `
    -v source_role=$SourceRole `
    -c "SELECT pid, usename, application_name, state FROM pg_stat_activity WHERE usename = :'source_role' ORDER BY pid;" |
    Tee-Object (Join-Path $EvidenceRoot "03-source-role-sessions.txt")
```

Create the writer-stop evidence only after the approver has verified every file:

```powershell
$WriterEvidence = [ordered]@{
    operation = "writers-stopped"
    result = "passed"
    environment = $Environment
    organisation_id = $OrganisationId
    api_endpoints = 0
    celery_active = 0
    celery_reserved = 0
    celery_scheduled = 0
    broker_queue_depth = 0
    source_role_sessions = 0
    evidence_root = "REQUIRED-immutable-evidence-url"
}
$WriterEvidence | ConvertTo-Json |
    Set-Content -NoNewline $WritersStoppedEvidence
Get-FileHash -Algorithm SHA256 $WritersStoppedEvidence |
    ConvertTo-Json |
    Set-Content -NoNewline (Join-Path $EvidenceRoot "03-writers-stopped-hash.json")
```

## 7. Copy and validate atomically

This is the only command that copies the source dataset. It runs in one
transaction and refuses incomplete parity.

```powershell
uv run python -m scripts.maintenance.cutover_shared_tenant_schema copy-and-validate `
    --organisation-id $OrganisationId `
    --report $CutoverReport `
    --writers-stopped-evidence "REQUIRED-immutable-writer-evidence-url" `
    --backup-reference $BackupReference `
    --apply
Get-FileHash -Algorithm SHA256 $CutoverReport |
    ConvertTo-Json |
    Set-Content -NoNewline (Join-Path $EvidenceRoot "05-copy-report-hash.json")
```

The approver must verify `result = copied_and_validated`, the exact organisation
and source schema, and all seven validations:

- counts;
- primary-key sets;
- row checksums;
- foreign keys;
- uniqueness;
- RLS;
- isolation sentinel unchanged.

Every table report must contain equal source/destination counts, primary-key
SHA-256 values, and row SHA-256 values. Any unexplained difference is a stop.

Now export the copied organisation from `tenant_data` through the row-aware
lifecycle tool. This must happen after copy and before source deletion:

```powershell
uv run python -m scripts.maintenance.manage_shared_tenant_data export `
    --organisation-id $OrganisationId `
    --destination $TenantExport
uv run python -m scripts.maintenance.manage_shared_tenant_data verify `
    --organisation-id $OrganisationId `
    --manifest $TenantExport |
    Tee-Object (Join-Path $EvidenceRoot "05-tenant-export-verify.json")
Get-FileHash -Algorithm SHA256 $TenantExport |
    ConvertTo-Json |
    Set-Content -NoNewline (Join-Path $EvidenceRoot "05-tenant-export-hash.json")
```

## 8. Deploy the permanent runtime while traffic stays closed

Deploy the approved image with only one internal API replica. The release reuses
its existing secret-bearing values; do not export or record them.

```powershell
helm upgrade $Release charts/application `
    --namespace $Namespace `
    --kube-context $KubeContext `
    --reuse-values `
    --set "api.image.tag=$ImageTag" `
    --set "api.gateway.enabled=false" `
    --set "api.ingress.enabled=false" `
    --set "api.autoscaling.enabled=false" `
    --set "api.replicaCount=1" `
    --set "knowledge-extract-worker.enabled=false" `
    --set "knowledge-upload-worker.enabled=false" `
    --set "knowledge-beat.enabled=false" `
    --set "organization-worker.enabled=false" `
    --set "chat-worker.enabled=false" `
    --set "chat-beat.enabled=false" `
    --set "integrations-worker.enabled=false" `
    --set "deal-worker.enabled=false" `
    --set "deal-insights-worker.enabled=false" `
    --set "deal-beat.enabled=false" `
    --set "integrations-beat.enabled=false" `
    --set "integrationsCronJobs.enabled=false" `
    --set "knowledgeCronJobs.enabled=false" `
    --set "dealCronJobs.enabled=false" `
    --wait `
    --timeout 15m
kubectl --context $KubeContext -n $Namespace rollout status deployment/$ApiDeployment --timeout=10m
```

Verify the permanent configuration and full database readiness from the deployed
artifact:

```powershell
$RuntimeCheck = 'from src.infra.config.configs import database_config; assert database_config.USE_SHARED_TENANT_DATABASE is True; assert database_config.TENANT_DATABASE_USER == "app_tenant_runtime"; print("permanent shared tenant runtime configured")'
kubectl --context $KubeContext -n $Namespace exec deployment/$ApiDeployment -- `
    python -c $RuntimeCheck |
    Tee-Object (Join-Path $EvidenceRoot "06-permanent-runtime-config.txt")
kubectl --context $KubeContext -n $Namespace exec deployment/$ApiDeployment -- `
    python -m scripts.maintenance.audit_shared_tenant_readiness `
    --database --schema tenant_data --runtime-role $RuntimeRole --json |
    Tee-Object (Join-Path $EvidenceRoot "06-deployed-database-audit.json")
```

## 9. Run master, organisation, workflow, and RLS smoke tests

Port-forward the internal API in a second PowerShell window:

```powershell
kubectl --context $KubeContext -n $Namespace port-forward deployment/$ApiDeployment 18080:8080
```

In the operator window, load approved smoke-user credentials into
`SMOKE_EMAIL` and `SMOKE_PASSWORD`. Do not enable command echoing and do not
write the login response or bearer token to disk.

```powershell
$ApiBase = "http://127.0.0.1:18080"
$LoginBody = @{
    email = $env:SMOKE_EMAIL
    password = $env:SMOKE_PASSWORD
} | ConvertTo-Json
$Login = Invoke-RestMethod `
    -Method Post `
    -Uri "$ApiBase/users/auth/login" `
    -ContentType "application/json" `
    -Body $LoginBody
$AccessToken = $Login.data.tokens.access_token
if (-not $AccessToken) { throw "Master login did not return an access token" }
$Headers = @{ Authorization = "Bearer $AccessToken" }

$SmokeEndpoints = [ordered]@{
    health = "/health"
    integrations = "/users/integrations/available"
    bots = "/users/integrations/bots/available"
    agents = "/users/v1/agents"
    jobs = "/users/v1/agent-runs?limit=1"
}
$ApiSmoke = [ordered]@{}
foreach ($Name in $SmokeEndpoints.Keys) {
    $Response = Invoke-WebRequest `
        -Method Get `
        -Uri "$ApiBase$($SmokeEndpoints[$Name])" `
        -Headers $Headers `
        -SkipHttpErrorCheck
    $ApiSmoke[$Name] = [ordered]@{
        path = $SmokeEndpoints[$Name]
        status = [int]$Response.StatusCode
        passed = ([int]$Response.StatusCode -eq 200)
    }
}
$ApiSmoke | ConvertTo-Json -Depth 4 |
    Set-Content -NoNewline (Join-Path $EvidenceRoot "06-api-smoke.json")
if (@($ApiSmoke.Values | Where-Object { -not $_.passed }).Count -ne 0) {
    throw "One or more API smoke checks failed"
}
$AccessToken = $null
$Login = $null
$LoginBody = $null
```

The API checks prove master login plus organisation-backed integration, agent,
and job reads. Prove organisation writes and isolation directly as the runtime
role in one rolled-back transaction. Load `PGHOST`, `PGPORT`, `PGDATABASE`,
`PGUSER=app_tenant_runtime`, and `PGPASSWORD` through the approved secret path.
The output contains only the selected table name and pass/fail notices.

```powershell
$TenantSmokeSql = Join-Path $EvidenceRoot "06-tenant-rls-smoke.sql"
@'
\set ON_ERROR_STOP on
BEGIN;
CREATE TEMP TABLE tenant_smoke_target (
    table_name text NOT NULL,
    organisation_id uuid NOT NULL
) ON COMMIT DROP;

SELECT set_config('app.current_organisation_id', :'organisation_id', true);

DO $block$
DECLARE
    candidate text;
    visible_rows bigint;
    updated_rows bigint := 0;
BEGIN
    FOR candidate IN
        SELECT c.table_name
        FROM information_schema.columns AS c
        WHERE c.table_schema = 'tenant_data'
          AND c.column_name = 'organisation_id'
        ORDER BY c.table_name
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM tenant_data.%I WHERE organisation_id = current_setting(''app.current_organisation_id'')::uuid',
            candidate
        ) INTO visible_rows;
        IF visible_rows > 0 THEN
            INSERT INTO tenant_smoke_target
            VALUES (candidate, current_setting('app.current_organisation_id')::uuid);
            EXECUTE format(
                'UPDATE tenant_data.%I SET organisation_id = organisation_id WHERE ctid = (SELECT ctid FROM tenant_data.%I WHERE organisation_id = current_setting(''app.current_organisation_id'')::uuid LIMIT 1)',
                candidate,
                candidate
            );
            GET DIAGNOSTICS updated_rows = ROW_COUNT;
            EXIT;
        END IF;
    END LOOP;
    IF updated_rows <> 1 THEN
        RAISE EXCEPTION 'organisation write smoke did not update exactly one row';
    END IF;
END
$block$;

SELECT set_config('app.current_organisation_id', :'isolation_organisation_id', true);

DO $block$
DECLARE
    target record;
    leaked_rows bigint;
BEGIN
    SELECT * INTO STRICT target FROM tenant_smoke_target;
    EXECUTE format(
        'SELECT count(*) FROM tenant_data.%I WHERE organisation_id = $1',
        target.table_name
    ) INTO leaked_rows USING target.organisation_id;
    IF leaked_rows <> 0 THEN
        RAISE EXCEPTION 'RLS isolation smoke exposed % row(s)', leaked_rows;
    END IF;
    RAISE NOTICE 'organisation write and RLS isolation passed using table %',
        target.table_name;
END
$block$;
ROLLBACK;
'@ | Set-Content -NoNewline $TenantSmokeSql

psql -X -v ON_ERROR_STOP=1 -d $env:PGDATABASE `
    -v organisation_id=$OrganisationId `
    -v isolation_organisation_id=$IsolationOrganisationId `
    -f $TenantSmokeSql |
    Tee-Object (Join-Path $EvidenceRoot "06-tenant-rls-smoke.txt")
```

Run the static clean-break audit against the exact deployed Git SHA:

```powershell
rg -n "get_schema_name\(|get_role_name\(|get_key_vault_secret_name\(" `
    src/api src/infra/database src/service src/workers `
    -g "!shared_tenancy_cutover_service.py" |
    Tee-Object (Join-Path $EvidenceRoot "06-runtime-legacy-reference-search.txt")
```

Expected result: no runtime hit. The maintenance-only cutover service and
`scripts/maintenance` are deliberately excluded.

Create the smoke report only after the approver has opened every referenced
file. The required booleans summarize evidence; they do not replace it.

```powershell
$CutoverHash = (Get-FileHash -Algorithm SHA256 $CutoverReport).Hash.ToLowerInvariant()
$ExportHash = (Get-FileHash -Algorithm SHA256 $TenantExport).Hash.ToLowerInvariant()
$SmokeEvidenceUrl = "REQUIRED-immutable-smoke-evidence-url"
$ExportReference = "REQUIRED-immutable-export-url#sha256=$ExportHash"

$Smoke = [ordered]@{
    operation = "permanent-runtime-smoke"
    result = "passed"
    export_reference = $ExportReference
    cutover_report_sha256 = $CutoverHash
    approved_source_resources = [ordered]@{
        schema = $SourceSchema
        role = $SourceRole
        secret = $SourceSecret
    }
    checks = [ordered]@{
        master_login = $true
        organisation_read = $true
        organisation_write = $true
        integrations = $true
        bots = $true
        agents = $true
        jobs = $true
        rls_isolation = $true
        permanent_runtime = $true
    }
    evidence_reference = $SmokeEvidenceUrl
}
$Smoke | ConvertTo-Json -Depth 5 |
    Set-Content -NoNewline $SmokeReport
Get-FileHash -Algorithm SHA256 $SmokeReport |
    ConvertTo-Json |
    Set-Content -NoNewline (Join-Path $EvidenceRoot "06-smoke-report-hash.json")
```

## 10. Irreversibly delete the exact old resources

This is the point of no return for the old runtime. Obtain fresh approval for
this exact command and these exact three targets. The CLI independently derives
the expected schema and role from the organisation UUID, binds them to the
copy/smoke report hashes, rejects wildcard/prefix targets, and verifies absence.

```powershell
uv run python -m scripts.maintenance.cutover_shared_tenant_schema delete-source-resources `
    --source-schema $SourceSchema `
    --source-role $SourceRole `
    --source-secret $SourceSecret `
    --cutover-report $CutoverReport `
    --smoke-report $SmokeReport `
    --apply
```

The companion deletion report is created beside the smoke report. If database
deletion commits but Key Vault deletion fails, the source database resources
remain absent; fix Key Vault access and rerun the same exact, hash-linked
command. Do not recreate the schema or role.

## 11. Post-deletion absence and parity proof

Using the master/operator database connection:

```powershell
psql -X -v ON_ERROR_STOP=1 -d $env:PGDATABASE `
    -v source_schema=$SourceSchema `
    -v source_role=$SourceRole `
    -c "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :'source_schema') AS source_schema_exists, EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'source_role') AS source_role_exists;" |
    Tee-Object (Join-Path $EvidenceRoot "07-source-database-absence.txt")

az keyvault secret show `
    --vault-name $AzureKeyVault `
    --name $SourceSecret `
    --query id `
    --output tsv 2>&1 |
    Tee-Object (Join-Path $EvidenceRoot "07-source-secret-absence.txt")
if ($LASTEXITCODE -eq 0) { throw "Source secret still exists" }

uv run python -m scripts.maintenance.audit_shared_tenant_readiness `
    --database `
    --schema tenant_data `
    --runtime-role $RuntimeRole `
    --json |
    Tee-Object (Join-Path $EvidenceRoot "07-database-audit-after-deletion.json")

uv run python -m scripts.maintenance.manage_shared_tenant_data verify `
    --organisation-id $OrganisationId `
    --manifest $TenantExport |
    Tee-Object (Join-Path $EvidenceRoot "07-tenant-parity-after-deletion.json")

rg -n "get_schema_name\(|get_role_name\(|get_key_vault_secret_name\(" `
    src/api src/infra/database src/service src/workers `
    -g "!shared_tenancy_cutover_service.py" |
    Tee-Object (Join-Path $EvidenceRoot "07-runtime-legacy-reference-search.txt")
```

Required result: both database booleans are false, Key Vault returns not found,
the database audit passes, export parity passes, and the static search is empty.
Hash and upload the full evidence set before reopening traffic.

## 12. Reopen the permanent runtime

Restore the environment's normal values while preserving pipeline-injected
secret values:

```powershell
helm upgrade $Release charts/application `
    --namespace $Namespace `
    --kube-context $KubeContext `
    --reuse-values `
    -f $ValuesFile `
    --set "api.image.tag=$ImageTag" `
    --set "knowledge-extract-worker.image.tag=$ImageTag" `
    --set "knowledge-upload-worker.image.tag=$ImageTag" `
    --set "organization-worker.image.tag=$ImageTag" `
    --set "chat-worker.image.tag=$ImageTag" `
    --set "chat-beat.image.tag=$ImageTag" `
    --set "integrations-worker.image.tag=$ImageTag" `
    --set "deal-worker.image.tag=$ImageTag" `
    --set "deal-insights-worker.image.tag=$ImageTag" `
    --wait `
    --timeout 20m

kubectl --context $KubeContext -n $Namespace rollout status deployment/$ApiDeployment --timeout=10m
kubectl --context $KubeContext -n $Namespace get deploy,pods,cronjob,scaledobject -o wide |
    Tee-Object (Join-Path $EvidenceRoot "08-runtime-reopened.txt")
```

Confirm `/health`, master login, every smoke endpoint, one representative task
per active worker family, and telemetry/alerts. Do not create a production test
organisation unless that exact mutation is included in the approved change.

## 13. Disaster recovery into `tenant_data`

The tenant archive is organisation-bound, schema-bound, revision-bound, and
checksum verified.

Verify an archive without changing data:

```powershell
uv run python -m scripts.maintenance.manage_shared_tenant_data verify `
    --organisation-id $OrganisationId `
    --manifest $TenantExport
```

For an approved delete/restore drill, first obtain the state-bound token from a
dry run and store only the structured report:

```powershell
$DeletePreview = uv run python -m scripts.maintenance.manage_shared_tenant_data delete `
    --organisation-id $OrganisationId |
    Tee-Object (Join-Path $EvidenceRoot "dr-delete-preview.json") |
    ConvertFrom-Json
$DeleteApprovalToken = $DeletePreview.approval_token
if (-not $DeleteApprovalToken) { throw "Delete preview did not return an approval token" }

uv run python -m scripts.maintenance.manage_shared_tenant_data delete `
    --organisation-id $OrganisationId `
    --approval-token $DeleteApprovalToken `
    --apply

uv run python -m scripts.maintenance.manage_shared_tenant_data restore `
    --organisation-id $OrganisationId `
    --manifest $TenantExport `
    --apply

uv run python -m scripts.maintenance.manage_shared_tenant_data verify `
    --organisation-id $OrganisationId `
    --manifest $TenantExport
```

If rows already exist and an approved replacement is required:

```powershell
$ReplacePreview = uv run python -m scripts.maintenance.manage_shared_tenant_data delete `
    --organisation-id $OrganisationId |
    Tee-Object (Join-Path $EvidenceRoot "dr-replace-preview.json") |
    ConvertFrom-Json
$ReplaceApprovalToken = $ReplacePreview.approval_token
if (-not $ReplaceApprovalToken) { throw "Replacement preview did not return an approval token" }

uv run python -m scripts.maintenance.manage_shared_tenant_data restore `
    --organisation-id $OrganisationId `
    --manifest $TenantExport `
    --replace-approval-token $ReplaceApprovalToken `
    --apply
```

Never restore an organisation archive under another UUID and never restore it
into `public` or an `org_*` schema.

## 14. Required alerts and proof

Application logs are structured and row-free. Configure the following rules in
the deployed Grafana/Loki stack before Task 16. Export the final rule JSON/YAML,
hash it into the evidence root, and fire a test notification through the real
contact point.

| Signal | Query/selector | Initial gate |
|---|---|---|
| Tenant context rejected | `{namespace="copilotgtm"} \| json \| event="shared_tenancy_context" \| outcome="failed"` | Page on any event |
| RLS violation | `{namespace="copilotgtm"} \| json \| event="shared_tenancy_database" \| failure_kind="rls_violation"` | Page on any event |
| Pool wait exhausted | `{namespace="copilotgtm"} \| json \| event="shared_tenancy_database" \| failure_kind="pool_timeout"` | Page on any event in 5 minutes |
| Deadlock/lock/statement timeout | Same event with `failure_kind` equal to `deadlock`, `lock_timeout`, or `statement_timeout` | Page on any deadlock; warn on timeout, page if repeated |
| Startup gate failed | `{namespace="copilotgtm"} \| json \| event="shared_tenancy_startup_gate" \| outcome="failed"` | Page on any event and block rollout |
| Copy/validation failed | `{namespace="copilotgtm"} \| json \| event="shared_tenancy_cutover" \| outcome="failed"` | Page on any event and stop cutover |
| Lifecycle failure | `{namespace="copilotgtm"} \| json \| event="shared_tenancy_lifecycle" \| outcome="failed"` | Page on any destructive-operation failure |
| API latency | `copilotgtm.http.server.request.duration` p95 grouped by route | Warn for 10 minutes above the approved Task 16 baseline/limit |
| Celery latency | `copilotgtm.celery.task.duration` p95 grouped by task and queue | Warn for 10 minutes above the approved Task 16 baseline/limit |

Metric-backend syntax depends on the OTLP/Grafana data source; the metric names
and dimensions above are the contract. Do not invent a second application
metrics stack.

Configure these read-only PostgreSQL checks from the database monitoring
identity:

```sql
-- Deadlocks: page when the delta is greater than zero.
SELECT datname, deadlocks
FROM pg_stat_database
WHERE datname = current_database();

-- Blocking locks: page when a blocked transaction is older than 60 seconds.
SELECT blocked.pid,
       now() - blocked.query_start AS blocked_for,
       pg_blocking_pids(blocked.pid) AS blocking_pids
FROM pg_stat_activity AS blocked
WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0
  AND now() - blocked.query_start > interval '60 seconds';

-- Autovacuum lag: warn only when both the absolute and ratio gates are met.
SELECT schemaname,
       relname,
       n_live_tup,
       n_dead_tup,
       last_autovacuum
FROM pg_stat_user_tables
WHERE schemaname = 'tenant_data'
  AND n_dead_tup > 100000
  AND n_dead_tup::numeric / greatest(n_live_tup + n_dead_tup, 1) > 0.20
  AND coalesce(last_autovacuum, '-infinity'::timestamptz) < now() - interval '24 hours';

-- Daily table/index growth: warn above both 20 percent and 1 GiB.
SELECT schemaname,
       relname,
       pg_total_relation_size(relid) AS total_bytes,
       pg_indexes_size(relid) AS index_bytes
FROM pg_stat_user_tables
WHERE schemaname = 'tenant_data'
ORDER BY total_bytes DESC;
```

Store a daily size baseline so the monitoring rule can calculate the growth
delta. Before reopening production traffic, prove all alert rules are enabled,
their contact points are healthy, and the test notification is acknowledged.

## 15. Completion evidence

The clean break is complete only when one evidence manifest contains SHA-256
hashes and immutable locations for:

- Git SHA and approved image digest;
- model and live readiness audits;
- reviewed Terraform plan and `tenant_data` state;
- explicit-schema migration output;
- Azure restore proof and tenant export;
- API/Celery/queue/database writer-stop proof;
- atomic copy/parity report;
- permanent runtime, master/org/API/workflow/RLS smoke evidence;
- exact source deletion report;
- post-deletion schema, role, secret, static-reference, parity, and RLS proof;
- reopened API/workers proof;
- exported alert rules and acknowledged test notification;
- Task 16 p95 baseline and maintenance-window duration.

If any item is missing, indirect, mutable, or inconsistent, the task is not
complete.
