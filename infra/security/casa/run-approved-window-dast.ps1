param(
    [string]$ProdApiBaseUrl = "https://api.copilotgtm.com",
    [string]$ProdAppUrl = "https://app.copilotgtm.com",
    [string]$OutputRoot = "infra/security/casa/evidence/dast-runs",
    [switch]$ApprovedWindow,
    [switch]$RunZap
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = $OutputEncoding

if (!$ApprovedWindow) {
    throw "ApprovedWindow is required. Run production DAST only during an approved testing window."
}

$repoRoot = (Resolve-Path ".").Path
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$runDir = Join-Path $repoRoot (Join-Path $OutputRoot $timestamp)
New-Item -ItemType Directory -Path $runDir -Force | Out-Null

$manifestPath = Join-Path $runDir "dast-manifest.txt"
@(
    "CASA approved-window DAST evidence"
    "GeneratedAt=$((Get-Date).ToUniversalTime().ToString("o"))"
    "ProdApiBaseUrl=$ProdApiBaseUrl"
    "ProdAppUrl=$ProdAppUrl"
    "RunZap=$RunZap"
    ""
) | Set-Content -Path $manifestPath -Encoding UTF8

$healthTextPath = Join-Path $runDir "08-dast-health-security-report.txt"
$healthJsonPath = Join-Path $runDir "08-dast-health-security-report.json"

"Running health endpoint DAST smoke check" | Add-Content -Path $manifestPath -Encoding UTF8
uv run python scripts/security/validate_health_security.py `
    --base-url $ProdApiBaseUrl `
    --text-output $healthTextPath `
    --json-output $healthJsonPath

if ($LASTEXITCODE -ne 0) {
    "Health endpoint DAST smoke check failed with exit code $LASTEXITCODE" |
        Add-Content -Path $manifestPath -Encoding UTF8
    exit $LASTEXITCODE
}

if ($RunZap) {
    $zapOutputPath = Join-Path $runDir "08-dast-zap-baseline.txt"
    "Running OWASP ZAP baseline scan" | Add-Content -Path $manifestPath -Encoding UTF8
    docker run --rm `
        -v "${runDir}:/zap/wrk/:rw" `
        ghcr.io/zaproxy/zaproxy:stable `
        zap-baseline.py `
        -t $ProdAppUrl `
        -r "08-dast-zap-baseline.html" `
        -J "08-dast-zap-baseline.json" `
        2>&1 | Tee-Object -FilePath $zapOutputPath

    if ($LASTEXITCODE -ne 0) {
        "OWASP ZAP baseline scan exited with code $LASTEXITCODE; review 08-dast-zap-baseline.txt for findings." |
            Add-Content -Path $manifestPath -Encoding UTF8
    }
} else {
    "RunZap=false; OWASP ZAP baseline was not requested." | Add-Content -Path $manifestPath -Encoding UTF8
}

"CompletedAt=$((Get-Date).ToUniversalTime().ToString("o"))" | Add-Content -Path $manifestPath -Encoding UTF8
Write-Output "CASA DAST evidence exported to: $runDir"
