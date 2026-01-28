# Teams App Manifest Build Script
# Usage: .\build-package.ps1 -BotAppId "your-bot-app-id-guid"

param(
    [Parameter(Mandatory=$true)]
    [string]$BotAppId
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$manifestPath = Join-Path $scriptDir "manifest.json"
$outputZip = Join-Path $scriptDir "copilotgtm-teams-app.zip"

# Read and update manifest
Write-Host "Updating manifest with Bot App ID: $BotAppId"
$manifest = Get-Content $manifestPath -Raw
$manifest = $manifest -replace '\{\{BOT_APP_ID\}\}', $BotAppId
$manifest | Set-Content $manifestPath -NoNewline

# Remove old zip if exists
if (Test-Path $outputZip) {
    Remove-Item $outputZip
}

# Create zip package
Write-Host "Creating Teams app package..."
$filesToZip = @(
    (Join-Path $scriptDir "manifest.json"),
    (Join-Path $scriptDir "color.png"),
    (Join-Path $scriptDir "outline.png")
)

Compress-Archive -Path $filesToZip -DestinationPath $outputZip

Write-Host ""
Write-Host "SUCCESS! Teams app package created: $outputZip"
Write-Host ""
Write-Host "Next steps:"
Write-Host "1. Go to Microsoft Teams"
Write-Host "2. Click 'Apps' in the left sidebar"
Write-Host "3. Click 'Manage your apps' at the bottom"
Write-Host "4. Click 'Upload an app' -> 'Upload a custom app'"
Write-Host "5. Select: $outputZip"
