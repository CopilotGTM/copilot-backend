# =============================================================================
# Local Development Deployment Script (PowerShell) - Refactored to use .env.development
# =============================================================================
# This script allows you to test changes quickly by:
# 1. Building Docker image locally
# 2. Pushing to Azure Container Registry
# 3. Deploying with Helm using the same configuration as Azure DevOps pipeline
#
# Prerequisites:
# - Azure CLI logged in
# - Docker running
# - Helm installed
# - kubectl configured for target cluster
# - Access to Azure Container Registry
# =============================================================================

param(
    [string]$DeployEnvironment = "dev",
    [bool]$EnableFlower = $true,
    [bool]$EnableObservability = $true,
    [bool]$UseSeparatedImages = $true,
    [bool]$UseFullImageForWorkers = $false,
    [bool]$UseFullImageForApi = $false,
    [switch]$Help
)

# Show usage if help is requested
if ($Help) {
    Write-Host "Usage: .\local-deploy.ps1 [-DeployEnvironment <env>] [-EnableFlower <bool>] [-EnableObservability <bool>] [-UseSeparatedImages <bool>] [-Help]"
    Write-Host ""
    Write-Host "Parameters:"
    Write-Host "  -DeployEnvironment    Target environment (default: dev)"
    Write-Host "  -EnableFlower         Deploy Flower UI and enable Celery events (default: true)"
    Write-Host "  -EnableObservability  Deploy Loki + Promtail + Grafana for log observability (default: true)"
    Write-Host "  -UseSeparatedImages   Build separate images per component (default: true, matches pipeline)"
    Write-Host "  -UseFullImageForWorkers Build worker images using the 'full' target to include API deps (default: false)"
    Write-Host "  -UseFullImageForApi   Build API image using the 'full' target (default: false)"
    Write-Host "  -Help                 Show this help message"
    Write-Host ""
    Write-Host "Example:"
    Write-Host "  .\local-deploy.ps1                              # Deploy with all features enabled"
    Write-Host "  .\local-deploy.ps1 -EnableFlower `$false         # Deploy without Flower UI"
    Write-Host "  .\local-deploy.ps1 -EnableObservability `$false  # Deploy without observability stack"
    Write-Host "  .\local-deploy.ps1 -UseSeparatedImages `$false   # Use legacy single image"
    exit 0
}

# Error handling
$ErrorActionPreference = "Stop"

# =============================================================================
# Load .env.development file and set environment variables
# =============================================================================
function Import-EnvFile {
    param([string]$FilePath)
    if (-not (Test-Path $FilePath)) {
        Write-Error "Environment file not found: $FilePath"
        exit 1
    }
    Get-Content $FilePath | ForEach-Object {
        if ($_ -match "^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$") {
            $name = $matches[1]
            $value = $matches[2]
            # Remove surrounding quotes if present
            if ($value.StartsWith('"') -and $value.EndsWith('"')) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            Set-Item -Path "Env:$name" -Value $value
        }
    }
}

# Use $PSScriptRoot to reference .env.development relative to script location
$envFilePath = Join-Path $PSScriptRoot "..\..\env.development"
if (-not (Test-Path $envFilePath)) {
    $envFilePath = Join-Path $PSScriptRoot "..\..\.env.development"
}
Import-EnvFile $envFilePath

# =============================================================================
# Configuration
# =============================================================================

# Azure Configuration (from .env.development)
$ACR_NAME = $env:AZURE_ACR_NAME
$ACR_LOGIN_SERVER = "$ACR_NAME.azurecr.io"
$RESOURCE_GROUP = $env:AZURE_RESOURCE_GROUP
$SUBSCRIPTION_ID = $env:AZURE_SUBSCRIPTION_ID

# Kubernetes Configuration (from .env.development)
$NAMESPACE = "copilotgtm"
$HELM_RELEASE_NAME = "copilot-backend"
$CHART_PATH = "$PSScriptRoot\..\..\charts\application"

# Image Configuration
$IMAGE_REPOSITORY = "copilot-backend"  # legacy/full image repository
$API_IMAGE_REPOSITORY = "copilot-backend-api"
$KNOWLEDGE_IMAGE_REPOSITORY = "copilot-backend-knowledge-worker"
$LIGHT_IMAGE_REPOSITORY = "copilot-backend-light-worker"
$INTEGRATIONS_IMAGE_REPOSITORY = "copilot-backend-integrations-worker"
$IMAGE_TAG = "local-$(Get-Date -Format 'yyyyMMdd-HHmmss')"

# =============================================================================
# Functions
# =============================================================================

function Write-Header {
    param([string]$Message)
    Write-Host "=================================" -ForegroundColor Blue
    Write-Host $Message -ForegroundColor Blue
    Write-Host "=================================" -ForegroundColor Blue
}

function Write-Success {
    param([string]$Message)
    Write-Host "✅ $Message" -ForegroundColor Green
}

function Write-Warning {
    param([string]$Message)
    Write-Host "⚠️  $Message" -ForegroundColor Yellow
}

function Write-Error {
    param([string]$Message)
    Write-Host "❌ $Message" -ForegroundColor Red
}

function Test-Prerequisites {
    Write-Header "Checking Prerequisites"

    # Check if required tools are installed
    try {
        docker --version | Out-Null
    } catch {
        Write-Error "Docker is required but not installed. Aborting."
        exit 1
    }

    try {
        az --version | Out-Null
    } catch {
        Write-Error "Azure CLI is required but not installed. Aborting."
        exit 1
    }

    try {
        helm version | Out-Null
    } catch {
        Write-Error "Helm is required but not installed. Aborting."
        exit 1
    }

    try {
        kubectl version --client | Out-Null
    } catch {
        Write-Error "kubectl is required but not installed. Aborting."
        exit 1
    }

    # Check if logged into Azure
    try {
        az account show | Out-Null
    } catch {
        Write-Error "Not logged into Azure CLI. Run 'az login' first."
        exit 1
    }

    # Check if Docker is running
    try {
        docker ps | Out-Null
    } catch {
        Write-Error "Docker is not running. Please start Docker."
        exit 1
    }

    # Check if Dockerfile exists (relative to repo root)
    $dockerfilePath = Join-Path $PSScriptRoot "..\..\Dockerfile"
    if (-not (Test-Path $dockerfilePath)) {
        Write-Error "Dockerfile not found in repo root directory."
        exit 1
    }

    # Check if Helm chart exists
    if (-not (Test-Path $CHART_PATH)) {
        Write-Error "Helm chart not found at $CHART_PATH."
        exit 1
    }

    Write-Success "All prerequisites checked"
}

function Connect-ToACR {
    Write-Header "Logging into Azure Container Registry"

    # Login to ACR
    az acr login --name $ACR_NAME
    Write-Success "Logged into ACR: $ACR_LOGIN_SERVER"
}

function Build-AndPushImage {
    Write-Header "Building and Pushing Docker Image"

    if (-not $UseSeparatedImages) {
        # Legacy behavior: build single full image (default Dockerfile target)
        Write-Host "Building image: $ACR_LOGIN_SERVER/$IMAGE_REPOSITORY`:$IMAGE_TAG"
        docker build -t "$ACR_LOGIN_SERVER/$IMAGE_REPOSITORY`:$IMAGE_TAG" $PSScriptRoot\..\..

        # Also tag as latest for this local deployment
        docker tag "$ACR_LOGIN_SERVER/$IMAGE_REPOSITORY`:$IMAGE_TAG" "$ACR_LOGIN_SERVER/$IMAGE_REPOSITORY`:latest"

        Write-Host "Pushing image to ACR..."
        docker push "$ACR_LOGIN_SERVER/$IMAGE_REPOSITORY`:$IMAGE_TAG"
        docker push "$ACR_LOGIN_SERVER/$IMAGE_REPOSITORY`:latest"

        Write-Success "Image pushed: $ACR_LOGIN_SERVER/$IMAGE_REPOSITORY`:$IMAGE_TAG"
        return
    }

    # Separated images: build Dockerfile targets and push to distinct repositories
    Write-Host "Building separated images (Dockerfile targets) with tag: $IMAGE_TAG"

    $apiTarget = if ($UseFullImageForApi) { "full" } else { "api" }
    $knowledgeTarget = if ($UseFullImageForWorkers) { "full" } else { "knowledge-worker" }
    $lightWorkerTarget = if ($UseFullImageForWorkers) { "full" } else { "light-worker" }
    $integrationsTarget = if ($UseFullImageForWorkers) { "full" } else { "integrations-worker" }

    if ($UseFullImageForApi) {
        Write-Warning "Using 'full' image target for API to include async Key Vault deps (aiohttp) in local deploy."
    }

    if ($UseFullImageForWorkers) {
        Write-Warning "Using 'full' image target for workers to ensure API deps (fastapi) are present."
    }

    $repoToTarget = @(
        @{ Repo = $API_IMAGE_REPOSITORY; Target = $apiTarget },
        @{ Repo = $KNOWLEDGE_IMAGE_REPOSITORY; Target = $knowledgeTarget },
        @{ Repo = $LIGHT_IMAGE_REPOSITORY; Target = $lightWorkerTarget },
        @{ Repo = $INTEGRATIONS_IMAGE_REPOSITORY; Target = $integrationsTarget }
    )

    foreach ($item in $repoToTarget) {
        $repo = $item.Repo
        $target = $item.Target

        Write-Host "Building $repo (target: $target) => $ACR_LOGIN_SERVER/$repo`:$IMAGE_TAG"
        docker build --target $target -t "$ACR_LOGIN_SERVER/$repo`:$IMAGE_TAG" $PSScriptRoot\..\..

        docker tag "$ACR_LOGIN_SERVER/$repo`:$IMAGE_TAG" "$ACR_LOGIN_SERVER/$repo`:latest"

        Write-Host "Pushing $repo to ACR..."
        docker push "$ACR_LOGIN_SERVER/$repo`:$IMAGE_TAG"
        docker push "$ACR_LOGIN_SERVER/$repo`:latest"
    }

    Write-Success "Separated images pushed with tag: $IMAGE_TAG"
}

function Get-AzureKeyVaultSecrets {
    Write-Header "Checking Secrets Configuration"

    # List of required secrets
    $requiredSecrets = @(
        "AZURE_OPENAI_API_KEY",
        "AZURE_EMBEDDER_OPENAI_API_KEY",
        "DATABASE_PASSWORD",
        "REDIS_PASSWORD",
        "RESEND_API_KEY",
        "SUPABASE_ANON_KEY",
        "SUPABASE_JWT_SECRET",
        "SUPABASE_SERVICE_ROLE_KEY",
        "NANGO_SECRET_KEY"
    )

    # Check if all secrets are already set from .env.development
    $allSecretsPresent = $true
    foreach ($secret in $requiredSecrets) {
        $value = [Environment]::GetEnvironmentVariable($secret)
        if (-not $value) {
            $allSecretsPresent = $false
            Write-Host "  Missing: $secret"
        }
    }

    # Ensure NANGO_BASE_URL is set
    if (-not $env:NANGO_BASE_URL) {
        $env:NANGO_BASE_URL = "https://api.nango.dev"
    }

    if ($allSecretsPresent) {
        Write-Success "All secrets already present from .env.development - skipping Key Vault retrieval"
        return
    }

    # If any secrets are missing, try to retrieve from Key Vault
    Write-Host "Some secrets missing - attempting to retrieve from Azure Key Vault..."

    # Key Vault name for infrastructure secrets (from .env.development)
    $KEY_VAULT_NAME = $env:AZURE_KEY_VAULT_NAME

    if (-not $KEY_VAULT_NAME) {
        Write-Warning "AZURE_KEY_VAULT_NAME not set and some secrets are missing. Please ensure all secrets are in .env.development"
        return
    }

    Write-Host "Retrieving secrets from Key Vault: $KEY_VAULT_NAME"

    # Function to safely get secret
    function Get-SecretSafely {
        param([string]$VaultName, [string]$SecretName, [string]$DefaultValue = "")
        try {
            $result = az keyvault secret show --vault-name $VaultName --name $SecretName --query value -o tsv 2>$null
            if ($result) { return $result } else { return $DefaultValue }
        } catch {
            return $DefaultValue
        }
    }

    # Only fetch secrets that are not already set
    if (-not $env:AZURE_OPENAI_API_KEY) {
        $env:AZURE_OPENAI_API_KEY = Get-SecretSafely $KEY_VAULT_NAME "azure-openai-api-key"
    }
    if (-not $env:AZURE_EMBEDDER_OPENAI_API_KEY) {
        $env:AZURE_EMBEDDER_OPENAI_API_KEY = Get-SecretSafely $KEY_VAULT_NAME "azure-embedder-openai-api-key"
    }
    if (-not $env:DATABASE_PASSWORD) {
        $env:DATABASE_PASSWORD = Get-SecretSafely $KEY_VAULT_NAME "database-password"
    }
    if (-not $env:REDIS_PASSWORD) {
        $env:REDIS_PASSWORD = Get-SecretSafely $KEY_VAULT_NAME "redis-password"
    }
    if (-not $env:RESEND_API_KEY) {
        $env:RESEND_API_KEY = Get-SecretSafely $KEY_VAULT_NAME "resend-api-key"
    }
    if (-not $env:SUPABASE_ANON_KEY) {
        $env:SUPABASE_ANON_KEY = Get-SecretSafely $KEY_VAULT_NAME "supabase-anon-key"
    }
    if (-not $env:SUPABASE_JWT_SECRET) {
        $env:SUPABASE_JWT_SECRET = Get-SecretSafely $KEY_VAULT_NAME "supabase-jwt-secret"
    }
    if (-not $env:SUPABASE_SERVICE_ROLE_KEY) {
        $env:SUPABASE_SERVICE_ROLE_KEY = Get-SecretSafely $KEY_VAULT_NAME "supabase-service-role-key"
    }
    if (-not $env:NANGO_SECRET_KEY) {
        $env:NANGO_SECRET_KEY = Get-SecretSafely $KEY_VAULT_NAME "nango-secret-key"
    }

    Write-Success "Secrets configuration complete"
}

function Deploy-WithHelm {
    Write-Header "Deploying with Helm"

    # Check if Helm is locked and fix it
    Write-Host "Checking Helm release status..."
    $helmStatus = helm status copilot-backend -n $NAMESPACE 2>&1
    if ($helmStatus -match "pending-upgrade|pending-install|pending-rollback") {
        Write-Warning "Helm release is in pending state. Rolling back to unlock..."
        helm rollback copilot-backend -n $NAMESPACE
        Start-Sleep -Seconds 10
    }

    # Clean up previous startup job and pods
    Write-Host "Cleaning up previous startup job and pods..."
    kubectl delete job copilot-backend-startup -n $NAMESPACE --ignore-not-found=true
    kubectl delete pods -n $NAMESPACE -l job-name=copilot-backend-startup --ignore-not-found=true
    
    # Clean up any stuck GPU worker pods
    Write-Host "Cleaning up any stuck GPU worker pods..."
    kubectl delete pods -n $NAMESPACE -l app.kubernetes.io/name=knowledge-extract-worker --grace-period=0 --force --ignore-not-found=true
    Start-Sleep -Seconds 5

    # Deploy with Helm using the same configuration as Azure DevOps pipeline
    Write-Host "Deploying with Helm release: $HELM_RELEASE_NAME"

    $apiRepo = if ($UseSeparatedImages) { "$ACR_LOGIN_SERVER/$API_IMAGE_REPOSITORY" } else { "$ACR_LOGIN_SERVER/$IMAGE_REPOSITORY" }
    $knowledgeRepo = if ($UseSeparatedImages) { "$ACR_LOGIN_SERVER/$KNOWLEDGE_IMAGE_REPOSITORY" } else { "$ACR_LOGIN_SERVER/$IMAGE_REPOSITORY" }
    $lightRepo = if ($UseSeparatedImages) { "$ACR_LOGIN_SERVER/$LIGHT_IMAGE_REPOSITORY" } else { "$ACR_LOGIN_SERVER/$IMAGE_REPOSITORY" }
    $integrationsRepo = if ($UseSeparatedImages) { "$ACR_LOGIN_SERVER/$INTEGRATIONS_IMAGE_REPOSITORY" } else { "$ACR_LOGIN_SERVER/$IMAGE_REPOSITORY" }

    $helmArgs = @(
        "upgrade", "--install", $HELM_RELEASE_NAME, $CHART_PATH,
        "--namespace", $NAMESPACE,
        "--create-namespace",
        "--set", "api.workloadIdentity.clientId=$($env:WORKLOAD_IDENTITY_BACKEND_CLIENT_ID)",
        "--set", "api.workloadIdentity.tenantId=$($env:WORKLOAD_IDENTITY_API_TENANT_ID)",
        "--set", "global.redis.host=$($env:REDIS_HOST)",
        "--set", "global.redis.port=$($env:REDIS_PORT)",
        "--set", "global.redis.enableTLS=true",
        "--set", "global.redis.usePassword=true",
        "--set", "api.image.repository=$apiRepo",
        "--set", "api.image.tag=$IMAGE_TAG",
        "--set", "knowledge-extract-worker.image.repository=$knowledgeRepo",
        "--set", "knowledge-extract-worker.image.tag=$IMAGE_TAG",
        "--set", "knowledge-extract-worker.autoscaling.enabled=true",
        "--set", "knowledge-extract-worker.gpu.enabled=true",
        "--set", "knowledge-upload-worker.image.repository=$knowledgeRepo",
        "--set", "knowledge-upload-worker.image.tag=$IMAGE_TAG",
        "--set", "knowledge-upload-worker.autoscaling.enabled=true",
        "--set", "organization-worker.image.repository=$lightRepo",
        "--set", "organization-worker.image.tag=$IMAGE_TAG",
        "--set", "organization-worker.autoscaling.enabled=true",
        "--set", "chat-worker.image.repository=$lightRepo",
        "--set", "chat-worker.image.tag=$IMAGE_TAG",
        "--set", "integrations-worker.image.repository=$integrationsRepo",
        "--set", "integrations-worker.image.tag=$IMAGE_TAG",
        "--set", "integrations-worker.autoscaling.enabled=true",
        "--set", "deal-worker.image.repository=$lightRepo",
        "--set", "deal-worker.image.tag=$IMAGE_TAG",
        "--set", "deal-worker.autoscaling.enabled=true",
        "--set", "deal-beat.image.repository=$lightRepo",
        "--set", "deal-beat.image.tag=$IMAGE_TAG",
        "--set", "integrations-beat.image.repository=$integrationsRepo",
        "--set", "integrations-beat.image.tag=$IMAGE_TAG",
        "--set", "chat-worker.autoscaling.enabled=true",
        "--set", "api.secrets.DATABASE_HOST=$($env:DATABASE_HOST)",
        "--set", "api.secrets.DATABASE_USER=$($env:DATABASE_USER)",
        "--set", "api.secrets.DATABASE_PORT=$($env:DATABASE_PORT)",
        "--set", "api.secrets.DATABASE_NAME=$($env:DATABASE_NAME)",
        "--set", "api.secrets.DATABASE_PASSWORD=$($env:DATABASE_PASSWORD)",
        "--set", "api.secrets.DATABASE_SSL_MODE=$($env:DATABASE_SSL_MODE)",
        "--set", "api.secrets.REDIS_HOST=$($env:REDIS_HOST)",
        "--set", "api.secrets.REDIS_PORT=$($env:REDIS_PORT)",
        "--set", "api.secrets.REDIS_PASSWORD=$($env:REDIS_PASSWORD)",
        "--set", "api.secrets.REDIS_SSL_CERT_REQS=CERT_REQUIRED",
        "--set", "api.secrets.AZURE_OPENAI_API_KEY=$($env:AZURE_OPENAI_API_KEY)",
        "--set", "api.secrets.AZURE_OPENAI_ENDPOINT=$($env:AZURE_OPENAI_ENDPOINT)",
        "--set", "api.secrets.CLOUD_PROVIDER=$($env:CLOUD_PROVIDER)",
        "--set", "api.secrets.APPLICATION_STORAGE_NAME=$($env:APPLICATION_STORAGE_NAME)",
        "--set", "api.secrets.ORGANISATION_STORAGE_NAME=$($env:ORGANISATION_STORAGE_NAME)",
        "--set", "api.secrets.APPLICATION_REGION=$($env:APPLICATION_REGION)",
        "--set", "api.secrets.ENV=$($env:ENV)",
        "--set", "api.secrets.AZURE_KEY_VAULT_URL=$($env:AZURE_KEY_VAULT_URL)",
        "--set", "api.secrets.AZURE_RESOURCE_GROUP=$RESOURCE_GROUP",
        "--set", "api.secrets.AZURE_SUBSCRIPTION_ID=$SUBSCRIPTION_ID",
        "--set", "api.secrets.SUPABASE_URL=$($env:SUPABASE_URL)",
        "--set", "api.secrets.SUPABASE_ANON_KEY=$($env:SUPABASE_ANON_KEY)",
        "--set", "api.secrets.SUPABASE_SERVICE_ROLE_KEY=$($env:SUPABASE_SERVICE_ROLE_KEY)",
        "--set", "api.secrets.SUPABASE_JWT_SECRET=$($env:SUPABASE_JWT_SECRET)",
        "--set", "api.secrets.RESEND_API_KEY=$($env:RESEND_API_KEY)",
        "--set", "api.secrets.FROM_EMAIL=$($env:FROM_EMAIL)",
        "--set", "api.secrets.APP_NAME=$($env:APP_NAME)",
        "--set", "api.secrets.APP_URL=$($env:APP_URL)",
        "--set", "api.secrets.ADMIN_APP_URL=$($env:ADMIN_APP_URL)",
        "--set", "api.secrets.LOG_FORMAT=json",
        "--set", "api.secrets.SUPER_ADMIN_EMAIL=$($env:SUPER_ADMIN_EMAIL)",
        "--set", "api.secrets.WEB_APP_NAME=$($env:WEB_APP_NAME)",
        "--set", "api.secrets.AZURE_EMBEDDER_OPENAI_API_KEY=$($env:AZURE_EMBEDDER_OPENAI_API_KEY)",
        "--set", "api.secrets.AZURE_EMBEDDER_OPENAI_ENDPOINT=$($env:AZURE_EMBEDDER_OPENAI_ENDPOINT)",
        "--set", "api.secrets.AZURE_EMBEDDER_DEPLOYMENT=$($env:AZURE_EMBEDDER_DEPLOYMENT)",
        "--set", "api.secrets.AZURE_SUBSCRIPTION=$($env:AZURE_SUBSCRIPTION)",
        "--set", "api.secrets.NANGO_SECRET_KEY=$($env:NANGO_SECRET_KEY)",
        "--set", "api.secrets.NANGO_BASE_URL=$($env:NANGO_BASE_URL)",
        "--set", "knowledge-extract-worker.workloadIdentity.clientId=$($env:WORKLOAD_IDENTITY_WORKERS_CLIENT_ID)",
        "--set", "knowledge-upload-worker.workloadIdentity.clientId=$($env:WORKLOAD_IDENTITY_WORKERS_CLIENT_ID)",
        "--set", "organization-worker.workloadIdentity.clientId=$($env:WORKLOAD_IDENTITY_WORKERS_CLIENT_ID)",
        "--set", "chat-worker.workloadIdentity.clientId=$($env:WORKLOAD_IDENTITY_WORKERS_CLIENT_ID)",
        "--set", "integrations-worker.workloadIdentity.clientId=$($env:WORKLOAD_IDENTITY_WORKERS_CLIENT_ID)",
        "--set", "deal-worker.workloadIdentity.clientId=$($env:WORKLOAD_IDENTITY_WORKERS_CLIENT_ID)",
        "--set", "deal-beat.workloadIdentity.clientId=$($env:WORKLOAD_IDENTITY_WORKERS_CLIENT_ID)",
        "--set", "integrations-beat.workloadIdentity.clientId=$($env:WORKLOAD_IDENTITY_WORKERS_CLIENT_ID)",
        "--set", "knowledge-extract-worker.workloadIdentity.tenantId=$($env:WORKLOAD_IDENTITY_WORKERS_TENANT_ID)",
        "--set", "knowledge-upload-worker.workloadIdentity.tenantId=$($env:WORKLOAD_IDENTITY_WORKERS_TENANT_ID)",
        "--set", "organization-worker.workloadIdentity.tenantId=$($env:WORKLOAD_IDENTITY_WORKERS_TENANT_ID)",
        "--set", "chat-worker.workloadIdentity.tenantId=$($env:WORKLOAD_IDENTITY_WORKERS_TENANT_ID)",
        "--set", "integrations-worker.workloadIdentity.tenantId=$($env:WORKLOAD_IDENTITY_WORKERS_TENANT_ID)",
        "--set", "deal-worker.workloadIdentity.tenantId=$($env:WORKLOAD_IDENTITY_WORKERS_TENANT_ID)",
        "--set", "deal-beat.workloadIdentity.tenantId=$($env:WORKLOAD_IDENTITY_WORKERS_TENANT_ID)",
        "--set", "integrations-beat.workloadIdentity.tenantId=$($env:WORKLOAD_IDENTITY_WORKERS_TENANT_ID)"
    )

    if ($EnableFlower) {
        Write-Host "Flower enabled: deploying Flower UI and enabling Celery events for workers..."
        $helmArgs += @(
            "--set", "flower.enabled=true",
            "--set", "flower.ingress.enabled=false",
            "--set", "global.celery.enableEvents=true"
        )
    }

    if ($EnableObservability) {
        Write-Host "Observability enabled: deploying Loki + Promtail + Grafana..."
        $helmArgs += @(
            "--set", "loki.enabled=true",
            "--set", "promtail.enabled=true",
            "--set", "grafana.enabled=true"
        )
    }

    # Execute Helm command and check for errors
    $helmResult = & helm @helmArgs
    $helmExitCode = $LASTEXITCODE
    
    if ($helmExitCode -ne 0) {
        Write-Error "Helm deployment failed with exit code: $helmExitCode"
        Write-Host "Helm output: $helmResult"
        Write-Host "Attempting to check Helm status..."
        helm status copilot-backend -n $NAMESPACE
        exit 1
    }

    Write-Success "Helm deployment completed"
    
    # Wait a moment for pods to be created
    Start-Sleep -Seconds 10
    
    # Force restart worker pods to ensure they pick up latest values
    Write-Host "Restarting worker deployments to ensure latest values are applied..."
    kubectl rollout restart deployment copilot-backend-knowledge-extract-worker -n $NAMESPACE
    kubectl rollout restart deployment copilot-backend-knowledge-upload-worker -n $NAMESPACE
    kubectl rollout restart deployment copilot-backend-chat-worker -n $NAMESPACE
    kubectl rollout restart deployment copilot-backend-organization-worker -n $NAMESPACE
    kubectl rollout restart deployment copilot-backend-integrations-worker -n $NAMESPACE
    kubectl rollout restart deployment copilot-backend-deal-worker -n $NAMESPACE
    kubectl rollout restart deployment copilot-backend-deal-beat -n $NAMESPACE
    
    Write-Host "Waiting for rollouts to complete..."
    kubectl rollout status deployment copilot-backend-knowledge-extract-worker -n $NAMESPACE --timeout=300s
    kubectl rollout status deployment copilot-backend-knowledge-upload-worker -n $NAMESPACE --timeout=300s
    kubectl rollout status deployment copilot-backend-chat-worker -n $NAMESPACE --timeout=300s
    kubectl rollout status deployment copilot-backend-organization-worker -n $NAMESPACE --timeout=300s
    kubectl rollout status deployment copilot-backend-integrations-worker -n $NAMESPACE --timeout=300s
    kubectl rollout status deployment copilot-backend-deal-worker -n $NAMESPACE --timeout=300s
    kubectl rollout status deployment copilot-backend-deal-beat -n $NAMESPACE --timeout=300s
    
    Write-Success "Worker restarts completed"

    if ($EnableObservability) {
        Write-Host "Restarting observability components to apply latest values..."
        kubectl rollout restart deployment copilot-backend-grafana -n $NAMESPACE
        kubectl rollout restart statefulset copilot-backend-loki -n $NAMESPACE
        kubectl rollout restart daemonset copilot-backend-promtail -n $NAMESPACE

        Write-Host "Waiting for observability rollouts to complete..."
        kubectl rollout status deployment copilot-backend-grafana -n $NAMESPACE --timeout=300s
        kubectl rollout status statefulset copilot-backend-loki -n $NAMESPACE --timeout=300s
    }
    
    # Verify GPU configuration was applied
    Write-Host "Verifying GPU configuration..."
    $gpuConfig = helm get values copilot-backend -n $NAMESPACE | Select-String -Pattern "gpu:" -Context 0,3
    if ($gpuConfig) {
        Write-Success "GPU configuration found in Helm values"
    } else {
        Write-Warning "GPU configuration not found in deployed values"
    }
}

function Test-DeploymentStatus {
    Write-Header "Checking Deployment Status"

    Write-Host "Pods in namespace $NAMESPACE`:"
    kubectl get pods -n $NAMESPACE

    Write-Host ""
    Write-Host "Deployments in namespace $NAMESPACE`:"
    kubectl get deployments -n $NAMESPACE

    Write-Host ""
    Write-Host "Services in namespace $NAMESPACE`:"
    kubectl get services -n $NAMESPACE

    Write-Host ""
    Write-Host "Checking for worker deployments..."
    $workers = kubectl get deployments -n $NAMESPACE | Select-String "knowledge"
    if ($workers) {
        $workers
    } else {
        Write-Host "No knowledge workers deployed yet"
    }

    $integrations = kubectl get deployments -n $NAMESPACE | Select-String "integrations"
    if ($integrations) {
        $integrations
    } else {
        Write-Host "No integrations workers deployed yet"
    }

    Write-Success "Deployment status checked"
}

# =============================================================================
# Main Execution
# =============================================================================

function Main {
    Write-Header "Local Development Deployment Script"
    Write-Host "Environment: $DeployEnvironment"
    Write-Host "Image Tag: $IMAGE_TAG"
    Write-Host "ACR: $ACR_LOGIN_SERVER"
    Write-Host ""

    # Run deployment steps
    Test-Prerequisites
    Connect-ToACR
    Build-AndPushImage
    Get-AzureKeyVaultSecrets
    Deploy-WithHelm
    Test-DeploymentStatus

    Write-Success "Deployment completed successfully!"

    if ($EnableFlower) {
        Write-Host ""
        Write-Host "Flower UI is deployed (internal). Access it from your machine with:"
        Write-Host "  kubectl port-forward -n $NAMESPACE svc/$HELM_RELEASE_NAME-flower 5555:5555"
        Write-Host "  Then open: http://localhost:5555"
    }
    if ($EnableObservability) {
        Write-Host ""
        Write-Host "Observability Stack is deployed. Access Grafana from your machine with:"
        Write-Host "  kubectl port-forward -n $NAMESPACE svc/$HELM_RELEASE_NAME-grafana 3001:3000"
        Write-Host "  Then open: http://localhost:3001 (admin/admin)"
    }
    Write-Host ""
    Write-Host "🚀 Your application should now be running with the latest changes."
    Write-Host "🔍 Check the status with: kubectl get pods -n $NAMESPACE"
    Write-Host "📝 View logs with: kubectl logs -f deployment/copilot-backend-api -n $NAMESPACE"
}

# Run main function
Main
