# =================================================================
# CopilotGTM Prod Environment Configuration
# =================================================================
# Only variables that are REQUIRED or OVERRIDE defaults are included.
# Commented variables show available customizations (using defaults).
# Secrets are provided via Azure DevOps Variable Group (TF_VAR_*)
# =================================================================

# -----------------------------------------------------------------
# Core Settings (Required)
# -----------------------------------------------------------------
subscription_id     = "675df0ed-8e06-441b-a1e4-110d19521816"
resource_group_name = "copilot-gtm-app"
location            = "centralus"
project_name        = "copilotgtm"

# -----------------------------------------------------------------
# Database (uses same location as main resources)
# -----------------------------------------------------------------
db_location = "centralus" # Same as location - can be different if needed
db_name     = "copilotgtm-postgresdb-centralus-prod"

# PostgreSQL SKU - General Purpose required for PgBouncer
# GP_Standard_D2s_v3 = 2 vCores, 8GB RAM (~$100/month)
db_sku_name = "GP_Standard_D2s_v3"

# PgBouncer - Connection pooling to share DB connections across all workers
pgbouncer_enabled           = true
pgbouncer_default_pool_size = 200           # Actual PostgreSQL connections (max 859 on GP_D2s_v3)
pgbouncer_max_client_conn   = 500           # Max client connections from workers/API to PgBouncer
pgbouncer_pool_mode         = "transaction" # Release connection after transaction

# -----------------------------------------------------------------
# Networking
# -----------------------------------------------------------------
# vnet_name                     = "copilot-gtm-vnet"      # Default
# vnet_address_space            = ["10.0.0.0/16"]         # Default
# vnet_public_subnets           = ["10.0.1.0/24"]         # Default
# vnet_private_subnets          = ["10.0.2.0/24", "10.0.3.0/24"]  # Default
# vnet_private_endpoints_subnet = "10.0.4.0/24"           # Default
# vnet_waf_subnet               = "10.0.5.0/24"           # Default
# Managed DevOps Pools need one dedicated delegated subnet per pool.
enable_managed_devops_pools = true
# managed_devops_dev_center_project_resource_id = "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.DevCenter/projects/<project>"
managed_devops_pools_subnets = [
  { name = "mdp-prod-deploy", address_prefix = "10.0.6.0/27" },
]
managed_devops_pool_sku_name = "Standard_D2ads_v5"

# Private Link (disabled for initial deployment - enable later with proper VNet DNS setup)
enable_key_vault_private_endpoints = false
enable_storage_private_endpoints   = false
enable_postgresql_private_endpoint = true

# -----------------------------------------------------------------
# AKS
# -----------------------------------------------------------------
# aks_name           = "copilot-gtm-aks"     # Default
# aks_dns_prefix     = "aks"                 # Default
# aks_node_count     = 2                     # Default (ignored when autoscaling enabled)
aks_vm_size               = "Standard_D2as_v6" # Cost optimization: 2 vCores, 8GB RAM (down from D4s_v3 4 vCores)
aks_max_pods              = 35
aks_enable_auto_scaling   = true
aks_min_node_count        = 3
aks_max_node_count        = 6
aks_os_disk_size_gb       = 64
aks_outbound_type         = "loadBalancer"
aks_existing_principal_id = "fdf38c8e-7ddd-42f1-9c53-b3feef8ed28d"
# aks_service_cidr   = "10.1.0.0/16"         # Default
# aks_dns_service_ip = "10.1.0.10"           # Default
aks_main_app_namespace       = "copilotgtm"      # Override: custom namespace
aks_main_app_service_account = "copilot-backend" # Override: custom service account

# -----------------------------------------------------------------
# ACR & Redis
# -----------------------------------------------------------------
# acr_name     = "copilotgtmacr"       # Default
acr_type = "Standard"
# redis_name   = "copilot-gtm-redis"   # Default
redis_sku_name = "Basic" # Override: Basic for dev (cheaper)
# redis_family   = "C"                 # Default
# redis_capacity = 0                   # Default

# -----------------------------------------------------------------
# Frontend App Service
# -----------------------------------------------------------------
# frontend_app_service_plan_name = "copilot-gtm-frontend-asp"  # Default
# frontend_app_service_name      = "copilot-gtm-frontend-app"  # Default
frontend_sku_name = "B2" # Override: B2 for better performance
# frontend_os_type    = "Linux"   # Default
# frontend_https_only = true      # Default

# -----------------------------------------------------------------
# Frontend Admin App Service
# -----------------------------------------------------------------
# frontend_admin_app_service_plan_name = "copilot-gtm-admin-asp"  # Default
# frontend_admin_app_service_name      = "copilot-gtm-admin-app"  # Default
# frontend_admin_sku_name = "B1"       # Default
# frontend_admin_os_type  = "Linux"    # Default

# -----------------------------------------------------------------
# Storage
# -----------------------------------------------------------------
storage_account_name = "masterstorage" # Override: existing storage account name
# org_storage_account_name = "organisationstorage"  # Default

# -----------------------------------------------------------------
# DNS (Required)
# -----------------------------------------------------------------
dns_zone_name           = "copilotgtm.com"
dns_zone_resource_group = "master-resources"
# frontend_dns_a_record_name       = "app"    # Default for prod
# frontend_admin_dns_a_record_name = "admin"  # Default for prod

# -----------------------------------------------------------------
# Envoy Gateway
# -----------------------------------------------------------------
enable_envoy_gateway         = true
envoy_gateway_class_name     = "envoy-gateway-prod"
envoy_gateway_public_ip_name = "copilot-gtm-envoy-gateway-prod-pip"
envoy_validation_subdomain   = "api-envoy"

# -----------------------------------------------------------------
# Azure DevOps (Required - org specific)
# -----------------------------------------------------------------
org_service_url         = "https://dev.azure.com/copilotgtm"
devops_project_name     = "app-copilotgtm"
devops_environment_name = "production"

# Service Principal & Agent
# sp_name                     = "copilot-gtm-devops-sp"  # Default
# azuredevops_sp_display_name = "TerraformSP"            # Default
azure_resource_owner_object_id = "4c81e541-7b3f-4d76-bcca-a04da9a15962"
agent_pool                     = "StaticAgentPool-prod" # Override: unique pool name to avoid collision with dev in shared Azure DevOps org
# Target Managed DevOps Pool name after validation:
managed_devops_prod_deploy_pool_name = "copilot-devops-prod-deploy"
# agent_name                  = "gtm-devops-agent-01"    # Default
# admin_username              = "azureuser"              # Default

# -----------------------------------------------------------------
# Repository Names (Required - external resources in Azure DevOps)
# -----------------------------------------------------------------
backend_repo_name = "copilot-backend"
backend_yml_path  = ".azure-pipelines/azure-backend-pipeline-prod.yml"
# backend_pipeline_name = "backend-pipeline"  # Default
# backend_branch        = "main"              # Default for prod

frontend_repo_name = "copilot-app-frontend"
frontend_yml_path  = "pipelines/.azure-pipelines/azure-pipelines-prod.yml"
# frontend_pipeline_name = "frontend-pipeline"  # Default
# frontend_branch        = "main"               # Default for prod

frontend_admin_repo_name = "copilot-admin-frontend"
frontend_admin_yml_path  = "pipelines/.azure-pipelines/azure-pipelines-prod.yml"
# frontend_admin_pipeline_name = "frontend-admin-pipeline"  # Default
# frontend_admin_branch        = "main"                     # Default for prod

terraform_repo_name = "copilot-backend"
# terraform_pipeline_name = "infra-pipeline"               # Default
terraform_yml_path = ".azure-pipelines/azure-infra-pipeline-prod.yml"

# -----------------------------------------------------------------
# Cert-Manager & K8s
# -----------------------------------------------------------------
letsencrypt_email = "tech@copilotgtm.com"
api_subdomain     = "api"        # Override: api for prod environment
app_namespace     = "copilotgtm" # Override: custom namespace
# keda_namespace  = "keda-system"  # Default

# -----------------------------------------------------------------
# KEDA & GPU Node Pool
# -----------------------------------------------------------------
enable_keda_autoscaling = true # Override: enabled for autoscaling
# keda_polling_interval = 30     # Default
# keda_cooldown_period  = 300    # Default

enable_gpu_worker_pool = true              # Override: enabled for AI/ML workloads
gpu_vm_size            = "Standard_D8s_v3" # Override: high-performance compute
# gpu_node_pool_name   = "kgpu"            # Default
# gpu_min_count        = 0                 # Default (scale to zero)
# gpu_max_count        = 1                 # Default
# gpu_node_labels      = { workload = "knowledge-gpu" }      # Default
# gpu_node_taints      = ["dedicated=gpu:NoSchedule"]        # Default

# -----------------------------------------------------------------
# Microsoft Teams Bot
# -----------------------------------------------------------------
# Messaging endpoint is managed via Azure DevOps Variable Group (copilot-infra-secrets-prod)
# Change TF_VAR_teams_bot_messaging_endpoint in the Variable Group to switch URLs
# (e.g., ngrok for local dev, or deployed API endpoint)
teams_bot_messaging_endpoint = "https://api.copilotgtm.com/users/bots/teams/messages"

# -----------------------------------------------------------------
# OAuth App Registration — redirect URIs for prod environment
# -----------------------------------------------------------------
oauth_redirect_uris = [
  "https://api.copilotgtm.com/users/integrations/copilot-oauth/callback",
]

# -----------------------------------------------------------------
# Secrets (provided via TF_VAR_* environment variables)
# -----------------------------------------------------------------
# TF_VAR_azuredevops_pat           - Azure DevOps Personal Access Token
