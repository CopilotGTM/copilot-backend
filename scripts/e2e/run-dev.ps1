[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("auth", "crm-security", "gmail-sync")]
    [string]$Suite = "auth"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$vaultName = "copilot-master-kv-dev"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$environmentNames = @(
    "E2E_API_BASE_URL",
    "E2E_TARGET_ENVIRONMENT",
    "E2E_ALLOWED_MUTATION_HOSTS",
    "E2E_USER_EMAIL",
    "E2E_USER_PASSWORD",
    "E2E_REQUIRE_CONFIG",
    "E2E_CRM_ENABLE_LIVE_MUTATION",
    "E2E_CRM_OWNER_A_EMAIL",
    "E2E_CRM_OWNER_A_PASSWORD",
    "E2E_CRM_COLLABORATOR_B_EMAIL",
    "E2E_CRM_COLLABORATOR_B_PASSWORD",
    "E2E_CRM_MEMBER_C_EMAIL",
    "E2E_CRM_MEMBER_C_PASSWORD",
    "E2E_CRM_FOREIGN_D_EMAIL",
    "E2E_CRM_FOREIGN_D_PASSWORD",
    "E2E_CRM_OWNER_ORGANISATION_ID",
    "E2E_CRM_PRIMARY_DEAL_ID",
    "E2E_CRM_PRIMARY_EVENT_ID",
    "E2E_CRM_SECONDARY_EVENT_ID",
    "E2E_CRM_DATABASE_URL",
    "E2E_GMAIL_ENABLE_LIVE_MUTATION",
    "E2E_GMAIL_ORGANISATION_ID",
    "E2E_GMAIL_INTEGRATION_ID",
    "E2E_GMAIL_DATABASE_URL",
    "E2E_INTEGRATIONS_BROKER_URL",
    "E2E_ALLOWED_BROKER_HOSTS",
    "E2E_GMAIL_SEEDER_CLIENT_ID",
    "E2E_GMAIL_SEEDER_CLIENT_SECRET",
    "E2E_GMAIL_SEEDER_REFRESH_TOKEN",
    "E2E_GMAIL_SEEDER_EMAIL"
)
$previousEnvironment = @{}
foreach ($name in $environmentNames) {
    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}
$locationChanged = $false
$testExitCode = 1
$testPath = switch ($Suite) {
    "auth" { "e2e/test_auth.py" }
    "crm-security" { "e2e/crm_security" }
    "gmail-sync" { "e2e/gmail_sync" }
}

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

    $env:E2E_API_BASE_URL = "https://devapi.copilotgtm.com"
    $env:E2E_TARGET_ENVIRONMENT = "dev"
    $env:E2E_ALLOWED_MUTATION_HOSTS = "devapi.copilotgtm.com"
    $env:E2E_REQUIRE_CONFIG = "1"

    if ($Suite -eq "auth") {
        $env:E2E_USER_EMAIL = Get-RequiredKeyVaultSecret -Name "e2e-user-email"
        $env:E2E_USER_PASSWORD = Get-RequiredKeyVaultSecret -Name "e2e-user-password"
    }
    elseif ($Suite -eq "crm-security") {
        $env:E2E_CRM_ENABLE_LIVE_MUTATION = "true"
        $secretMappings = @{
            E2E_CRM_OWNER_A_EMAIL = "e2e-crm-owner-a-email"
            E2E_CRM_OWNER_A_PASSWORD = "e2e-crm-owner-a-password"
            E2E_CRM_COLLABORATOR_B_EMAIL = "e2e-crm-collaborator-b-email"
            E2E_CRM_COLLABORATOR_B_PASSWORD = "e2e-crm-collaborator-b-password"
            E2E_CRM_MEMBER_C_EMAIL = "e2e-crm-member-c-email"
            E2E_CRM_MEMBER_C_PASSWORD = "e2e-crm-member-c-password"
            E2E_CRM_FOREIGN_D_EMAIL = "e2e-crm-foreign-d-email"
            E2E_CRM_FOREIGN_D_PASSWORD = "e2e-crm-foreign-d-password"
            E2E_CRM_OWNER_ORGANISATION_ID = "e2e-crm-owner-organisation-id"
            E2E_CRM_PRIMARY_DEAL_ID = "e2e-crm-primary-deal-id"
            E2E_CRM_PRIMARY_EVENT_ID = "e2e-crm-primary-event-id"
            E2E_CRM_SECONDARY_EVENT_ID = "e2e-crm-secondary-event-id"
            E2E_CRM_DATABASE_URL = "e2e-crm-readonly-database-url"
        }
        foreach ($mapping in $secretMappings.GetEnumerator()) {
            [Environment]::SetEnvironmentVariable(
                $mapping.Key,
                (Get-RequiredKeyVaultSecret -Name $mapping.Value),
                "Process"
            )
        }
    }
    elseif ($Suite -eq "gmail-sync") {
        $env:E2E_GMAIL_ENABLE_LIVE_MUTATION = "true"
        $secretMappings = @{
            E2E_GMAIL_ORGANISATION_ID = "e2e-gmail-organisation-id"
            E2E_GMAIL_INTEGRATION_ID = "e2e-gmail-integration-id"
            E2E_GMAIL_DATABASE_URL = "e2e-gmail-readonly-database-url"
            E2E_INTEGRATIONS_BROKER_URL = "e2e-dev-integrations-broker-url"
            E2E_ALLOWED_BROKER_HOSTS = "e2e-dev-integrations-broker-host"
            E2E_GMAIL_SEEDER_CLIENT_ID = "e2e-gmail-seeder-client-id"
            E2E_GMAIL_SEEDER_CLIENT_SECRET = "e2e-gmail-seeder-client-secret"
            E2E_GMAIL_SEEDER_REFRESH_TOKEN = "e2e-gmail-seeder-refresh-token"
            E2E_GMAIL_SEEDER_EMAIL = "e2e-gmail-seeder-email"
        }
        foreach ($mapping in $secretMappings.GetEnumerator()) {
            [Environment]::SetEnvironmentVariable(
                $mapping.Key,
                (Get-RequiredKeyVaultSecret -Name $mapping.Value),
                "Process"
            )
        }
    }

    Push-Location $repoRoot
    $locationChanged = $true
    & uv run --no-project --with pytest --with httpx --with psycopg2-binary --with celery `
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
}

exit $testExitCode
