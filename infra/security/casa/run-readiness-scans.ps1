param(
    [string]$OutputRoot = "infra/security/casa/evidence/runs",
    [string]$ProdApiBaseUrl = "https://api.copilotgtm.com",
    [string]$ProdAppUrl = "https://app.copilotgtm.com",
    [string]$SecretsBaseline = ".secrets.baseline",
    [string[]]$ImageRefs = @(),
    [switch]$RunDast,
    [switch]$RunTrivy,
    [switch]$RunPipAudit,
    [switch]$RunIacScan,
    [switch]$PlanOnly
)

$ErrorActionPreference = "Continue"
$OutputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = $OutputEncoding
$repoRoot = (Resolve-Path ".").Path
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$runDir = Join-Path $repoRoot (Join-Path $OutputRoot $timestamp)
$configDir = Join-Path $runDir "configs"
$script:Results = @()
$script:DetectSecretsExcludeFiles = "(^|[\\/])\.secrets\.baseline$|^infra[\\/]security[\\/]casa[\\/]evidence[\\/]"
$script:IacScanAvailable = $false

function New-RunDirectory {
    New-Item -ItemType Directory -Path $configDir -Force | Out-Null
}

function Write-RunInfo {
    $scopePath = Join-Path $runDir "00-scope.txt"
    @(
        "CASA production readiness scan"
        "GeneratedAt=$((Get-Date).ToUniversalTime().ToString("o"))"
        "RepoRoot=$repoRoot"
        "GitCommit=$(git rev-parse HEAD 2>$null)"
        "ProdApiBaseUrl=$ProdApiBaseUrl"
        "ProdAppUrl=$ProdAppUrl"
        "RunDast=$RunDast"
        "RunTrivy=$RunTrivy"
        "RunPipAudit=$RunPipAudit"
        "RunIacScan=$RunIacScan"
        "ImageRefs=$($ImageRefs -join ',')"
        "SecretsBaseline=$SecretsBaseline"
    ) | Set-Content -Path $scopePath -Encoding UTF8
}

function Copy-ConfigEvidence {
    Copy-Item -Path "pyproject.toml" -Destination (Join-Path $configDir "pyproject.toml") -Force
    if (Test-Path "uv.lock") {
        Copy-Item -Path "uv.lock" -Destination (Join-Path $configDir "uv.lock") -Force
    }
    Copy-Item -Path "Dockerfile" -Destination (Join-Path $configDir "Dockerfile") -Force
    Copy-Item -Path ".azure-pipelines/azure-backend-pipeline-prod.yml" -Destination (Join-Path $configDir "azure-backend-pipeline-prod.yml") -Force
    Copy-Item -Path "infra/terraform/azure/prod/main.tf" -Destination (Join-Path $configDir "terraform-prod-main.tf") -Force
    Copy-Item -Path "infra/terraform/azure/prod/variables.tf" -Destination (Join-Path $configDir "terraform-prod-variables.tf") -Force
    Copy-Item -Path "infra/terraform/azure/prod/terraform.tfvars" -Destination (Join-Path $configDir "terraform-prod-tfvars.txt") -Force
}

function Invoke-CasaCommand {
    param(
        [string]$Name,
        [string]$OutputFile,
        [scriptblock]$Command
    )

    $path = Join-Path $runDir $OutputFile
    "### $Name" | Set-Content -Path $path -Encoding UTF8
    "StartedAt=$((Get-Date).ToUniversalTime().ToString("o"))" | Add-Content -Path $path -Encoding UTF8
    "" | Add-Content -Path $path -Encoding UTF8

    if ($PlanOnly) {
        "PlanOnly=true; command not executed." | Add-Content -Path $path -Encoding UTF8
        $script:Results += [pscustomobject]@{
            Name = $Name
            OutputFile = $OutputFile
            ExitCode = "SKIPPED"
        }
        return
    }

    $output = & $Command 2>&1
    $exitCode = if ($LASTEXITCODE -ne $null) { $LASTEXITCODE } else { 0 }
    foreach ($line in $output) {
        $line.ToString() | Add-Content -Path $path -Encoding UTF8
    }
    "" | Add-Content -Path $path -Encoding UTF8
    "ExitCode=$exitCode" | Add-Content -Path $path -Encoding UTF8
    $script:Results += [pscustomobject]@{
        Name = $Name
        OutputFile = $OutputFile
        ExitCode = $exitCode
    }
}

function Invoke-DetectSecretsBaselineCheck {
    param([string]$CurrentScanPath = "")

    if (!(Test-Path $SecretsBaseline)) {
        Write-Output "Missing secrets baseline: $SecretsBaseline"
        exit 2
    }

    if (!$CurrentScanPath -or !(Test-Path $CurrentScanPath)) {
        $CurrentScanPath = Join-Path $runDir "03-detect-secrets-current.json"
        $scanLines = uv run detect-secrets scan --exclude-files $script:DetectSecretsExcludeFiles 2>&1
        if ($LASTEXITCODE -ne 0) {
            $scanLines
            exit $LASTEXITCODE
        }
        $scanLines | Set-Content -Path $CurrentScanPath -Encoding UTF8
    }

    uv run python scripts/security/check_detect_secrets_baseline.py --baseline $SecretsBaseline --current-json $CurrentScanPath
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

function Write-DetectSecretsBaselineSummary {
    if (!(Test-Path $SecretsBaseline)) {
        Write-Output "Missing secrets baseline: $SecretsBaseline"
        exit 2
    }

    $baselineJson = Get-Content $SecretsBaseline -Raw | ConvertFrom-Json
    $rows = foreach ($fileProperty in $baselineJson.results.PSObject.Properties) {
        foreach ($finding in $fileProperty.Value) {
            $top = ($fileProperty.Name -split "[\\/]")[0]
            [pscustomobject]@{
                Top = $top
                File = $fileProperty.Name
                Type = $finding.type
                Line = $finding.line_number
            }
        }
    }

    Write-Output "Baseline finding count: $($rows.Count)"
    Write-Output ""
    Write-Output "By top-level path:"
    $rows | Group-Object Top | Sort-Object Count -Descending | ForEach-Object {
        Write-Output ("{0}`t{1}" -f $_.Count, $_.Name)
    }
    Write-Output ""
    Write-Output "By detector type:"
    $rows | Group-Object Type | Sort-Object Count -Descending | ForEach-Object {
        Write-Output ("{0}`t{1}" -f $_.Count, $_.Name)
    }
    Write-Output ""
    Write-Output "Production-sensitive candidates to audit first:"
    $rows |
        Where-Object { $_.Top -in @("charts", "infra", "scripts", "src") } |
        Sort-Object File, Line |
        ForEach-Object {
            Write-Output ("{0}`t{1}`t{2}" -f $_.File, $_.Line, $_.Type)
        }
}

function Write-Summary {
    $summaryPath = Join-Path $runDir "99-summary.txt"
    "CASA readiness scan summary" | Set-Content -Path $summaryPath -Encoding UTF8
    "GeneratedAt=$((Get-Date).ToUniversalTime().ToString("o"))" | Add-Content -Path $summaryPath -Encoding UTF8
    "" | Add-Content -Path $summaryPath -Encoding UTF8
    foreach ($result in $script:Results) {
        "$($result.ExitCode)`t$($result.OutputFile)`t$($result.Name)" | Add-Content -Path $summaryPath -Encoding UTF8
    }
    "" | Add-Content -Path $summaryPath -Encoding UTF8
    "Note: Non-zero scanner exit codes can mean findings or tool/runtime failure. Review the matching output file before treating a scan as failed." | Add-Content -Path $summaryPath -Encoding UTF8
}

New-RunDirectory
Write-RunInfo
Copy-ConfigEvidence

Invoke-CasaCommand "Ruff security/lint rules" "01-ruff-security.txt" {
    uv run ruff check src/ tests/
}

Invoke-CasaCommand "Bandit SAST" "02-bandit-sast.txt" {
    uv run bandit -c pyproject.toml -r src/ -f txt
}

Invoke-CasaCommand "Detect-secrets scan" "03-detect-secrets.txt" {
    uv run detect-secrets scan --exclude-files $script:DetectSecretsExcludeFiles
}

Invoke-CasaCommand "Detect-secrets baseline check" "03-detect-secrets-baseline-check.txt" {
    Invoke-DetectSecretsBaselineCheck -CurrentScanPath (Join-Path $runDir "03-detect-secrets.txt")
}

Invoke-CasaCommand "Detect-secrets baseline summary" "03-detect-secrets-baseline-summary.txt" {
    Write-DetectSecretsBaselineSummary
}

Invoke-CasaCommand "Terraform prod validate" "04-terraform-prod-validate.txt" {
    terraform -chdir=infra/terraform/azure/prod validate
}

Invoke-CasaCommand "Terraform format check" "05-terraform-prod-fmt-check.txt" {
    terraform fmt -check -recursive infra/terraform/azure
}

Invoke-CasaCommand "Terraform Trivy config scan status" "06-iac-trivy-config-status.txt" {
    if (!$RunIacScan) {
        Write-Output "RunIacScan=false; Terraform IaC security scan not requested for this run."
        Write-Output "Install Trivy or use the prod pipeline Trivy reports, then rerun with -RunIacScan for assessor-facing IaC evidence."
        return
    }
    $trivy = Get-Command trivy -ErrorAction SilentlyContinue
    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if (!$trivy -and !$docker) {
        Write-Output "RunIacScan=true, but neither trivy nor docker is installed on this machine."
        Write-Output "Install Trivy, or run from an environment with Docker so the aquasec/trivy container can be used."
        $global:LASTEXITCODE = 2
        return
    }
    if ($trivy) {
        Write-Output "RunIacScan=true; local Trivy is available for Terraform IaC security scan."
        $script:IacScanAvailable = $true
    } else {
        $dockerInfo = docker info --format "{{.ServerVersion}}" 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Output "RunIacScan=true, but Docker is not running or cannot reach a Linux engine."
            $dockerInfo
            $global:LASTEXITCODE = 2
            return
        }
        Write-Output "RunIacScan=true; Docker fallback is available for Terraform IaC security scan."
        $script:IacScanAvailable = $true
    }
}

if ($RunIacScan -and $script:IacScanAvailable) {
    Invoke-CasaCommand "Terraform Trivy config scan" "06-iac-trivy-config.txt" {
        $trivy = Get-Command trivy -ErrorAction SilentlyContinue
        if ($trivy) {
            trivy config --severity CRITICAL,HIGH --no-progress --format table infra/terraform/azure/prod
            return
        }

        docker run --rm `
            -v "${repoRoot}:/repo" `
            -w /repo `
            aquasec/trivy:0.49.1 `
            config --severity CRITICAL,HIGH --no-progress --format table infra/terraform/azure/prod
    }
}

Invoke-CasaCommand "Python locked dependency tree" "06-python-dependency-tree.txt" {
    uv tree --locked --no-dev --universal
}

Invoke-CasaCommand "Python locked requirements export" "06-python-requirements.txt" {
    uv export --format requirements.txt --locked --no-dev --no-emit-project --no-header
}

Invoke-CasaCommand "Python dependency vulnerability scan status" "06-pip-audit-status.txt" {
    $pipAudit = Get-Command pip-audit -ErrorAction SilentlyContinue
    if (!$RunPipAudit) {
        Write-Output "RunPipAudit=false; pip-audit vulnerability scan not requested for this run."
        Write-Output "Install pip-audit or run it in CI, then rerun with -RunPipAudit for assessor-facing SCA evidence."
        return
    }
    if (!$pipAudit) {
        Write-Output "RunPipAudit=true, but pip-audit is not installed on this machine."
        Write-Output "Install pip-audit in the runner image or export CI SCA results before CASA submission."
        exit 2
    }
    pip-audit --requirement (Join-Path $runDir "06-python-requirements.txt") --progress-spinner off --format columns
}

if ($RunTrivy) {
    if ($ImageRefs.Count -eq 0) {
        Invoke-CasaCommand "Trivy image scan" "07-trivy-missing-images.txt" {
            Write-Output "RunTrivy was set, but no -ImageRefs were provided."
            exit 2
        }
    }

    foreach ($imageRef in $ImageRefs) {
        $safeName = ($imageRef -replace "[^a-zA-Z0-9_.-]", "_")
        Invoke-CasaCommand "Trivy image scan: $imageRef" "07-trivy-$safeName.txt" {
            trivy image --severity CRITICAL,HIGH --ignore-unfixed --no-progress --format table $imageRef
        }
    }
}

if ($RunDast) {
    Invoke-CasaCommand "Health endpoint security check" "08-dast-health-security.txt" {
        uv run python scripts/security/validate_health_security.py `
            --base-url $ProdApiBaseUrl `
            --text-output (Join-Path $runDir "08-dast-health-security-report.txt") `
            --json-output (Join-Path $runDir "08-dast-health-security-report.json")
    }

    Invoke-CasaCommand "OWASP ZAP baseline placeholder" "08-dast-zap-baseline.txt" {
        Write-Output "Run OWASP ZAP baseline from an approved production testing window."
        Write-Output "Example:"
        Write-Output "docker run --rm -t ghcr.io/zaproxy/zaproxy:stable zap-baseline.py -t $ProdAppUrl -r zap-report.html"
    }
}

Write-Summary
Write-Output "CASA readiness run created: $runDir"
