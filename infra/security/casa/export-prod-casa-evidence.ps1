param(
    [string]$BackendRunId,
    [string]$InfraRunId,
    [switch]$UseLatestSuccessfulRuns,
    [string]$Organization = "https://dev.azure.com/copilotgtm",
    [string]$Project = "app-copilotgtm"
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = $OutputEncoding

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../../..")).Path

function Get-LatestSuccessfulRunWithArtifacts {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PipelineName,
        [Parameter(Mandatory = $true)]
        [string[]]$RequiredArtifacts
    )

    $pipelineId = az pipelines list `
        --organization $Organization `
        --project $Project `
        --query "[?name=='$PipelineName'] | [0].id" `
        -o tsv

    if ([string]::IsNullOrWhiteSpace($pipelineId)) {
        throw "Azure DevOps pipeline not found: $PipelineName"
    }

    $runs = az pipelines runs list `
        --organization $Organization `
        --project $Project `
        --pipeline-ids $pipelineId `
        --result succeeded `
        --status completed `
        --top 10 `
        --query "[].{id:id,buildNumber:buildNumber,finishTime:finishTime}" `
        -o json | ConvertFrom-Json

    foreach ($run in $runs) {
        $artifactNames = @(
            az pipelines runs artifact list `
                --organization $Organization `
                --project $Project `
                --run-id $run.id `
                --query "[].name" `
                -o tsv
        )

        $missingArtifacts = @($RequiredArtifacts | Where-Object { $_ -notin $artifactNames })
        if ($missingArtifacts.Count -eq 0) {
            Write-Output "Selected $PipelineName run $($run.id) ($($run.buildNumber), finished $($run.finishTime))."
            return [string]$run.id
        }
    }

    throw "No recent successful $PipelineName run contains required artifacts: $($RequiredArtifacts -join ', '). Run the updated pipeline first."
}

function Invoke-CasaEvidenceExport {
    if ($UseLatestSuccessfulRuns) {
        $script:BackendRunId = Get-LatestSuccessfulRunWithArtifacts `
            -PipelineName "copilot-backend-pipeline-prod" `
            -RequiredArtifacts @("secrets-scan-reports", "python-sca-reports", "trivy-reports", "security-report")

        $script:InfraRunId = Get-LatestSuccessfulRunWithArtifacts `
            -PipelineName "copilot-infra-pipeline-prod" `
            -RequiredArtifacts @("terraform-iac-scan")
    }

    if ([string]::IsNullOrWhiteSpace($BackendRunId) -or [string]::IsNullOrWhiteSpace($InfraRunId)) {
        throw "Provide -BackendRunId and -InfraRunId, or pass -UseLatestSuccessfulRuns after updated prod pipeline runs finish."
    }

    Push-Location $repoRoot
    try {
        & (Join-Path $PSScriptRoot "export-prod-pipeline-artifacts.ps1") `
            -RunId $BackendRunId `
            -Organization $Organization `
            -Project $Project

        & (Join-Path $PSScriptRoot "export-prod-infra-pipeline-artifacts.ps1") `
            -RunId $InfraRunId `
            -Organization $Organization `
            -Project $Project

        uv run python scripts/security/check_casa_submission_preflight.py `
            --output infra/security/casa/evidence/submission-preflight.txt
        if ($LASTEXITCODE -ne 0) {
            throw "CASA preflight still has unresolved blockers. See infra/security/casa/evidence/submission-preflight.txt."
        }
    }
    finally {
        Pop-Location
    }
}

try {
    Invoke-CasaEvidenceExport
}
catch {
    [Console]::Error.WriteLine("ERROR: $($_.Exception.Message)")
    exit 1
}
