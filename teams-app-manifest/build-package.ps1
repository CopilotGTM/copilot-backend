# Teams App Manifest Build Script (Multi-Environment)
# Each environment has its own independent manifest.json
#
# Usage:
#   .\build-package.ps1 -Environment "local" -BotAppId "your-bot-id"
#   .\build-package.ps1 -Environment "dev"
#   .\build-package.ps1 -Environment "uat"
#   .\build-package.ps1 -Environment "prod"

param(
    [Parameter(Mandatory=$true)]
    [ValidateSet("local", "dev", "uat", "prod")]
    [string]$Environment,

    [Parameter(Mandatory=$false)]
    [string]$BotAppId
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$envDir = Join-Path $scriptDir $Environment
$manifestPath = Join-Path $envDir "manifest.json"
$outputDir = Join-Path $scriptDir "output"
$outputZip = Join-Path $outputDir "copilotgtm-teams-app-$Environment.zip"

# Validate manifest exists
if (-not (Test-Path $manifestPath)) {
    Write-Error "Manifest not found: $manifestPath"
    exit 1
}

# Create output directory if it doesn't exist
if (-not (Test-Path $outputDir)) {
    New-Item -ItemType Directory -Path $outputDir | Out-Null
}

# Read manifest
$manifest = Get-Content $manifestPath -Raw

# Replace Bot App ID placeholder if provided
if ($BotAppId) {
    Write-Host "Setting Bot App ID: $BotAppId"
    $manifest = $manifest -replace '\{\{BOT_APP_ID\}\}', $BotAppId
    $manifest = $manifest -replace '\{\{REPLACE_WITH_\w+_BOT_APP_ID\}\}', $BotAppId
}

# Check for remaining placeholders
if ($manifest -match '\{\{.*?\}\}') {
    Write-Error "Manifest still contains placeholders. Pass -BotAppId or update $manifestPath directly."
    exit 1
}

# Build in temp directory
$tempDir = Join-Path $env:TEMP "teams-app-build-$Environment"
if (Test-Path $tempDir) {
    Remove-Item $tempDir -Recurse -Force
}
New-Item -ItemType Directory -Path $tempDir | Out-Null

$manifest | Set-Content (Join-Path $tempDir "manifest.json") -NoNewline

# Copy shared icons
Copy-Item (Join-Path $scriptDir "color.png") $tempDir
Copy-Item (Join-Path $scriptDir "outline.png") $tempDir

# Remove old zip if exists
if (Test-Path $outputZip) {
    Remove-Item $outputZip
}

# Create zip package
Write-Host "Creating Teams app package for: $Environment"
Compress-Archive -Path (Join-Path $tempDir "*") -DestinationPath $outputZip

# Cleanup temp
Remove-Item $tempDir -Recurse -Force

Write-Host ""
Write-Host "SUCCESS! Package created: $outputZip" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:"
Write-Host "1. Go to Microsoft Teams"
Write-Host "2. Click 'Apps' -> 'Manage your apps'"
Write-Host "3. Click 'Upload an app' -> 'Upload a custom app'"
Write-Host "4. Select: $outputZip"
