# =============================================================================
# Azure Docker Authentication Script
# =============================================================================
# This script sets up Azure authentication for all Docker containers.
# 
# Usage:
#   .\scripts\azure_docker_auth.ps1
#   OR from any folder in the repo:
#   path\to\scripts\azure_docker_auth.ps1
#
# What it does:
#   1. Finds the repo root (where docker-compose.yml is located)
#   2. Ensures the API container is running
#   3. Fixes permissions on the azure-data volume
#   4. Runs 'az login' inside the container
#   5. Verifies the authentication worked
#   6. Restarts all services to pick up the new credentials
# =============================================================================

$ErrorActionPreference = "Stop"

# Get the directory where this script is located
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Find the repo root (parent of scripts folder, where docker-compose.yml should be)
$RepoRoot = Split-Path -Parent $ScriptDir

# Verify we found the right directory
if (-not (Test-Path (Join-Path $RepoRoot "docker-compose.yml"))) {
    # Try to find docker-compose.yml by walking up from current directory
    $SearchDir = Get-Location
    while ($SearchDir -and -not (Test-Path (Join-Path $SearchDir "docker-compose.yml"))) {
        $SearchDir = Split-Path -Parent $SearchDir
    }
    if ($SearchDir -and (Test-Path (Join-Path $SearchDir "docker-compose.yml"))) {
        $RepoRoot = $SearchDir
    } else {
        Write-Host "ERROR: Could not find docker-compose.yml" -ForegroundColor Red
        Write-Host "Please run this script from within the copilot-backend repository." -ForegroundColor Yellow
        exit 1
    }
}

# Store original directory and change to repo root
$OriginalDir = Get-Location
Set-Location $RepoRoot

Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "  Azure Docker Authentication Setup" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Working directory: $RepoRoot" -ForegroundColor Gray
Write-Host ""

# Check if Docker is running
try {
    docker info | Out-Null
} catch {
    Write-Host "ERROR: Docker is not running. Please start Docker Desktop first." -ForegroundColor Red
    exit 1
}

# Check if copilot-api container is running
$apiContainer = docker ps --filter "name=copilot-api" --format "{{.Names}}" 2>$null
if (-not $apiContainer) {
    Write-Host "Starting containers..." -ForegroundColor Yellow
    docker-compose up -d api
    Start-Sleep -Seconds 10
}

Write-Host "[1/5] Checking container status..." -ForegroundColor Green
$apiContainer = docker ps --filter "name=copilot-api" --format "{{.Names}}" 2>$null
if (-not $apiContainer) {
    Write-Host "ERROR: copilot-api container is not running." -ForegroundColor Red
    Write-Host "Please run 'docker-compose up -d api' first." -ForegroundColor Yellow
    exit 1
}
Write-Host "      Container 'copilot-api' is running." -ForegroundColor Gray

Write-Host "[2/5] Fixing permissions on azure-data volume..." -ForegroundColor Green
docker exec -u root copilot-api sh -c "mkdir -p /home/appuser/.azure && chown -R appuser:appuser /home/appuser/.azure && chmod 755 /home/appuser/.azure"
Write-Host "      Permissions fixed." -ForegroundColor Gray

Write-Host "[3/5] Running Azure login..." -ForegroundColor Green
Write-Host ""
Write-Host "      Please follow the browser instructions to log in." -ForegroundColor Yellow
Write-Host ""

# Run az login interactively
docker exec -it copilot-api az login

# Check if login was successful
Write-Host ""
Write-Host "[4/5] Verifying authentication..." -ForegroundColor Green
$accountInfo = docker exec copilot-api az account show 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Azure login failed. Please try again." -ForegroundColor Red
    exit 1
}

Write-Host "      Successfully authenticated!" -ForegroundColor Gray
Write-Host ""
Write-Host "      Account Details:" -ForegroundColor Cyan
docker exec copilot-api az account show --query "{Name:name, User:user.name, Tenant:tenantDisplayName}" -o table

Write-Host ""
Write-Host "[5/5] Restarting services to apply credentials..." -ForegroundColor Green
docker-compose restart

Write-Host ""
Write-Host "=============================================" -ForegroundColor Green
Write-Host "  Azure Authentication Complete!" -ForegroundColor Green
Write-Host "=============================================" -ForegroundColor Green
Write-Host ""
Write-Host "All containers now have access to Azure credentials." -ForegroundColor Gray
Write-Host "The credentials are stored in the 'azure-data' Docker volume." -ForegroundColor Gray
Write-Host ""
Write-Host "If you encounter auth issues later, simply run this script again:" -ForegroundColor Yellow
Write-Host "  .\scripts\azure_docker_auth.ps1" -ForegroundColor Yellow
Write-Host ""

# Return to original directory
Set-Location $OriginalDir
