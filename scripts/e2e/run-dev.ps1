[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("auth")]
    [string]$Suite = "auth"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$vaultName = "copilot-master-kv-dev"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$environmentNames = @(
    "E2E_API_BASE_URL",
    "E2E_USER_EMAIL",
    "E2E_USER_PASSWORD",
    "E2E_REQUIRE_CONFIG"
)
$previousEnvironment = @{}
foreach ($name in $environmentNames) {
    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}
$locationChanged = $false
$testExitCode = 1
$testPath = switch ($Suite) { "auth" { "e2e/test_auth.py" } }

function Get-RequiredKeyVaultSecret {
    param(
        [Parameter(Mandatory)]
        [string]$Name
    )

    $value = & az keyvault secret show `
        --vault-name $vaultName `
        --name $Name `
        --query value `
        --output tsv `
        --only-show-errors

    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($value)) {
        throw "Unable to read required Key Vault secret: $Name"
    }

    return $value.Trim()
}

try {
    if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
        throw "Azure CLI is required. Install it and run 'az login'."
    }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "uv is required to run the E2E test environment."
    }

    & az account show --output none --only-show-errors
    if ($LASTEXITCODE -ne 0) {
        throw "Azure CLI is not authenticated. Run 'az login'."
    }

    $email = Get-RequiredKeyVaultSecret -Name "e2e-user-email"
    $password = Get-RequiredKeyVaultSecret -Name "e2e-user-password"

    $env:E2E_API_BASE_URL = "https://devapi.copilotgtm.com"
    $env:E2E_USER_EMAIL = $email
    $env:E2E_USER_PASSWORD = $password
    $env:E2E_REQUIRE_CONFIG = "1"

    Push-Location $repoRoot
    $locationChanged = $true
    & uv run --no-project --with pytest --with httpx `
        pytest $testPath -m e2e -v
    $testExitCode = $LASTEXITCODE
}
finally {
    if ($locationChanged) {
        Pop-Location
    }
    foreach ($name in $environmentNames) {
        [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], "Process")
    }
    $email = $null
    $password = $null
}

exit $testExitCode
