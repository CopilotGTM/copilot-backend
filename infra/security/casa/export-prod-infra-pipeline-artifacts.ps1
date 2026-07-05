param(
    [Parameter(Mandatory = $true)]
    [string]$RunId,
    [string]$Organization = "https://dev.azure.com/copilotgtm",
    [string]$Project = "app-copilotgtm",
    [string]$OutputRoot = "infra/security/casa/evidence/infra-pipeline-runs"
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = $OutputEncoding

$artifactNames = @(
    "terraform-iac-scan"
)

$publishedArtifactNames = @(
    az pipelines runs artifact list `
        --organization $Organization `
        --project $Project `
        --run-id $RunId `
        --query "[].name" `
        -o tsv
)

foreach ($artifactName in $artifactNames) {
    if ($artifactName -notin $publishedArtifactNames) {
        throw "Missing required infra pipeline artifact '$artifactName' on run $RunId. Run the updated prod infra pipeline before exporting CASA evidence."
    }
}

$repoRoot = (Resolve-Path ".").Path
$runDir = Join-Path $repoRoot (Join-Path $OutputRoot $RunId)
New-Item -ItemType Directory -Path $runDir -Force | Out-Null

$manifestPath = Join-Path $runDir "manifest.txt"
@(
    "CASA prod infra pipeline artifact export"
    "GeneratedAt=$((Get-Date).ToUniversalTime().ToString("o"))"
    "RunId=$RunId"
    "Organization=$Organization"
    "Project=$Project"
    "Artifacts=$($artifactNames -join ',')"
    ""
) | Set-Content -Path $manifestPath -Encoding UTF8

foreach ($artifactName in $artifactNames) {
    $artifactPath = Join-Path $runDir $artifactName
    New-Item -ItemType Directory -Path $artifactPath -Force | Out-Null

    "Downloading $artifactName" | Add-Content -Path $manifestPath -Encoding UTF8
    az pipelines runs artifact download `
        --organization $Organization `
        --project $Project `
        --run-id $RunId `
        --artifact-name $artifactName `
        --path $artifactPath
}

"CompletedAt=$((Get-Date).ToUniversalTime().ToString("o"))" | Add-Content -Path $manifestPath -Encoding UTF8
Write-Output "CASA prod infra pipeline artifacts exported to: $runDir"
