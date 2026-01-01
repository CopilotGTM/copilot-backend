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

# -----------------------------------------------------------------
# Networking
# -----------------------------------------------------------------
# vnet_name                     = "copilot-gtm-vnet"      # Default
# vnet_address_space            = ["10.0.0.0/16"]         # Default
# vnet_public_subnets           = ["10.0.1.0/24"]         # Default
# vnet_private_subnets          = ["10.0.2.0/24", "10.0.3.0/24"]  # Default
# vnet_private_endpoints_subnet = "10.0.4.0/24"           # Default
# vnet_waf_subnet               = "10.0.5.0/24"           # Default

# Private Link (enabled for SOC2 compliance in prod)
enable_key_vault_private_endpoints = true
enable_storage_private_endpoints   = true

# -----------------------------------------------------------------
# AKS
# -----------------------------------------------------------------
# aks_name           = "copilot-gtm-aks"     # Default
# aks_dns_prefix     = "aks"                 # Default
# aks_node_count     = 2                     # Default
aks_vm_size = "Standard_D4s_v3" # Override: larger than default Standard_DS2_v2
# aks_os_disk_size_gb = 128                  # Default
# aks_service_cidr   = "10.1.0.0/16"         # Default
# aks_dns_service_ip = "10.1.0.10"           # Default
aks_main_app_namespace       = "copilotgtm"      # Override: custom namespace
aks_main_app_service_account = "copilot-backend" # Override: custom service account

# -----------------------------------------------------------------
# ACR & Redis
# -----------------------------------------------------------------
# acr_name     = "copilotgtmacr"       # Default
acr_type = "Premium" # Override: Premium for geo-replication
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
# WAF / Application Gateway
# -----------------------------------------------------------------
# waf_name = "copilot-gtm-waf"  # Default

# -----------------------------------------------------------------
# DNS (Required)
# -----------------------------------------------------------------
dns_zone_name           = "copilotgtm.com"
dns_zone_resource_group = "master-resources"
# frontend_dns_a_record_name       = "app"    # Default for prod
# frontend_admin_dns_a_record_name = "admin"  # Default for prod

# -----------------------------------------------------------------
# Azure DevOps (Required - org specific)
# -----------------------------------------------------------------
org_service_url         = "https://dev.azure.com/copilotgtm"
devops_project_name     = "app-copilotgtm"
devops_environment_name = "production"

# Service Principal & Agent
# sp_name                     = "copilot-gtm-devops-sp"  # Default
# azuredevops_sp_display_name = "TerraformSP"            # Default
# agent_pool                  = "StaticAgentPool"        # Default
# agent_name                  = "gtm-devops-agent-01"    # Default
# admin_username              = "azureuser"              # Default

# -----------------------------------------------------------------
# Repository Names (Required - external resources in Azure DevOps)
# -----------------------------------------------------------------
backend_repo_name = "copilot-backend"
backend_yml_path  = ".azure-pipelines/azure-pipelines.yml"
# backend_pipeline_name = "backend-pipeline"  # Default
# backend_branch        = "main"              # Default for prod

frontend_repo_name = "copilot-app-frontend"
frontend_yml_path  = "pipelines/.azure-pipelines/azure-pipelines.yml"
# frontend_pipeline_name = "frontend-pipeline"  # Default
# frontend_branch        = "main"               # Default for prod

frontend_admin_repo_name = "copilot-admin-frontend"
frontend_admin_yml_path  = "pipelines/.azure-pipelines/azure-pipelines.yml"
# frontend_admin_pipeline_name = "frontend-admin-pipeline"  # Default
# frontend_admin_branch        = "main"                     # Default for prod

terraform_repo_name = "copilot-app-infra"
# terraform_pipeline_name = "infra-pipeline"               # Default
# terraform_yml_path      = ".pipeline/.azure-pipeline.yml" # Default

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
# External Services
# -----------------------------------------------------------------
super_admin_email = "tech@copilotgtm.com"
supabase_url      = "https://mdpbmdnwwdkjcxefbbbk.supabase.co"
resend_email      = "noreply@team.copilotgtm.com" # Override from default

# -----------------------------------------------------------------
# Secrets (provided via TF_VAR_* environment variables)
# -----------------------------------------------------------------
# TF_VAR_azuredevops_pat           - Azure DevOps Personal Access Token
# TF_VAR_supabase_anon_key         - Supabase Anonymous Key
# TF_VAR_supabase_service_role_key - Supabase Service Role Key
# TF_VAR_supabase_jwt_secret       - Supabase JWT Secret
# TF_VAR_resend_api_key            - Resend Email API Key
# TF_VAR_nango_secret              - Nango Integration Secret
